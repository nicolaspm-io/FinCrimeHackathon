#!/usr/bin/env python3
"""Dump every raw record that could matter for one or more hits, with NO decision logic.

    python3 hit_dossier.py <data_dir> H-0181BD [H-... | A-...] [--json] [--all-notes]

For each hit it prints:
  1. the alert (customer as screened, link fields, screened_at)
  2. the hit profile, in full (names, aliases, dobs, countries, addresses, categories, notes)
  3. customer candidates
       - LINKED: following the alert's account_ref / login_handle / email
       - LOOSE (only when the alert has no link, or the link leads nowhere): records whose
         name parts overlap the screened name, ranked by overlap; the reviewer decides
  4. for each candidate: the record, its accounts, onboarding -> KYC checks (all, oldest
     first, with result), notes filed under its email, and RELATED records that share a
     phone (customers or accounts) or a document number on a KYC check
  5. search results whose query is any of the hit's or the customer's names (token set)

Parsed dates are shown next to raw values only as a reading aid ("UNPARSED by helper"
means this helper could not read it — not that the data is wrong).
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (edit_distance, fmt_date, hit_names, index, load, name_parts,  # noqa: E402
                     parse_date, short)

BOILERPLATE_HINTS = (
    "asked about savings", "international transfer takes", "card payment was declined",
    "requested a statement", "updated their mobile number", "reported a lost card",
)


class Data:
    def __init__(self, data_dir):
        d = load(data_dir)
        self.alerts = {a["alert_id"]: a for a in d["alerts"]}
        self.hits = {h["hit_id"]: h for h in d["hits"]}
        self.customers = {c["record_id"]: c for c in d["customers"]}
        self.acc_by_id = index(d["accounts"], "account_id")
        self.acc_by_login = index(d["accounts"], "login_handle")
        self.acc_by_cust = index(d["accounts"], "customer_id")
        self.acc_by_phone = index(d["accounts"], "phone")
        self.onb_by_cust = index(d["onboarding"], "customer_id")
        self.onb_by_acc = index(d["onboarding"], "account_id")
        self.onb_by_app = index(d["onboarding"], "kyc_applicant_id")
        self.kyc_by_app = index(d["kyc_checks"], "applicant_id")
        self.kyc_by_doc = index(d["kyc_checks"], "document_number")
        self.notes_by_email = collections.defaultdict(list)
        for n in d["notes"]:
            if n.get("requester_email"):
                self.notes_by_email[n["requester_email"].lower()].append(n)
        self.cust_by_email = collections.defaultdict(list)
        self.cust_by_phone = collections.defaultdict(list)
        self.cust_by_login = collections.defaultdict(list)
        self.cust_by_part = collections.defaultdict(set)
        for c in d["customers"]:
            if c.get("email"):
                self.cust_by_email[c["email"].lower()].append(c)
            if c.get("phone"):
                self.cust_by_phone[c["phone"]].append(c)
            if c.get("login_name"):
                self.cust_by_login[c["login_name"]].append(c)
            for field in ("full_name", "preferred_name", "login_name"):
                for p in name_parts(c.get(field)):
                    self.cust_by_part[p].add(c["record_id"])
        self.sr_by_tokens = collections.defaultdict(list)
        for s in d["search_results"]:
            self.sr_by_tokens[frozenset(name_parts(s.get("query")))].append(s)
        self.parts_vocab = list(self.cust_by_part)

    # ---------------------------------------------------------------- customers
    def linked_customers(self, alert):
        """[(record_id, how)] following the alert's link fields, without judging them."""
        found = []
        if alert.get("account_ref"):
            rows = self.acc_by_id.get(alert["account_ref"], [])
            if not rows:
                found.append((None, f"account_ref {alert['account_ref']} -> NOT FOUND in accounts"))
            for r in rows:
                found.append((r["customer_id"], f"account_ref {alert['account_ref']} -> accounts row customer_id {r['customer_id']}" + (" (joint: account has %d holders)" % len(rows) if len(rows) > 1 else "")))
        if alert.get("login_handle"):
            rows = self.acc_by_login.get(alert["login_handle"], [])
            for r in rows:
                found.append((r["customer_id"], f"login_handle {alert['login_handle']} -> accounts {r['account_id']} -> {r['customer_id']}"))
            for c in self.cust_by_login.get(alert["login_handle"], []):
                found.append((c["record_id"], f"login_handle {alert['login_handle']} -> customers.login_name"))
            if not rows and not self.cust_by_login.get(alert["login_handle"]):
                found.append((None, f"login_handle {alert['login_handle']} -> NOT FOUND"))
        if alert.get("email"):
            rows = self.cust_by_email.get(alert["email"].lower(), [])
            for c in rows:
                found.append((c["record_id"], f"email {alert['email']} -> customers.email"))
            if not rows:
                found.append((None, f"email {alert['email']} -> NOT FOUND in customers"))
        return found

    def loose_candidates(self, alert, limit=12):
        """Records whose name parts overlap the screened name (exact or <=2 edits), ranked.
        Deliberately wide; the screened DOB/country/city are printed for the reviewer to judge."""
        screened = alert["customer"]
        parts = name_parts(screened.get("name"))
        score = collections.Counter()
        for p in parts:
            score.update({rid: 2 for rid in self.cust_by_part.get(p, ())})
            if len(p) >= 4:
                for q in self.parts_vocab:
                    if q != p and abs(len(q) - len(p)) <= 2 and q[:1] == p[:1] and edit_distance(p, q, 2) <= 2:
                        score.update({rid: 1 for rid in self.cust_by_part[q]})
        sdob = parse_date(screened.get("dob")) if screened.get("dob") else None
        ranked = []
        for rid, s in score.items():
            c = self.customers[rid]
            bonus = 0
            cdob = parse_date(c.get("dob"))
            if sdob and cdob and sdob[0] == cdob[0]:
                bonus += 2
            if screened.get("city") and screened["city"].lower() in ((c.get("city") or "") + " " + (c.get("address") or "")).lower():
                bonus += 1
            if c.get("entity_type") == screened.get("entity_type"):
                bonus += 1
            ranked.append((s + bonus, rid))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return ranked[:limit]

    def picture(self, rid):
        """Everything filed against one customer record, plus records sharing phone/doc."""
        c = self.customers.get(rid)
        if c is None:
            return {"record_id": rid, "missing": True}
        accounts = self.acc_by_cust.get(rid, [])
        # onboarding rows carry their own customer_id: only this record's rows are its KYC
        # applicants. Rows reached through a (joint) account but owned by another customer
        # are listed separately so they are never mistaken for this person's checks.
        onboarding = list(self.onb_by_cust.get(rid, []))
        foreign_onboarding = []
        for a in accounts:
            for o in self.onb_by_acc.get(a["account_id"], []):
                if o.get("customer_id") != rid and o not in foreign_onboarding:
                    foreign_onboarding.append(o)
        checks = []
        for o in onboarding:
            checks.extend(self.kyc_by_app.get(o["kyc_applicant_id"], []))
        checks.sort(key=lambda k: (k.get("checked_at") or "", k["check_id"]))
        notes = sorted(self.notes_by_email.get((c.get("email") or "").lower(), []), key=lambda n: (n.get("created_at") or "", n["note_id"]))
        related = collections.defaultdict(set)
        phones = {c.get("phone")} | {a.get("phone") for a in accounts}
        for ph in filter(None, phones):
            for other in self.cust_by_phone.get(ph, []):
                if other["record_id"] != rid:
                    related[other["record_id"]].add(f"shares phone {ph} (customer record)")
            for a in self.acc_by_phone.get(ph, []):
                if a["customer_id"] != rid:
                    related[a["customer_id"]].add(f"shares phone {ph} (account {a['account_id']})")
        for k in checks:
            if not k.get("document_number"):
                continue
            for other in self.kyc_by_doc.get(k["document_number"], []):
                for o in self.onb_by_app.get(other["applicant_id"], []):
                    if o["customer_id"] != rid:
                        related[o["customer_id"]].add(
                            f"document {k['document_number']}: {k['check_id']} ({k['result']}) / {other['check_id']} ({other['result']})")
        own_dob = parse_date(c.get("dob"))
        for other in related:
            oc = self.customers.get(other) or {}
            odob = parse_date(oc.get("dob"))
            same = "same full dob (parsed)" if own_dob and odob and own_dob == odob and None not in own_dob else "dob differs or partial"
            related[other].add(f"other dob {oc.get('dob')!r}: {same}")
        return {"record": c, "accounts": accounts, "onboarding": onboarding, "foreign_onboarding": foreign_onboarding, "kyc_checks": checks,
                "notes": notes, "related": {k: sorted(v) for k, v in sorted(related.items())}}

    def search_results(self, names):
        out, seen = [], set()
        for n in names:
            key = frozenset(name_parts(n))
            for s in self.sr_by_tokens.get(key, []):
                if s["result_id"] not in seen:
                    seen.add(s["result_id"])
                    out.append((n, s))
        return out


def customer_names(pic):
    c = pic.get("record") or {}
    names = [c.get("full_name"), c.get("preferred_name"), c.get("login_name")]
    names += [k.get("verified_name") for k in pic.get("kyc_checks", [])]
    return [n for n in dict.fromkeys(names) if n]


def dossier(data, hit_id, all_notes=False, expand_related=True):
    hit = data.hits[hit_id]
    alert = data.alerts.get(hit["alert_id"], {})
    linked = data.linked_customers(alert) if alert else []
    rids = [rid for rid, _ in linked if rid]
    loose = [] if rids else data.loose_candidates(alert)
    candidates = list(dict.fromkeys(rids + [rid for _, rid in loose]))
    pictures = {rid: data.picture(rid) for rid in candidates}
    related_pictures = {}
    if expand_related:
        for rid in rids:
            for other in pictures[rid].get("related", {}):
                if other not in pictures and other not in related_pictures:
                    related_pictures[other] = data.picture(other)
    names = hit_names(hit)
    for pic in list(pictures.values())[: max(1, len(rids))]:
        names += customer_names(pic)
    return {"hit": hit, "alert": alert, "linked": linked, "loose": loose, "pictures": pictures,
            "related_pictures": related_pictures, "search_results": data.search_results(names),
            "all_notes": all_notes}


def render_picture(rid, pic, all_notes, out):
    if pic.get("missing"):
        out.append(f"    {rid}: NOT IN customers.jsonl")
        return
    c = pic["record"]
    out.append(f"    {rid} [{c['entity_type']}] full_name={c['full_name']!r} preferred={c.get('preferred_name')!r} login={c.get('login_name')!r}")
    out.append(f"      dob {fmt_date(c.get('dob'))} | nationality={c.get('nationality')!r} residence={c.get('residence_country')!r} city={c.get('city')!r}")
    out.append(f"      address={c.get('address')!r} email={c.get('email')} phone={c.get('phone')} reg_no={c.get('registration_number')} status={c.get('status')} onboarded={c.get('onboarded_at')}")
    for a in pic["accounts"]:
        out.append(f"      account {a['account_id']} login={a.get('login_handle')} phone={a.get('phone')} opened={a.get('opened_at')} status={a.get('status')}")
    for o in pic["onboarding"]:
        out.append(f"      onboarding {o['account_id']} -> applicant {o['kyc_applicant_id']} ({o.get('onboarded_at')})")
    for o in pic.get("foreign_onboarding", []):
        out.append(f"      (onboarding of shared account {o['account_id']} belongs to {o.get('customer_id')}: applicant {o['kyc_applicant_id']} — not this record's KYC)")
    for k in pic["kyc_checks"]:
        out.append(f"      KYC {k['check_id']} {k['checked_at']} {k['result'].upper():8} {k['provider']}: name={k.get('verified_name')!r} dob {fmt_date(k.get('verified_dob'))} doc={k.get('document_type')} {k.get('document_number')} {k.get('document_country')}")
    shown = 0
    for n in pic["notes"]:
        boring = any(h in n["text"].lower() for h in BOILERPLATE_HINTS)
        if boring and not all_notes:
            continue
        shown += 1
        out.append(f"      NOTE {n['note_id']} {n.get('created_at')} [{n.get('channel')}]: {n['text']}")
    hidden = len(pic["notes"]) - shown
    if hidden:
        out.append(f"      ({hidden} routine service notes hidden; --all-notes to show)")
    for other, why in pic["related"].items():
        out.append(f"      RELATED {other}: {'; '.join(why)}")


def render(dos):
    h, a = dos["hit"], dos["alert"]
    out = [f"=== {h['hit_id']}  (alert {h['alert_id']})"]
    if a:
        s = a["customer"]
        out.append(f"ALERT screened_at={a.get('screened_at')} name={s.get('name')!r} type={s.get('entity_type')} dob {fmt_date(s.get('dob'))} country={s.get('country')!r} city={s.get('city')!r}")
        out.append(f"      link: account_ref={a.get('account_ref')} login_handle={a.get('login_handle')} email={a.get('email')}  hits={a.get('hit_ids')}")
    out.append(f"HIT [{h['entity_type']}] name={h['name']!r} best_matching={h.get('best_matching_name')!r} first/middle/last={h.get('first_name')!r}/{h.get('middle_name')!r}/{h.get('last_name')!r} score={h.get('match_score')}")
    out.append(f"    aliases={h.get('aliases')}")
    out.append(f"    dobs={[fmt_date(x) for x in h.get('dobs') or []]} gender={h.get('gender')} life_status={h.get('life_status')} deceased_date={h.get('deceased_date')!r}")
    out.append(f"    countries={h.get('countries')} nationality={h.get('nationality')} citizenship={h.get('citizenship')} place_of_birth={h.get('place_of_birth')!r}")
    out.append(f"    addresses={h.get('addresses')}")
    out.append(f"    categories={h.get('categories')} primary={h.get('primary_category')!r} sub={h.get('subcategories')} position={short(h.get('position_or_allegation'), 160)}")
    out.append(f"    identifiers={h.get('identifiers')} relationships={short(h.get('relationships'), 200)}")
    if h.get("details"):
        out.append(f"    details={short(json.dumps(h['details'], ensure_ascii=False), 400)}")
    if h.get("sources"):
        out.append(f"    sources={h.get('sources')}")
    out.append("    profile_notes:")
    for line in (h.get("profile_notes") or "(none)").split("\n"):
        if line.strip():
            out.append(f"      | {line}")
    out.append("CUSTOMER LINKS:")
    for rid, how in dos["linked"] or [(None, "alert carries no link")]:
        out.append(f"    {how}")
    if dos["loose"]:
        out.append("    LOOSE name candidates (no usable link; ranked by name-part overlap + dob-year/city/type bonus — judge them yourself):")
        for score, rid in dos["loose"]:
            out.append(f"      score {score}: {rid}")
    out.append("CUSTOMER PICTURES:")
    for rid, pic in dos["pictures"].items():
        render_picture(rid, pic, dos["all_notes"], out)
    if dos["related_pictures"]:
        out.append("RELATED RECORDS (share a phone or document number with a linked record — same person only if POLICY 3 says so):")
        for rid, pic in dos["related_pictures"].items():
            render_picture(rid, pic, dos["all_notes"], out)
    out.append("SEARCH RESULTS (query = one of the hit's or customer's names):")
    for name, s in dos["search_results"] or [(None, None)]:
        if s is None:
            out.append("    none")
            continue
        out.append(f"    {s['result_id']} q={s['query']!r} [{s.get('source')}] {s.get('title')!r}: {s['snippet']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir")
    ap.add_argument("ids", nargs="+", help="hit IDs (H-...) or alert IDs (A-..., expands to its hits)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--all-notes", action="store_true", help="also show routine service notes")
    args = ap.parse_args()
    data = Data(args.data_dir)
    hit_ids = []
    for i in args.ids:
        if i.startswith("A-"):
            hit_ids += data.alerts[i]["hit_ids"]
        elif i in data.hits:
            hit_ids.append(i)
        else:
            sys.exit(f"unknown id {i}")
    for hid in hit_ids:
        dos = dossier(data, hid, args.all_notes)
        if args.json:
            dos["search_results"] = [s for _, s in dos["search_results"]]
            print(json.dumps(dos, ensure_ascii=False, default=list))
        else:
            print(render(dos))
            print()


if __name__ == "__main__":
    main()
