#!/usr/bin/env python3
"""Inventory of the variety in a data folder — the inputs a PR's parsers and rules must cope with.

    python3 data_atlas.py <data_dir> [--out atlas.md] [--examples 3] [--top 25]

Every section lists the distinct *shapes* or *templates* a field takes, with counts and
example record IDs, so a reviewer can check a PR's handling against the real spread of
inputs instead of the few cases in the practice labels. Templates mask names, dates,
numbers, countries and providers so different wordings of the same fact group together;
the long tail of templates is where parsers usually break, and the unseen set rewords
free text further.
"""

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import MONTHS, date_shape, hit_names, index, load, name_parts, parse_date, short  # noqa: E402

PLACEHOLDER = re.compile(r"^(na|n/a|n\.a\.|test|unknown|xxx+|none|null|nil|tbc|tbd|-+|\?+|\d+)$", re.I)
LISTING_PHRASES = {
    "Sanctions": "a sanctioned individual", "PEP": "a politically exposed person",
    "Enforcement": "the subject of enforcement action", "Adverse Media": "named in adverse media",
    "SOE": "a state-owned enterprise official", "other": "named on a watchlist",
}
DATED_REPORT = re.compile(r"^According to .+?; ((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}):")


class Table:
    def __init__(self, examples):
        self.count = collections.Counter()
        self.ex = collections.defaultdict(list)
        self.k = examples

    def add(self, key, example):
        self.count[key] += 1
        if len(self.ex[key]) < self.k and example not in self.ex[key]:
            self.ex[key].append(example)

    def md(self, title, top, note=""):
        lines = [f"### {title}", ""]
        if note:
            lines += [note, ""]
        total = sum(self.count.values())
        lines.append(f"{len(self.count)} distinct, {total} values.")
        lines.append("")
        lines.append("| count | value / template | examples |")
        lines.append("|---:|---|---|")
        for key, n in self.count.most_common(top):
            k = str(key).replace("|", "\\|").replace("\n", " ⏎ ")
            ex = "; ".join(str(e).replace("|", "\\|") for e in self.ex[key])
            lines.append(f"| {n} | `{short(k, 160)}` | {short(ex, 220)} |")
        rest = len(self.count) - top
        if rest > 0:
            tail = sum(n for _, n in self.count.most_common()[top:])
            lines.append(f"| {tail} | …{rest} more rare values (the long tail — check these) | |")
        lines.append("")
        return lines


class Masker:
    """Mask names/dates/numbers/countries/providers so wordings group into templates."""

    def __init__(self, countries, providers):
        words = sorted({c for c in countries if c and len(c) > 2}, key=len, reverse=True)
        self.country_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None
        prov = sorted({p for p in providers if p}, key=len, reverse=True)
        self.provider_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in prov) + r")\b") if prov else None
        month = "|".join(sorted((m for m in MONTHS if len(m) > 3 or m in ("may",)), key=len, reverse=True))
        self.date_res = [
            re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"), re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
            re.compile(rf"\b(?:{month})\.? \d{{1,2}},? \d{{4}}\b", re.I), re.compile(rf"\b\d{{1,2}} (?:{month})\.?,? \d{{4}}\b", re.I),
        ]
        self.monthyear = re.compile(rf"\b(?:{month})\.?,? \d{{4}}\b", re.I)
        self.month = re.compile(rf"\b(?:{month})\b", re.I)

    KEEP_CAPS = {"DOB", "KYC", "PEP", "SOE", "ID", "USA", "UK", "US", "EU", "UN", "SAM", "OFAC", "AKA", "LLC", "LTD"}

    def __call__(self, text, names=()):
        # tags are lowercase in braces so later masks never re-match them
        t = text
        for n in sorted({p for n in names for p in re.split(r"[\s,]+", n or "") if len(p) > 1}, key=len, reverse=True):
            t = re.sub(r"(?<![\w])" + re.escape(n) + r"(?![\w])", "{name}", t, flags=re.I)
        for r in self.date_res:
            t = r.sub("{date}", t)
        t = self.monthyear.sub("{month year}", t)
        t = self.month.sub("{month}", t)
        if self.provider_re:
            t = self.provider_re.sub("{provider}", t)
        if self.country_re:
            t = self.country_re.sub("{country}", t)
        t = re.sub(r"\b(1[89]|20)\d{2}\b", "{year}", t)
        t = re.sub(r"\d+", "{n}", t)
        t = re.sub(r"\b[A-Z][A-Z'\-]{2,}\b", lambda m: m.group(0) if m.group(0) in self.KEEP_CAPS else "{CAPS}", t)
        t = re.sub(r"\{(?:name|CAPS)\}(?:[ ,]+\{(?:name|CAPS)\})+", "{name}", t)
        return t


def country_shape(v):
    if v is None:
        return "<null>"
    s = v.strip()
    if re.fullmatch(r"[A-Z]{2}", s):
        return "ISO-2 code"
    if re.fullmatch(r"[A-Z]{3}", s):
        return "ISO-3 code"
    if re.search(r"\[.*\]", s):
        return "with [tag]"
    if "," in s:
        return "comma list / address-like"
    if re.search(r"(ian|ese|ish|ean|ani|ic|i|ch|ss)$", s) and " " not in s:
        return "demonym-like (Nigerian, British…)"
    return "name"


def build(data_dir, examples, top):
    d = load(data_dir)
    alerts, hits, customers = d["alerts"], d["hits"], d["customers"]
    accounts, onboarding, checks, notes, srs = d["accounts"], d["onboarding"], d["kyc_checks"], d["notes"], d["search_results"]
    md = [f"# Data atlas: `{data_dir}`", "",
          "Generated by `.claude/skills/review-pr/scripts/data_atlas.py`. Counts are over the whole folder. "
          "Use it to check that a PR's parsers and rules cover the real spread of inputs, then probe the long tails yourself.", ""]

    # ------------------------------------------------------------ sizes and nulls
    md += ["## 1. Files and field coverage", ""]
    for name in ("alerts", "hits", "customers", "accounts", "onboarding", "kyc_checks", "notes", "search_results"):
        rows = d[name]
        if not rows:
            md.append(f"- **{name}**: missing or empty")
            continue
        keys = list(rows[0].keys())
        filled = {k: sum(1 for r in rows if r.get(k) not in (None, "", [], {})) for k in keys}
        md.append(f"- **{name}** ({len(rows)} rows): " + ", ".join(f"{k} {100 * v // len(rows)}%" for k, v in filled.items()))
    md.append("")
    t = Table(examples)
    for h in hits:
        present = tuple(k for k in ("dobs", "profile_notes", "countries", "nationality", "citizenship", "addresses", "place_of_birth", "aliases") if h.get(k))
        t.add(present or ("nothing beyond name",), h["hit_id"])
    md += t.md("Hit fact availability (which fact-bearing fields a hit has)", top,
               "Hits without `dobs` must get a DOB from profile notes or search results, or stay unknown.")

    # ------------------------------------------------------------ dates
    md += ["## 2. Date formats by source", "",
           "A parser that misses a shape turns a known date into unknown (safe) — or, worse, into a wrong date (unsafe). "
           "Slash dates in the bank's systems: check day-first vs month-first below.", ""]
    sources = [
        ("alerts.customer.dob", [(a["customer"].get("dob"), a["alert_id"]) for a in alerts]),
        ("alerts.screened_at", [(a.get("screened_at"), a["alert_id"]) for a in alerts]),
        ("customers.dob", [(c.get("dob"), c["record_id"]) for c in customers]),
        ("kyc_checks.verified_dob", [(k.get("verified_dob"), k["check_id"]) for k in checks]),
        ("kyc_checks.checked_at", [(k.get("checked_at"), k["check_id"]) for k in checks]),
        ("hits.dobs[]", [(x, h["hit_id"]) for h in hits for x in (h.get("dobs") or [])]),
        ("hits.deceased_date", [(h.get("deceased_date"), h["hit_id"]) for h in hits if h.get("deceased_date") or h.get("life_status") == "Deceased"]),
    ]
    for title, values in sources:
        t = Table(examples)
        unparsed = 0
        for v, rid in values:
            t.add(date_shape(v), f"{rid}={v!r}")
            if v is not None and parse_date(v) is None:
                unparsed += 1
        md += t.md(title, top, f"{unparsed} non-null values the review helper itself cannot parse.")
    slash = [c.get("dob") for c in customers if c.get("dob") and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", c["dob"])]
    slash += [k.get("verified_dob") for k in checks if k.get("verified_dob") and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", k["verified_dob"])]
    md += [f"Slash dates: {len(slash)}; first part > 12 in {sum(1 for s in slash if int(s.split('/')[0]) > 12)}, "
           f"second part > 12 in {sum(1 for s in slash if int(s.split('/')[1]) > 12)} (0 second-part > 12 means day-first is consistent).", ""]

    # ------------------------------------------------------------ countries
    md += ["## 3. Country forms", "",
           "Location rules compare countries across sources. Codes, names, demonyms and address tails must all map to one country. "
           "`International`/`Europe` are not countries; territories are their own country.", ""]
    csources = [
        ("customers.nationality", [(c.get("nationality"), c["record_id"]) for c in customers]),
        ("customers.residence_country", [(c.get("residence_country"), c["record_id"]) for c in customers]),
        ("kyc_checks.document_country", [(k.get("document_country"), k["check_id"]) for k in checks]),
        ("alerts.customer.country", [(a["customer"].get("country"), a["alert_id"]) for a in alerts]),
        ("hits.countries[]", [(x, h["hit_id"]) for h in hits for x in h.get("countries") or []]),
        ("hits.nationality[]", [(x, h["hit_id"]) for h in hits for x in h.get("nationality") or []]),
        ("hits.citizenship[]", [(x, h["hit_id"]) for h in hits for x in h.get("citizenship") or []]),
        ("hits.addresses[] last part", [(x.split(",")[-1].strip(), h["hit_id"]) for h in hits for x in h.get("addresses") or []]),
        ("hits.place_of_birth", [(h.get("place_of_birth"), h["hit_id"]) for h in hits if h.get("place_of_birth")]),
    ]
    for title, values in csources:
        shapes, vals = Table(examples), Table(examples)
        for v, rid in values:
            shapes.add(country_shape(v), f"{rid}={v!r}")
            vals.add(v, rid)
        md += shapes.md(f"{title} — shapes", top)
        md += vals.md(f"{title} — values", top)

    # ------------------------------------------------------------ names
    md += ["## 4. Names", ""]
    legal = set(d["name_variants"].get("company_legal_forms", []))
    t = Table(examples)
    for c in customers:
        for field in ("full_name", "preferred_name"):
            for p in re.split(r"\s+", c.get(field) or ""):
                if p and PLACEHOLDER.match(p.strip(".,")):
                    t.add(f"{field}: {p}", c["record_id"])
    for a in alerts:
        for p in re.split(r"\s+", a["customer"].get("name") or ""):
            if p and PLACEHOLDER.match(p.strip(".,")):
                t.add(f"alert screened name: {p}", a["alert_id"])
    for k in checks:
        for p in re.split(r"\s+", k.get("verified_name") or ""):
            if p and PLACEHOLDER.match(p.strip(".,")):
                t.add(f"kyc verified_name: {p}", k["check_id"])
    md += t.md("Placeholder-like name parts (not name parts under POLICY 8)", top)
    t = Table(examples)
    for c in customers:
        if c["entity_type"] != "Individual":
            continue
        real = [p for p in name_parts(c.get("full_name")) if not PLACEHOLDER.match(p)]
        if len(real) < 2:
            t.add(f"{len(real)} real part(s)", f"{c['record_id']}={c.get('full_name')!r}")
    md += t.md("Individual customers whose full_name has fewer than two real parts", top)
    t = Table(examples)
    for h in hits:
        for n in hit_names(h):
            feats = []
            if re.search(r"[\(\[]", n):
                feats.append("brackets")
            if "," in n:
                feats.append("comma (Last, First)")
            if re.search(r"\b[A-Za-z]\.?(\s|$)", n):
                feats.append("initial")
            if re.search(r"[^\x00-\x7f]", n):
                feats.append("non-ASCII")
            if re.search(r"[^\u0000-\u024f\s.,'()\-\[\]]", n):
                feats.append("non-Latin script")
            if len(name_parts(n)) == 1:
                feats.append("single part")
            if h["entity_type"] == "Company" and any(p in legal for p in name_parts(n)):
                feats.append("company legal form")
            t.add(", ".join(feats) or "plain", f"{h['hit_id']}={n!r}")
    md += t.md("Hit name/alias features", top)

    # ------------------------------------------------------------ linking
    md += ["## 5. Linking the customer", ""]
    acc_by_id = index(accounts, "account_id")
    acc_by_login = index(accounts, "login_handle")
    cust_email = index([dict(c, email=(c.get("email") or "").lower()) for c in customers], "email")
    t = Table(examples)
    for a in alerts:
        kinds = []
        if a.get("account_ref"):
            n = len(acc_by_id.get(a["account_ref"], []))
            kinds.append(f"account_ref→{n} holder(s)" if n else "account_ref→NOT FOUND")
        if a.get("login_handle"):
            n = len(acc_by_login.get(a["login_handle"], []))
            kinds.append(f"login_handle→{n} account(s)" if n else "login_handle→NOT FOUND in accounts")
        if a.get("email"):
            n = len(cust_email.get(a["email"].lower(), []))
            kinds.append(f"email→{n} record(s)" if n else "email→NOT FOUND")
        t.add(" + ".join(kinds) or "NO LINK (resolve by screened name/dob/country/city)", a["alert_id"])
    md += t.md("Alert link types", top)
    t = Table(examples)
    for a in alerts:
        s = a["customer"]
        t.add(" ".join(f"{k}={'Y' if s.get(k) else '-'}" for k in ("dob", "country", "city")) + f" [{s.get('entity_type')}]", a["alert_id"])
    md += t.md("What the screened customer carries", top)
    by_phone = collections.defaultdict(list)
    for c in customers:
        if c.get("phone"):
            by_phone[c["phone"]].append(c)
    t = Table(examples)
    for ph, group in by_phone.items():
        if len(group) < 2:
            continue
        dobs = collections.Counter(parse_date(c.get("dob")) for c in group)
        same = max(dobs.values())
        t.add(f"{len(group)} records share a phone; {same} with the same parsed dob", ",".join(c["record_id"] for c in group[:4]))
    md += t.md("Shared phones (same person only with the same full DOB — POLICY 3)", top)
    onb_by_app = index(onboarding, "kyc_applicant_id")
    by_doc = collections.defaultdict(set)
    for k in checks:
        if k.get("document_number") and k.get("result") == "approved":
            for o in onb_by_app.get(k["applicant_id"], []):
                by_doc[k["document_number"]].add(o["customer_id"])
    t = Table(examples)
    for doc, cs in by_doc.items():
        if len(cs) > 1:
            t.add(f"{len(cs)} customer records share an approved document number", f"{doc}: {','.join(sorted(cs)[:4])}")
    md += t.md("Shared document numbers on approved checks (same person — POLICY 3)", top)
    per_cust = collections.Counter(a["customer_id"] for a in accounts)
    md += [f"Accounts per customer: {dict(sorted(collections.Counter(per_cust.values()).items()))}. "
           f"Joint accounts (account_id with >1 holder): {sum(1 for v in acc_by_id.values() if len(v) > 1)}.", ""]

    # ------------------------------------------------------------ KYC
    md += ["## 6. Identity checks", ""]
    by_app = index(checks, "applicant_id")
    t = Table(examples)
    for app, ks in by_app.items():
        ks = sorted(ks, key=lambda k: k.get("checked_at") or "")
        seq = "→".join(k["result"] for k in ks)
        t.add(seq, app)
    md += t.md("Result sequences per applicant (oldest → newest)", top,
               "The most recent *approved* check is the verified identity; rejected/expired are not evidence.")
    t = Table(examples)
    for app, ks in by_app.items():
        appr = sorted([k for k in ks if k["result"] == "approved"], key=lambda k: k.get("checked_at") or "")
        if len(appr) > 1:
            first, last = appr[0], appr[-1]
            diffs = [f for f in ("verified_name", "verified_dob", "document_country", "document_number") if first.get(f) != last.get(f)]
            t.add("approved checks differ in: " + (", ".join(diffs) or "nothing"), f"{app}: {first['check_id']}→{last['check_id']}")
    md += t.md("Applicants with several approved checks — does the latest differ?", top)
    t = Table(examples)
    for k in checks:
        t.add(f"{k['provider']} / {k['document_type']} / {k['result']}", k["check_id"])
    md += t.md("Provider / document / result", top)

    # ------------------------------------------------------------ notes
    countries = {c.get("nationality") for c in customers} | {c.get("residence_country") for c in customers} | {k.get("document_country") for k in checks}
    countries |= {x for h in hits for x in (h.get("countries") or []) + (h.get("nationality") or []) + (h.get("citizenship") or [])}
    mask = Masker(countries, {k.get("provider") for k in checks} | {"Onfido", "Sumsub", "Veriff", "Persona", "Jumio", "Trulioo"})
    cust_by_email = {(c.get("email") or "").lower(): c for c in customers}
    md += ["## 7. Notes (support tool)", "",
           "Templates with names, dates, numbers, countries and providers masked. KYC-in-notes and residence-history wording vary; "
           "the unseen set words them differently again, so a parser keyed to these exact phrasings will miss facts.", ""]
    by_channel = collections.defaultdict(lambda: Table(examples))
    for n in notes:
        c = cust_by_email.get((n.get("requester_email") or "").lower(), {})
        names = [c.get("full_name"), c.get("preferred_name")]
        by_channel[n.get("channel")].add(mask(n.get("text") or "", names), n["note_id"])
    for ch, t in sorted(by_channel.items(), key=lambda x: -sum(x[1].count.values())):
        md += t.md(f"channel `{ch}` templates", top)
    t = Table(examples)
    for n in notes:
        text = n.get("text") or ""
        if re.search(r"verif|identit|passport|licen|national id|document|kyc|check", text, re.I) and \
                re.search(r"reject|fail|unsuccess|could not|unable|expired|mismatch|not (?:be )?verif|denied|invalid|declin", text, re.I):
            c = cust_by_email.get((n.get("requester_email") or "").lower(), {})
            t.add(mask(text, [c.get("full_name")]), n["note_id"])
    md += t.md("Notes that read as a FAILED / rejected / expired verification (must not count as verified)", top,
               "If this is empty, dev has none — but POLICY 4 covers checks 'recorded only in a note', so the unseen set may. "
               "A PR whose note parser treats any identity sentence as approved is a latent unsafe-clearance bug.")
    t = Table(examples)
    for n in notes:
        text = n.get("text") or ""
        if re.search(r"resid|liv(?:ed|ing)|moved|relocat|based in|home in|employ|work", text, re.I) and re.search(r"\b(1[89]|20)\d{2}\b", text):
            c = cust_by_email.get((n.get("requester_email") or "").lower(), {})
            t.add(mask(text, [c.get("full_name")]), n["note_id"])
    md += t.md("Notes about places the customer lived/worked (count as countries tied to the customer — POLICY 6)", top)

    # ------------------------------------------------------------ hit profile notes
    md += ["## 8. Hit profile notes", ""]
    t, near, ages, death = Table(examples), Table(examples), Table(examples), Table(examples)
    for h in hits:
        names = hit_names(h)
        for line in (h.get("profile_notes") or "").split("\n"):
            s = line.strip()
            if not s:
                continue
            if s.startswith("According to"):
                m = DATED_REPORT.match(s)
                if m:
                    t.add("dated report (counts): According to <SOURCE>; <Month D, YYYY>:", f"{h['hit_id']} {m.group(1)}")
                else:
                    head = re.sub(r"According to .+?;", "According to {source};", s)[:70]
                    near.add(mask(head, names), h["hit_id"])
            if re.search(r"\b(age[ds]?|aged|years? of age|year[- ]old|\d+[- ]year)\b", s, re.I):
                for sent in re.split(r"(?<=[.;])\s+", s):
                    if re.search(r"\b(age[ds]?|aged|years? of age|year[- ]old|\d+[- ]year)\b", sent, re.I):
                        ages.add(mask(sent, names), h["hit_id"])
            if re.search(r"\b(died|death|deceased|passed away|late)\b", s, re.I):
                death.add(mask(short(s, 140), names), h["hit_id"])
    md += t.md("Dated reports in POLICY's exact form", top)
    md += near.md("Lines starting 'According to' that are NOT in the exact dated form (POLICY: they do not count)", top)
    md += ages.md("Age-at-date sentences (each gives two possible birth years)", top)
    md += death.md("Death mentions in profile text", top)
    t = Table(examples)
    for h in hits:
        cats = h.get("categories") or []
        prim = h.get("primary_category") or (cats[0] if cats else None)
        t.add(f"primary={h.get('primary_category')!r} → used={prim!r} [{h['entity_type']}]", h["hit_id"])
    md += t.md("Category used for the listing phrase (primary, else first)", top)

    # ------------------------------------------------------------ search results
    md += ["## 9. Search results", "",
           "A result is about the hit only if it carries the hit's listing phrase AND a country tied to the hit.", ""]
    t, phr = Table(examples), Table(examples)
    for s in srs:
        q = [s.get("query")]
        t.add(mask(s.get("snippet") or "", q), s["result_id"])
        found = [k for k, p in LISTING_PHRASES.items() if p in (s.get("snippet") or "")]
        phr.add(", ".join(found) or "no listing phrase", s["result_id"])
    md += phr.md("Listing phrases present in snippets", top)
    md += t.md("Snippet templates", top)
    per_q = collections.Counter(s.get("query") for s in srs)
    md += [f"Results per query: {dict(sorted(collections.Counter(per_q.values()).items()))}.", ""]

    # ------------------------------------------------------------ alerts / hits shape
    md += ["## 10. Alerts and hits", ""]
    t = Table(examples)
    for a in alerts:
        t.add(f"{len(a.get('hit_ids') or [])} hit(s)", a["alert_id"])
    md += t.md("Hits per alert", top)
    alert_by_id = {a["alert_id"]: a for a in alerts}
    t = Table(examples)
    for h in hits:
        a = alert_by_id.get(h["alert_id"], {})
        t.add(f"customer {a.get('customer', {}).get('entity_type')} vs hit {h['entity_type']}", h["hit_id"])
    md += t.md("Entity type: screened customer vs hit", top)
    t = Table(examples)
    for h in hits:
        t.add(f"{len(h.get('dobs') or [])} dob value(s)", h["hit_id"])
    md += t.md("Number of hit DOB values", top)
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir")
    ap.add_argument("--out", help="write markdown here (default: stdout)")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    text = build(args.data_dir, args.examples, args.top)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
