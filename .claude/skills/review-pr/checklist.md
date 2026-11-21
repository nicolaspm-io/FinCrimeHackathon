# Review checklist: where implementations of POLICY.md go wrong

Each item gives the trap, **the direction a failure pushes the decision**, and how to probe it in the data. The directions are:
- **UNSAFE:** a wrong clearance costs 60.
- **COSTLY:** an extra review or a wrong confirm costs 2–3.
- **EITHER:** it can go both ways.

Counts are from `dev/` as of this skill's creation. Regenerate them with `scripts/data_atlas.py` before you rely on them.

The rule above all rules: **unknown stays unknown.** Wherever input can't be parsed, is missing, or is ambiguous, the code must make the fact unknown. It must never treat it as a mismatch. Trace every `except`, every `return None`, every default value and every LLM failure path to the decision it ends up in.

---

## P2 — Resolving the customer

| Trap | Direction | Probe |
|---|---|---|
| Joint accounts: `account_ref` can lead to 2 holders (58 alerts). Taking the first or only holder, instead of picking with the screened name and DOB | UNSAFE (clears on someone else's facts) or COSTLY | For alerts with `account_ref→2 holders`, compare the PR's `customer_record_id` with the holder whose name and DOB fit the screened ones |
| A link that leads to several people, where the screened details don't pick one, is treated as "no customer" or as the first one, instead of a candidate search | EITHER | Same list. Check what the PR does when both holders fit, or neither does |
| No-link alerts (146): exact-name lookup misses nicknames and typos. A fuzzy lookup that is too loose picks a namesake. Requiring every screened field instead of "equal in every part both give" | UNSAFE if a namesake is picked; COSTLY if nobody is found | For every no-link alert, list the PR's chosen record next to the `hit_dossier.py` loose candidates. Re-check the name, DOB parts, country (residence **or** nationality) and city (in city **or** address) |
| Screened DOB partial (34 year-only, 2 year-month) compared as a full date or a string | EITHER | Alerts whose `dob` is `9999` or `9999-99` |
| Using another customer's records: a joint account's onboarding or KYC belongs to the *other* holder; notes are filed by *email*, not by account | UNSAFE | For each resolved customer, check that every cited KYC or NOTE id belongs to that person's own onboarding rows or email |
| Login handle looked up in `customers.login_name` (empty in dev) instead of `accounts.login_handle` | COSTLY (finds no one) | 70 alerts with a `login_handle` |
| No record found → the code still clears on the screened facts alone, without a POLICY-grounded contradiction | UNSAFE | Rows with `customer_record_id: null` and decision `DIFFERENT_ENTITY` |

## P3 — One person, one picture

| Trap | Direction | Probe |
|---|---|---|
| Same phone + same full DOB ⇒ same person (96 two-record pairs). Comparing DOBs **as strings** misses `1985-09-12` = `September 12, 1985` | UNSAFE (the countries of the other record are lost, so a LOCATION clearance is made on half the picture) | Shared-phone groups in the atlas. Check that the PR merged the pairs whose parsed DOBs are equal |
| Merging on a shared phone **or address alone** (families: 48 pairs with different DOBs) | EITHER (foreign facts get in) | The same groups with different DOBs. They must **not** be merged |
| Year-only DOB treated as a "same full DOB" | EITHER | Records sharing a phone where one DOB is partial |
| Document number only on approved checks (7 shared numbers in dev). Using rejected or expired checks, or forgetting the link is transitive | EITHER | Atlas: shared document numbers |
| Several people fit → the code decides once (first match) instead of per candidate. Or it gives AMBIGUOUS_CUSTOMER_RECORD when all candidates lead to the same decision | UNSAFE / COSTLY | Rows with `customer_record_id: AMBIGUOUS`. Re-decide each candidate |

## P4 — Verified beats declared

| Trap | Direction | Probe |
|---|---|---|
| "Latest check" taken regardless of result: `approved→rejected` (831 applicants) and `approved→expired` (853) need the *earlier approved* check | EITHER | Applicants with those sequences, and the PR's chosen check |
| KYC recorded **only in a note** (22,575 kyc-channel notes, in about 15k distinct wordings) is ignored, or parsed with regexes keyed to a few templates | COSTLY in dev, but in the unseen set (new wording) the verified name or DOB is silently lost | Run the PR's note parser over **all** kyc-channel notes and count how many give a name, DOB and country. Then feed it paraphrases |
| A note that reports a **failed** or expired verification counted as verified. Dev has 0 such notes, but POLICY allows them, so the unseen set may have them | UNSAFE (a wrong verified name gives NAME_MISMATCH_VERIFIED) | Feed the parser "verification failed for X, DOB …" or "document expired …" |
| "Most recent" between a provider check and a note-recorded check compared by the wrong date, or by a string date | EITHER | Customers with both |
| NAME_MISMATCH_VERIFIED made from the record name or the screened name (they can be nicknames or typos) instead of the verified legal name | **UNSAFE** | Every NAME_MISMATCH_VERIFIED row must cite an approved KYC check or a kyc note |
| Verified DOB ignored in favour of the record's DOB | EITHER | Customers whose record DOB and approved-check DOB differ |

## P5 — The hit's facts

| Trap | Direction | Probe |
|---|---|---|
| Hit `dobs`: 240 year-only, 50 full, 3 `YYYY-MM`. A hit can list several (`["1996","1997"]`); a clearance needs **every** one contradicted | UNSAFE | Hits with more than one DOB value |
| Age at a date gives **two** birth years ("aged 45 in March 2019" → 1973 or 1974). Using only one | UNSAFE (a false DOB_MISMATCH at the ±1 boundary) | The atlas's age sentences: compute both years and compare with the PR |
| Age sentences only count from the **first paragraph** of the profile notes (paragraphs are split by a blank line). About 24 hits have one there, in many wordings ("aged 30, in …", "reached the age of 38", "a 46-year-old", "Age: 52" under an "information as of <date>:" header). 2 hits have "Age:" only in a later paragraph (H-666325, H-FB1E2D) | EITHER | Run the PR's age extractor on every first paragraph and on paraphrases. Check the later-paragraph cases are ignored when `dobs` is empty |
| Search results used when `dobs` or the profile already give a DOB. Or used **without** the filter: listing phrase of the primary (else first) category **and** a country tied to the hit. 10,203 of 16,882 snippets carry no listing phrase; most are namesakes | **UNSAFE** (a namesake's age clears a real match) | For every hit whose DOB came from search results, check the cited SR's phrase and country by hand |
| Category → phrase mapping wrong for `Registration`, `Other Segment` and similar ("named on a watchlist"), or primary category ignored | EITHER | Atlas: the category used for the listing phrase |
| Countries tied to the hit: `addresses` last part **carries a `[tag]`** (1,191 of 1,535: `Spain [Business]`). `nationality` can hold a whole place (`Ribeirao Pires, Sao Paulo, Brazil`). `place_of_birth` is `City, Region, Country`. `International` and `Europe` are not countries | **UNSAFE** (missing hit countries make LOCATION_MISMATCH too easy) or COSTLY | Atlas: country shapes. Compare the PR's hit-country set with the raw fields |
| Dated reports: only lines exactly `According to …; Month D, YYYY:` count (639 in dev). 168 near-miss lines (`; June 2014:`, `; 2019:`, `; as of …:`, no date) must **not** count. Use the **earliest** dated report | UNSAFE (a near-miss or a wrong date gives a false TEMPORAL_IMPOSSIBILITY) | Atlas section 8. Run the PR's regex on the near-miss lines |
| Company incorporation date taken from anywhere other than the hit's `dobs` | UNSAFE | Company hits with no `dobs` cleared on time or DOB |

## P6 — Clear only on a contradiction

| Rule | Traps | Direction |
|---|---|---|
| Time | Must be "younger than 16 at the earliest dated report": exactly 16 is **not** younger. With a year-only customer DOB, the customer must be under 16 for every possible birth date. Death date partial (`2022-03`) compared with `screened_at` | UNSAFE |
| DOB | "More than 1 year outside": a one-year gap is **not** a mismatch. The month/day contradiction applies only when both give a month **in the same year**. One agreeing hit DOB blocks the clearance | UNSAFE |
| Location | Customer countries = residence and nationality on **all** of the person's records, plus the approved document country, plus the countries in notes on where they lived or worked (about 3k such notes, many wordings). Codes, names and demonyms normalised (`GB`, `United Kingdom`, `British`). Territories are their own country (`Northern Mariana Islands` ≠ `United States`). If **name and DOB agree**, a country difference gives REVIEW, not a clearance. An empty country set on either side is **no** contradiction | **UNSAFE**: the biggest clearance code in dev |
| Name | Only the verified legal name (P4). Placeholder-only names can't clear | UNSAFE |
| Type | Person vs company. Watch customers or hits whose `entity_type` is missing or unusual | UNSAFE |

## P7 — Confirm only on agreement

- A DOB agrees when the customer's year is one of the hit's possible years and every part both give is equal. A year-only value agrees with a full date. Checking only the year, when both give months, is a **wrong confirm (COSTLY)**.
- A person needs name and DOB to agree. A company needs name and country. Confirming a person on name and country alone is a wrong confirm.
- Reason code: `NAME_DOB_LOCATION_MATCH` when the country also agrees, else `NAME_DOB_MATCH`. `NAME_LOCATION_MATCH` is for companies only.

## P8 — Names

| Trap | Direction |
|---|---|
| Edit thresholds: 4–6 letters ≤1 edit, 7+ letters ≤2, under 4 letters identical, **the shorter part's length decides**, a swap of two letters = 2 edits. So Damerau distance, `difflib` ratios and Jaro-Winkler are all **wrong** | EITHER; too strict gives UNSAFE NAME_MISMATCH |
| Agreement = at least two parts match, **or** every part of the shorter name matches. Requiring all parts of both names is too strict | UNSAFE (a false name mismatch) |
| Joined/split parts (Abdulsalam = Abdul Salam, counted as one part). Variant groups in `name_variants.json`. Brackets (`(AKA)`). Accents. Comma order (`Costa, Marcos`). Initials | UNSAFE if missing |
| Aliases count, nicknames included. Aliases in non-Latin script exist (`বাদশা মিয়া`) | UNSAFE if aliases are ignored |
| Placeholders (`N/a`, `0000`, `Xxx`, `Test`, `NA`: about 530 occurrences in records, KYC and screened names). An individual with fewer than two real parts can be neither confirmed nor cleared on name. **`na` is also a company legal form** in `name_variants.json` | UNSAFE (a placeholder-only name "mismatches" everything) |
| Company legal forms dropped only for companies, and from both sides | EITHER |

## P9 and the output

- Evidence = only the records that decided it. Dumping every record looked at loses reason points. Every id must exist in the folder.
- `customer_record_id`: `AMBIGUOUS` when more than one person fits, `null` when none does.
- The reason states this hit's own facts (dates, countries, names), not boilerplate.

## Engineering traps that become data bugs

- **Overfitting to dev wording:** regexes that match the top templates of notes, search snippets or profile sentences. Dev has about 15k distinct kyc-note templates and about 12.5k snippet templates, and the unseen set rewords free text. Only the dated-report form and the listing phrases are guaranteed stable. Probe with paraphrases.
- **Leakage:** reading `practice_labels.jsonl` at runtime, hard-coded hit, customer or note ids, hard-coded `dev/` paths, lookup tables built from dev answers.
- **LLM use:**
  - non-zero temperature, or no cache, breaks reproducibility
  - the model invents a DOB or country
  - the prompt includes every search result, so the model picks a namesake
  - an API failure or timeout must end in REVIEW. A default, a crash or an empty answer must never end in DIFFERENT
  - no validation that the model's output matches the schema
- **Determinism:** iteration over a `set`, `dict` order taken from unordered sources, time-dependent logic (`today` instead of `screened_at`).
- **Performance:** 72k customers. An O(alerts × customers × edit distance) search for no-link alerts can blow the 30-minute limit on 550 unseen hits.
- **Partial output:** hits missing from the output count as REVIEW. The run must also survive a malformed line or an unexpected null in any field.
