# Same Person or Not? Entity-screening system

For each watchlist hit, the system decides whether it is the bank's customer
(`SAME_ENTITY`), a different person or company (`DIFFERENT_ENTITY`), or a case for an
analyst (`REVIEW`). It writes one JSON line per hit with a reason code, the resolved
customer record, a 2–4 sentence reason and the IDs of the records that decided it.

The policy core is deterministic Python. A language model is used only as an optional
fallback for four narrow free-text extraction tasks. Its answers are checked by code
before they can count as evidence, and the system clears a hit only when a fact
contradicts it.

## 1. How to run

```sh
system/run <data folder> <output file>            # e.g. system/run dev output/dev.jsonl
system/run <data folder> <output file> --strict   # re-raise per-hit errors (dev / CI)
system/run <data folder> <output file> --no-llm   # force deterministic-only, even with a key
```

- **Requirements:** Python 3 standard library only. There is nothing to install.
- **`OPENROUTER_API_KEY`** (optional): turns on the OpenRouter fallback. Without it the
  system runs fully deterministic and marks text it could not read as a *pending clue*
  (section 7).
- **`OPENROUTER_MODEL`** (optional): overrides the default model (section 8).
- The system reads only the named folder and never opens `practice_labels.jsonl`. It
  works from any working directory and writes the output atomically (a temp file, then
  `os.replace`). LLM call and token statistics go to stderr.

## 2. Architecture

```text
  <data folder>/*.jsonl + name_variants.json
                    │
                    ▼
  data.load_dataset ─────────── indices: account→holders, login→customers, email→customers,
                    │           (account,customer)→KYC applicant→checks, email→notes,
                    │           search-result query key→results, birth year / name token
                    │
        ┌───────────┴──────────────────────────────┐
        ▼  once per alert (cached)                 ▼  once per hit
  customer.CustomerResolver.resolve_alert    watchlist.resolve_hit
   ├ CustomerGraph (Union-Find)               ├ names + aliases, 5 country sources
   ├ link follow / joint holders / linkless   ├ dated reports, deceased_date
   ├ approved KYC checks + KYC notes          ├ DOB cascade: dobs → P1 → search results
   └ customer_notes (lazy, per email)         └ profile_text parsers
        │                                          │
        │   unreadable free text ──► llm_fallback (OpenRouter, optional)
        │                            post-validated, else PENDING_CLUE
        ▼                                          ▼
   CandidatePerson × N                        ResolvedHit
        └──────────────────┬───────────────────────┘
                           ▼
               decision.decide  (SOP rules, per person, then aggregate)
                           ▼
               explain.to_hit_decision  (reason + minimal evidence)
                           ▼
               main.py: sorted by hit_id, per-hit error → REVIEW, atomic JSONL write
```

| Module | Role |
|---|---|
| `contracts.py` | Frozen dataclasses (`Fact`, `DateSpec`, `CandidatePerson`, `ResolvedHit`, `HitDecision`), `FactStatus`, the `LLMFallback` protocol and its no-op `NullLLMFallback` |
| `data.py` | JSONL loader and indices |
| `names.py` | Name matching (POLICY principle 8) and the search-provider query key |
| `dates.py` | Date parsing in two modes, partial-date comparison, age to birth years, temporal bounds |
| `countries.py` | ISO-2/3 codes, aliases, demonyms and territories mapped to one canonical name |
| `customer.py`, `customer_notes.py` | Customer graph and resolution; KYC and residence note parsers |
| `watchlist.py`, `profile_text.py` | Hit resolution; parsers for paragraph 1, dated reports and search snippets |
| `decision.py`, `explain.py` | SOP decision engine; reason text and evidence |
| `llm_fallback.py` | OpenRouter client (stdlib `urllib`), cache, caps and post-validation |
| `main.py`, `run` | CLI, pipeline wiring, error isolation, atomic output |

`main.py` loads the dataset once and builds one `CustomerResolver`. It processes hits in
`(hit_id, alert_id)` order and resolves each alert's candidates once, caching them for
that alert's other hits. If one hit raises an exception, the system logs it to stderr and
emits `REVIEW / INSUFFICIENT_DATA` for that hit only. With `--strict` it re-raises
instead.

## 3. Resolving the customer

**One person, one picture (`CustomerGraph`).** A global Union-Find runs over every
customer record. Two kinds of edges join records:

- the same phone digits (from `customers.phone` or `accounts.phone`) together with the
  same full date of birth (CRM `dob` or an approved check's `verified_dob`);
- the same document number on approved KYC checks. Approved KYC notes that carry a
  document number add edges too. Their numbers are extracted once, over all notes, at
  start-up, so the graph never depends on which alert was resolved first.

The system never joins records on a phone or an address alone. Each cluster's root is its
lexicographically smallest record ID, so `customer_record_id` is stable.

**Following the link.** If the alert carries an `account_ref`, `login_handle` or `email`,
the system takes every record behind it (a joint account returns both holders) and
collapses them into person clusters. It keeps only the clusters where at least one record
fits the screened details. A record fits when the entity type, the country (residence or
nationality, else the approved document country), the city (city field or a token run in
the address) and the date of birth all agree, the date of birth by every part both sides
give, with CRM, approved KYC and approved-note dates all accepted. The name must also
agree: full, preferred or login name, login handle, or a verified KYC name. This
disambiguates joint holders the same way a linkless search would.

**No link.** The candidate pool is every record with the screened birth year, plus records
that have no birth year. If the screened details give no year, the pool comes from the
name-token index, and as a last resort it is every record of the screened entity type.
The same fit test runs on the pool, and the matches collapse into clusters. More than one
cluster means more than one person (section 6).

**Verified beats declared.** For each cluster, the system gathers approved KYC checks and
approved KYC-channel notes. It filters on `approved` *before* sorting by date, so a newer
rejected or expired check is never chosen, and the most recent approved source supplies
the legal name, date of birth and document country. If an equally recent approved source
disagrees, the system keeps that conflict and treats the field as unknown. It does not
guess. Declared CRM facts (names, `dob`, residence, nationality) stay available. When
there is no verified date of birth, declared dates that agree at different precision
merge into one date; dates that really disagree become unknown.

**Notes are parsed lazily**, only for the emails of candidate clusters, and cached per
email. KYC notes are read for status, name, date of birth, document country and number.
Status words for rejected, expired, pending and negated outcomes are checked before
approval words. Support notes yield residence countries only when they carry a
residence or work signal. A pure transfer or travel mention gives no country, and a
negated or third-party mention ("his brother lives in…") gives none either. Text that
looks relevant but cannot be read safely becomes a `PENDING_CLUE`.

## 4. Resolving the listed person

- **Names:** `name`, `best_matching_name`, the joined first, middle and last names, and
  every alias.
- **Countries** come from all five sources: `countries`, `nationality`, `citizenship`,
  `addresses` (the last part, with a trailing `[Tag]` stripped) and `place_of_birth`
  (`;`-separated). "International", "Europe" and ambiguous demonyms ("Korean",
  "Congolese") map to nothing. Territories are their own country (Puerto Rico ≠ United
  States, Jersey ≠ United Kingdom).
- **Date of birth cascade (individuals):**
  1. the `dobs` field;
  2. otherwise paragraph 1 of `profile_notes`: a birth date, an age at a date (an age
     *a* at year *Y* gives birth years {Y−a−1, Y−a}), or a key-value line such as
     `DOB: 6/20/1955`. The parsers work on cues (an age word next to a number, a date in
     the same sentence) rather than fixed templates;
  3. otherwise search results for the hit's name, looked up by a key of sorted tokens
     without initials, with repeated tokens kept. A result counts as being about the hit
     only if its snippet contains the exact listing phrase for the hit's
     `primary_category` (or `categories[0]`) **and** its title or snippet names a country
     tied to the hit.

  A company's incorporation date comes only from `dobs`.
- **Dual-mode slash dates:** bank-side dates are always read as `DD/MM/YYYY`. Hit-side
  dates use a part greater than 12 to settle the order. When both parts are 12 or less,
  the system keeps **both** readings, and a contradiction then requires every reading to
  contradict. Two-digit years are never parsed.
- **Dated reports** are exactly the lines `According to …; <Month D, YYYY>: …`. The
  earliest one is the reference date for "younger than 16" and "not yet incorporated".
  Both checks use worst-case calendar bounds, so a partial date never creates a
  contradiction.
- **Deceased** is read only from a non-null `deceased_date`. If that date is ambiguous,
  the system keeps its *latest* reading, so it concludes the person died before
  screening only when every reading says so.
- **P1 versus search-result guard:** sometimes paragraph 1 has a key-value DOB line and
  an attributed search result also gives birth information. The system then keeps both
  and compares the customer with each source separately. If both contradict the
  customer, the result is `DOB_MISMATCH`. If both agree, the dates agree. If the two
  sources disagree with each other about the customer, the date of birth is treated as
  unknown and the hit is `REVIEW`.

## 5. Evidence representation

Every customer or hit fact is a frozen `Fact` with:

- `field`, `value` and `raw`;
- a `status`: `ABSENT` (no information), `PENDING_CLUE` (relevant text that could not be
  read safely) or `VERIFIED` (parsed and validated);
- the `subject` the fact is about;
- its provenance: `source_type` (CUS / KYC / NOTE / SR / HIT), `source_id` and
  `location` (for example `verified_dob` or `note_body`);
- a `verified_state`: `approved` or `declared`.

Hit dates are `DateSpec`s: the possible years, the exact dates (with every slash reading),
the source ID and the origin (`hit_dobs`, `p1_prose`, `p1_kv` or `sr`).

Each rule outcome (`Finding`) carries only the IDs of the facts it used. **Evidence** is
the deduplicated, sorted union of the findings behind the decision:

- **A date of birth contradiction** cites the source of the customer's date (often a
  `KYC-*` or `NOTE-*` alone, with no `CUS-*`) plus any `SR-*` it relied on. A date read
  from the hit's own profile is not cited again, because the hit is already named by
  `hit_id`.
- **A location contradiction** cites the customer records and the sources of their
  countries.
- **A verified-name contradiction** cites the approved check or note.
- **A match** cites the cluster's records, the date source, the verified-name source and
  the sources of the shared countries.
- **A `REVIEW`** cites the sources of the shared countries, or else the first record.
- **`DECEASED` and `ENTITY_TYPE_MISMATCH`** are decided by the hit alone, so they cite
  no records.

## 6. Decision engine

`decision.decide_person` compares one candidate person with the hit and collects
**every** contradiction that applies. The first one in this order gives the reason code:

```text
ENTITY_TYPE_MISMATCH > DECEASED > TEMPORAL_IMPOSSIBILITY > DOB_MISMATCH
                     > LOCATION_MISMATCH > NAME_MISMATCH_VERIFIED
```

The reason sentence lists every contradiction that applies, up to four. With no
contradiction, rules apply in this order:

1. **P1 versus search-result split:** `REVIEW / INSUFFICIENT_DATA`.
2. **Name and date of birth agree but the countries are disjoint:**
   `REVIEW / INSUFFICIENT_DATA` (POLICY principle 6). `LOCATION_MISMATCH` never fires in
   that case.
3. **An individual whose name and date of birth agree:** `SAME_ENTITY`, as
   `NAME_DOB_LOCATION_MATCH` if a country is shared, else `NAME_DOB_MATCH`.
4. **A company whose name and country agree:** `SAME_ENTITY / NAME_LOCATION_MATCH`.
5. **Otherwise:** `REVIEW / INSUFFICIENT_DATA`.

A name only counts as decidable if an individual's name has at least two real parts
(placeholders such as `UNKNOWN` or `0000` are not parts). A company needs one.

**Pending-clue guard.** A `PENDING_CLUE` on either side blocks only the rules it could
overturn:

- **A date of birth clue** makes the DOB comparison unknown. It blocks `DOB_MISMATCH`,
  `TEMPORAL_IMPOSSIBILITY` and the match.
- **A name clue** blocks `NAME_MISMATCH_VERIFIED` and the match.
- **A country clue** blocks `LOCATION_MISMATCH` and the company match.
- **A location clearance is also blocked** while a pending clue could still make name
  and date of birth agree.

`ENTITY_TYPE_MISMATCH` and `DECEASED` never depend on the customer, so no pending clue
blocks them.

**Several people fit the alert.** The system decides the hit for each person. If all
reach the same decision, it gives that decision with `customer_record_id = "AMBIGUOUS"`.
The reason code is the most frequent one, with ties broken by a fixed priority, and the
evidence is the union. If the decisions differ, the result is
`REVIEW / AMBIGUOUS_CUSTOMER_RECORD`.

**No customer found.** `customer_record_id` is `null`. Only contradictions that do not
depend on the customer can clear the hit: a screened entity type that differs from the
hit's, or a death before `screened_at`. Every other case is `REVIEW / INSUFFICIENT_DATA`.

## 7. Determinism

- **Fixed order everywhere:** hits are sorted by ID, notes by `note_id`, search results
  by `result_id`, and clusters and evidence are sorted. Union-Find roots are the smallest
  ID, and ties between approved KYC sources break on `(date, source_id)`. Sets are always
  turned into sorted tuples before use.
- **No randomness and no wall-clock dates.** "Before screening" is measured against the
  alert's `screened_at`, never against today. The reason text is a template filled
  deterministically.
- **LLM calls** (when enabled) are pinned to `temperature=0`, `top_p=1` and `seed=42`,
  with `max_tokens=2048` and a JSON `response_format`. (The pinned model always reasons
  and reasoning cannot be switched off; with a 256-token budget it spent every token on
  hidden reasoning and returned empty content, so the budget leaves room for the answer.) They are cached in memory for one
  run, keyed by `SHA-256(model ‖ PROMPT_VERSION ‖ task ‖ context ‖ text)`, and the cache
  is never written to disk. Failures are cached too, so the same text gets the same
  answer within a run. Each run has hard caps: 200 calls, 600k tokens, 20 minutes of wall
  clock, and a 30 s timeout per request. HTTP 429 and 5xx responses are retried with
  bounded backoff that honours `Retry-After`.
- **Post-validation gates** (`llm_fallback.validate_extraction`):
  - the `evidence_quote` must appear verbatim in the input;
  - names, document numbers, countries and dates must be grounded in the text;
  - negated and third-party statements are rejected;
  - a KYC "approved" is rejected if the text has any failure cue;
  - the model never does birth-year arithmetic: code computes {Y−a−1, Y−a} from the
    literal age and reference year;
  - a search result must pass the same phrase-plus-country attribution test in code.
- **Degradation:** a missing key, a timeout, an exhausted retry budget, invalid JSON, a
  failed validation or a reached cap returns `PENDING_CLUE`, never a guessed fact.
- **Without a key**, the run uses `NullLLMFallback`. Every clue that needed the model
  stays pending. The run is then fully deterministic, and the pending clues push the
  affected hits toward `REVIEW`, the safe direction.

## 8. Model and prompts

- **Model:** `baidu/ernie-5.5-flash-preview:free` (`llm_fallback.DEFAULT_MODEL`), which
  `OPENROUTER_MODEL` can override. Calls go only to
  `https://openrouter.ai/api/v1/chat/completions`.
- **Prompt version:** `PROMPT_VERSION = "v2"`. The version is part of the cache key and
  is reported in the run stats.
- **System prompt:** a strict extractor. It reports only facts stated in the text, never
  guesses, never subtracts to get a birth year, includes a verbatim `evidence_quote` for
  every claim, and returns only JSON.

| Task | Called when | Returns |
|---|---|---|
| `kyc_note` | a KYC-channel note's status is unknown, or it is approved but missing a name, DOB or country | status, verified name, DOB, document country and number, checked_at, quote |
| `residence_note` | a support note names a country but the residence meaning is unclear | `lived_countries[]`, each with a subject (customer / third_party / unknown) and a quote |
| `hit_p1_dob` | an individual hit has no `dobs` and paragraph 1 has a birth or age cue the local parser could not read | birth_date or age + reference_date, subject, quote |
| `sr_facts` | an attributed search snippet has a birth or age cue the local parser could not read | age or birth_date, reference_date, country, subject, quote |

The model can only fill a gap. It cannot overturn a locally detected rejected or expired
status, and only facts that pass validation enter the decision engine.

## 9. Time and cost

Measured on a laptop (Apple Silicon, Python 3.9), dev set, `practice_labels.jsonl` removed:

| Metric | Value |
|---|---|
| Deterministic runtime (`--no-llm`, dev, 819 hits) | 6–8 s per run (5 runs) |
| Live-LLM runtime (dev) | ~9 s |
| LLM calls on dev (made / cache hits) | 0 / 0: every dev free-text clue is read by the local parsers, so the fallback never fires |
| Typical single fallback call (synthetic paraphrases) | 5–12 s, ~450–900 tokens (mostly hidden reasoning) |
| Worst case (all caps reached) | 200 calls, ≤ 600k tokens, ≤ 20 min wall clock |
| Cost (USD) | 0 (free-tier model) |

On the unseen set, paraphrased wording may trigger the fallback. Every call is bounded by
the caps above, so a run stays well under the 30-minute limit.

## 10. Three worked cases

### Clear mismatch: `H-D861DA`

The alert links to a joint account, and the screened name and date of birth select one
holder. That holder's date of birth (1979) comes from an approved onboarding note. The
hit has no `dobs`; paragraph 1 gives only an age (46 at a January 2023 interview), so
the hit was born in 1976 or 1977. The customer's year is more than one year outside
both, so the result is `DOB_MISMATCH`. *(Alternative: in `H-622EDD` the hit's birth
years come from an attributed search result instead.)*

System output:

```json
{"hit_id": "H-D861DA", "decision": "DIFFERENT_ENTITY", "reason_code": "DOB_MISMATCH",
 "customer_record_id": "CUS-804632", "evidence": ["NOTE-8548C1"],
 "reason": "The customer's date of birth is 1979-11-08 (NOTE-8548C1) but the hit's profile notes put its birth year at 1976 or 1977."}
```

### Match: `H-0181BD`

The alert links to a joint account; the screened details select one holder, and a phone
plus date of birth link joins a second record to that holder. The hit (a PEP with no
`dobs`) gets its birth years (1985 or 1986) from an attributed search result. The
customer's date of birth from an approved note agrees with them, both sides are tied to
Brazil, and the name agrees: `NAME_DOB_LOCATION_MATCH`.

System output:

```json
{"hit_id": "H-0181BD", "decision": "SAME_ENTITY", "reason_code": "NAME_DOB_LOCATION_MATCH",
 "customer_record_id": "CUS-138588", "evidence": ["CUS-138588", "CUS-775419", "NOTE-CB2CB8", "SR-590704"],
 "reason": "The customer's date of birth 1985-09-12 (NOTE-CB2CB8) agrees with the hit's 1985 or 1986. Both the customer and the hit are linked to Brazil. The customer's name agrees with the hit's name COSTA, MARCOS ROBERTO."}
```

### Review: `H-6C7985`

The alert has no link, and the linkless search finds two different people who fit. The
hit is an adverse-media profile tied to Pakistan with no date of birth anywhere. For
each person the name and country agree, but there is no date of birth to compare and no
contradiction. Both people reach `REVIEW / INSUFFICIENT_DATA`, so the hit stays
`REVIEW` with `customer_record_id = "AMBIGUOUS"`.

System output:

```json
{"hit_id": "H-6C7985", "decision": "REVIEW", "reason_code": "INSUFFICIENT_DATA",
 "customer_record_id": "AMBIGUOUS", "evidence": ["CUS-618916", "CUS-885413", "KYC-8DE62A", "NOTE-BC6437"],
 "reason": "Both the customer and the hit are linked to Pakistan. The customer's name agrees with the hit's name Mohammad Nadeem. Nothing in the customer's records contradicts the hit, and the date of birth cannot be compared with it, so the hit can be neither cleared nor confirmed."}
```

## 11. Known limitations

- **Free-text coverage depends on cues.** Wording that has no age, birth or residence
  cue at all is invisible to both the local parsers and the fallback, and becomes
  `ABSENT` rather than `PENDING_CLUE`. A clue the parsers miss entirely cannot block a
  clearance.
- **Without an API key**, more hits are left as `REVIEW` than strictly necessary,
  because pending clues block clearances. This is safe, but it costs analyst time.
- **Free-tier LLM availability can vary between runs.** The order of calls and the cache
  are deterministic, but a 429 or timeout in one run and not another can change which
  clues are resolved. The caps (200 calls, 600k tokens, 20 min) can also leave late
  clues pending. Running with `--no-llm` is the fully reproducible mode.
- **A link is filtered by the screened details.** If the screened name, date of birth,
  country or city disagree with every linked record, the hit gets no customer
  (`null` → `REVIEW`) rather than a guessed one.
- **Search results are looked up by the hit's primary `name` only**, not by its
  aliases.
- **Dates and countries:** ambiguous demonyms and regions are unknown by design, so they
  can never clear a hit. Two-digit years are not parsed.
- **Evidence:** a `DIFFERENT_ENTITY` hit cites the union of every contradiction found,
  which can be more than the single deciding record.
- **`match_score` is ignored** on purpose. Identity is decided only from the facts.
