"""Unit tests for system/names.py (POLICY principle 8 and the search-provider query key)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))

import names as N  # noqa: E402

VARIANTS = N.build_variants({
    "groups": [
        ["muhammad", "mohammed", "mohammad", "mohamed", "mohd"],
        ["yusuf", "yousef", "josef", "joseph", "joe"],
        ["giuseppe", "joseph", "joe"],
        ["abdul", "abdel", "abd"],
    ],
    "company_legal_forms": ["ab", "ag", "gmbh", "inc", "llc", "ltd", "na", "sa", "se", "spa", "limited"],
})
IND, COMP = "Individual", "Company"


def agree(a, b, entity=IND):
    return N.names_agree(a, b, entity, VARIANTS)


class Normalisation(unittest.TestCase):
    def test_case_accents_punctuation_brackets(self):
        self.assertEqual(N.name_parts("Müller-Kägi, HANS (Jr.) [aka Hansi]"), ("hans", "kagi", "muller"))

    def test_special_letters_fold(self):
        self.assertEqual(N.name_parts("Paweł Søren Strauß"), ("pawel", "soren", "strauss"))

    def test_apostrophe_joins(self):
        self.assertEqual(N.name_parts("Sean O'Brien"), ("obrien", "sean"))
        self.assertEqual(N.name_parts("Sean O’Brien"), ("obrien", "sean"))

    def test_initials_are_not_parts(self):
        self.assertEqual(N.name_parts("J. R. Bernard, Samuel"), ("bernard", "samuel"))

    def test_distinct_parts_count_once(self):
        self.assertEqual(N.name_parts("Abubakar Abubakar"), ("abubakar",))
        self.assertEqual(N.real_part_count("Mohamed Youssef Mohamed"), 2)

    def test_placeholders(self):
        self.assertEqual(N.name_parts("NADEEM UNKNOWN"), ("nadeem",))
        self.assertEqual(N.name_parts("UNKNOWN NADEEM"), ("nadeem",))
        self.assertEqual(N.name_parts("BUSTAMANTE 0000"), ("bustamante",))
        self.assertEqual(N.name_parts("MARTON XXX XXX"), ("marton",))
        self.assertEqual(N.name_parts("TEST N/A NONE null"), ())
        self.assertEqual(N.real_part_count("NADEEM UNKNOWN"), 1)

    def test_alphanumeric_tokens_kept(self):
        self.assertEqual(N.name_parts("N26 Bank SE", COMP, VARIANTS), ("bank", "n26"))

    def test_company_legal_forms_dropped(self):
        self.assertEqual(N.name_parts("TD Bank, N.A. GmbH", COMP, VARIANTS), ("bank", "td"))
        self.assertEqual(N.name_parts("Pescanova S.A.", COMP, VARIANTS), ("pescanova",))
        # legal forms are only dropped for companies
        self.assertEqual(N.name_parts("Inc Smith", IND, VARIANTS), ("inc", "smith"))


class PartMatching(unittest.TestCase):
    def test_short_parts_must_be_identical(self):
        self.assertIsNone(N.parts_match("ali", "aly"))
        self.assertEqual(N.parts_match("ali", "ali"), "exact")

    def test_four_to_six_letters_one_edit(self):
        self.assertEqual(N.parts_match("smith", "smyth"), "typo")
        self.assertIsNone(N.parts_match("mark", "markus"))  # 2 edits, shorter part has 4 letters
        self.assertIsNone(N.parts_match("john", "jonathan"))

    def test_seven_plus_letters_two_edits(self):
        self.assertEqual(N.parts_match("christina", "christian"), "typo")
        self.assertIsNone(N.parts_match("christina", "kristiane"))  # 3 edits

    def test_swap_costs_two_edits(self):
        self.assertEqual(N.levenshtein("sebastian", "sebastain"), 2)
        self.assertIsNone(N.parts_match("maria", "mraia"))  # swap in a 5-letter part = 2 edits > 1
        self.assertEqual(N.parts_match("sebastian", "sebastain"), "typo")  # 9 letters: 2 allowed

    def test_shorter_part_decides_budget(self):
        self.assertEqual(N.edit_budget("anna", "annabelle"), 1)
        self.assertEqual(N.edit_budget("bob", "bobby"), 0)

    def test_variants_are_pairwise_not_transitive(self):
        self.assertEqual(N.parts_match("mohammed", "muhammad", VARIANTS), "variant")
        self.assertEqual(N.parts_match("yusuf", "joseph", VARIANTS), "variant")
        self.assertEqual(N.parts_match("giuseppe", "joseph", VARIANTS), "variant")
        self.assertIsNone(N.parts_match("yusuf", "giuseppe", VARIANTS))

    def test_levenshtein_limit(self):
        self.assertEqual(N.levenshtein("kitten", "sitting"), 3)
        self.assertEqual(N.levenshtein("kitten", "sitting", limit=1), 2)


class NameAgreement(unittest.TestCase):
    def test_policy_written_together_or_apart(self):
        m = N.match_names("Abdulsalam Bello", "Abdul Salam Bello", IND, VARIANTS)
        self.assertTrue(m.agrees)
        self.assertIn((("abdulsalam",), ("abdul", "salam"), "joined"), m.pairs)
        self.assertEqual(m.matched, 2)  # the joined match counts once

    def test_order_does_not_matter(self):
        self.assertTrue(agree("Bernard, Samuel", "Samuel Bernard"))
        self.assertTrue(agree("Elvira Velasco Morillo", "Velasco Morillo, Elvira"))

    def test_two_matching_parts_agree(self):
        self.assertTrue(agree("Mohammed Ali Khan", "Muhammad Ali"))

    def test_one_shared_surname_is_not_enough(self):
        self.assertFalse(agree("CHINEDU AZEEZ", "Ridwan, Azeez"))  # H-2808BE
        self.assertFalse(agree("Mark Smith", "Markus Smith"))
        self.assertFalse(agree("John Whitfield", "Jonathan Reyes Whitfield"))
        self.assertFalse(agree("Yusuf Kaya", "Giuseppe Kaya", IND))

    def test_every_part_of_shorter_name(self):
        self.assertTrue(agree("Azeez", "Ridwan Azeez"))  # 1-part alias fully covered
        self.assertTrue(agree("Pescanova S.A.", "Pescanova, Sociedad Anónima", COMP))
        self.assertTrue(agree("N26 Bank AG", "N26 Bank SE", COMP))

    def test_each_part_used_once(self):
        m = N.match_names("Ali Ali", "Ali Khan Hassan", IND, VARIANTS)
        self.assertEqual(m.matched, 1)  # "ali" counts once on both sides
        self.assertTrue(m.agrees)  # ...and covers the shorter (1-part) name

    def test_placeholder_only_names_never_agree(self):
        self.assertFalse(agree("UNKNOWN TEST", "Unknown Test"))

    def test_best_agreement_scans_aliases(self):
        found = N.best_agreement(["Chinedu Azeez"], ["Ridwan, Azeez", "Chinedu A. Azeez"], IND, VARIANTS)
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "Chinedu A. Azeez")
        self.assertIsNone(N.best_agreement(["Chinedu Azeez"], ["Ridwan, Azeez"], IND, VARIANTS))

    def test_deterministic(self):
        first = N.match_names("Mohamed Youssef Mohamed", "Mohammed Yousef", IND, VARIANTS)
        for _ in range(5):
            self.assertEqual(N.match_names("Mohamed Youssef Mohamed", "Mohammed Yousef", IND, VARIANTS), first)


class QueryKey(unittest.TestCase):
    def test_sorted_lowercase_without_initials(self):
        self.assertEqual(N.query_key("Bernard, J. Samuel"), "bernard samuel")

    def test_repeats_are_kept(self):
        self.assertEqual(N.query_key("Abubakar, Abubakar"), "abubakar abubakar")
        self.assertEqual(N.query_key("Mohamed Youssef Mohamed"), "mohamed mohamed youssef")

    def test_provider_quirks(self):
        self.assertEqual(N.query_key("O’Connor, Michael"), "connor michael")
        self.assertEqual(N.query_key("O'Connor, Michael"), "connor michael")
        self.assertEqual(N.query_key("Drozd, Tomasz Paweł"), "drozd pawe tomasz")
        self.assertEqual(N.query_key("Jérôme Kerviel"), "jerome kerviel")


DEV = os.path.join(ROOT, "dev")


@unittest.skipUnless(os.path.isdir(DEV), "dev/ data not present")
class DevData(unittest.TestCase):
    """Checks against the real dev files (they are not part of the public submission)."""

    @classmethod
    def setUpClass(cls):
        import json
        with open(os.path.join(DEV, "hits.jsonl"), encoding="utf-8") as handle:
            cls.hits = [json.loads(line) for line in handle if line.strip()]
        with open(os.path.join(DEV, "search_results.jsonl"), encoding="utf-8") as handle:
            cls.queries = {json.loads(line)["query"] for line in handle if line.strip()}
        cls.variants = N.load_variants(DEV)

    def test_query_key_covers_every_individual_hit(self):
        individuals = [h for h in self.hits if h["entity_type"] == IND]
        missing = [h["hit_id"] for h in individuals if N.query_key(h["name"]) not in self.queries]
        self.assertEqual(missing, [])
        self.assertEqual(len(individuals), 702)

    def test_real_variants_file(self):
        self.assertTrue(self.variants.same_group("mohammed", "muhammad"))
        self.assertFalse(self.variants.same_group("yusuf", "giuseppe"))
        self.assertIn("gmbh", self.variants.legal_forms)
        self.assertIn("na", self.variants.legal_forms)


if __name__ == "__main__":
    unittest.main()
