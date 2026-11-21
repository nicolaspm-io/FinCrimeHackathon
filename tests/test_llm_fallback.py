"""Unit tests for system/llm_fallback.py using mocked OpenRouter HTTP responses."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_DIR = os.path.join(REPO_ROOT, "system")

if SYSTEM_DIR not in sys.path:
    sys.path.insert(0, SYSTEM_DIR)

from contracts import ExtractionRequest, FactStatus, LLMFallback, NullLLMFallback
from llm_fallback import (
    DEFAULT_MODEL,
    OPENROUTER_URL,
    OpenRouterClient,
    create_llm_fallback,
    validate_extraction,
)


class _FakeResponse:
    """Minimal context-manager HTTP response stub."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _chat_envelope(content: str, *, total_tokens: int = 42) -> dict[str, object]:
    """Wrap raw message content in an OpenRouter chat completion payload."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": total_tokens},
    }


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    """Create a urllib HTTPError with optional headers."""
    return urllib.error.HTTPError(
        OPENROUTER_URL,
        code,
        f"HTTP {code}",
        headers or {},
        io.BytesIO(b""),
    )


class OpenRouterClientTest(unittest.TestCase):
    """Tests for OpenRouter transport, retries, caching, caps, and factory."""

    def test_request_payload_headers_model_override_and_cache(self) -> None:
        captured: list[tuple[str, dict[str, str], dict[str, object], float]] = []

        def opener(req, timeout=15.0):
            body = json.loads(req.data.decode("utf-8"))
            captured.append((req.full_url, dict(req.header_items()), body, timeout))
            return _FakeResponse(
                _chat_envelope(
                    json.dumps(
                        {
                            "lived_countries": [
                                {
                                    "country": "India",
                                    "subject": "customer",
                                    "evidence_quote": "living and working in India",
                                }
                            ]
                        }
                    ),
                    total_tokens=55,
                )
            )

        client = OpenRouterClient(
            api_key="test-key",
            http_opener=opener,
            sleep_fn=lambda _: None,
        )
        self.assertIsInstance(client, LLMFallback)
        self.assertEqual(client.model_id, DEFAULT_MODEL)

        text = "The customer noted living and working in India from 2013 to 2014."
        r1 = client.extract_residence_countries(text, "REC-1", created_at="2025-01-01")
        r2 = client.extract_residence_countries(text, "REC-2", created_at="2025-01-01")

        self.assertEqual(r1.status, FactStatus.VERIFIED)
        self.assertFalse(r1.cached)
        self.assertEqual(r1.payload["lived_countries"][0]["country"], "India")

        self.assertEqual(r2.status, FactStatus.VERIFIED)
        self.assertTrue(r2.cached)
        self.assertEqual(r2.source_id, "REC-2")
        self.assertEqual(len(captured), 1)

        url, headers, body, timeout = captured[0]
        self.assertEqual(url, OPENROUTER_URL)
        self.assertEqual(timeout, 15.0)
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["Content-type"], "application/json")
        self.assertEqual(body["model"], "baidu/ernie-5.5-flash-preview:free")
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["top_p"], 1.0)
        self.assertEqual(body["seed"], 42)
        self.assertEqual(body["max_tokens"], 256)

        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "custom/model:free"},
            clear=True,
        ):
            overridden = create_llm_fallback(http_opener=opener, sleep_fn=lambda _: None)
            self.assertIsInstance(overridden, OpenRouterClient)
            self.assertEqual(overridden.model_id, "custom/model:free")

    def test_missing_api_key_makes_zero_calls_and_factory_returns_null(self) -> None:
        called = False

        def opener(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("HTTP opener must not be called without API key")

        with mock.patch.dict(os.environ, {}, clear=True):
            fallback = create_llm_fallback()
            self.assertIsInstance(fallback, NullLLMFallback)
            client = OpenRouterClient(http_opener=opener, sleep_fn=lambda _: None)

        res = client.extract_hit_p1_dob("Born in 1974", "HIT-1")
        self.assertFalse(called)
        self.assertEqual(res.status, FactStatus.PENDING_CLUE)
        self.assertEqual(res.error, "missing_api_key")

    def test_retries_429_retry_after_and_503_backoff(self) -> None:
        sleeps: list[float] = []
        attempts = 0

        def opener(req, timeout=15.0):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _http_error(429, {"Retry-After": "3"})
            if attempts == 2:
                raise _http_error(503)
            return _FakeResponse(
                _chat_envelope(
                    json.dumps(
                        {
                            "birth_date": "1974",
                            "age": None,
                            "reference_date": None,
                            "subject": "hit",
                            "evidence_quote": "born in 1974",
                        }
                    )
                )
            )

        client = OpenRouterClient(
            api_key="k",
            max_retries=3,
            http_opener=opener,
            sleep_fn=sleeps.append,
        )
        res = client.extract_hit_p1_dob("Target was born in 1974.", "HIT-1")
        self.assertEqual(res.status, FactStatus.VERIFIED)
        self.assertEqual(res.payload["possible_years"], [1974])
        self.assertEqual(sleeps, [3.0, 2.0])

    def test_exhausted_retries_timeout_and_invalid_json_degrade_to_pending(self) -> None:
        sleeps: list[float] = []
        client_timeout = OpenRouterClient(
            api_key="k",
            max_retries=1,
            http_opener=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timed out")),
            sleep_fn=sleeps.append,
        )
        r_to = client_timeout.extract_hit_p1_dob("Aged 40 in 2020", "HIT-1")
        self.assertEqual(r_to.status, FactStatus.PENDING_CLUE)
        self.assertEqual(sleeps, [1.0])

        client_bad_json = OpenRouterClient(
            api_key="k",
            http_opener=lambda *a, **kw: _FakeResponse(_chat_envelope("not json at all")),
            sleep_fn=lambda _: None,
        )
        r_bad = client_bad_json.extract_kyc_note("Approved check for Bob", "NOTE-1")
        self.assertEqual(r_bad.status, FactStatus.PENDING_CLUE)
        self.assertIn("invalid_response", r_bad.error or "")

    def test_defensive_think_and_fence_stripping(self) -> None:
        wrapped = (
            "<think>internal reasoning</think>\n"
            "```json\n"
            '{"age": 45, "birth_date": null, "reference_date": "March 2019", '
            '"subject": "hit", "evidence_quote": "aged 45 in March 2019"}\n'
            "```"
        )
        client = OpenRouterClient(
            api_key="k",
            http_opener=lambda *a, **kw: _FakeResponse(_chat_envelope(wrapped)),
            sleep_fn=lambda _: None,
        )
        res = client.extract_hit_p1_dob("Subject was aged 45 in March 2019.", "HIT-1")
        self.assertEqual(res.status, FactStatus.VERIFIED)
        self.assertEqual(res.payload["possible_years"], [1973, 1974])

    def test_caps_max_calls_tokens_and_wall_clock(self) -> None:
        client_calls = OpenRouterClient(
            api_key="k",
            max_calls=1,
            http_opener=lambda *a, **kw: _FakeResponse(
                _chat_envelope(
                    json.dumps(
                        {
                            "birth_date": "1980",
                            "age": None,
                            "reference_date": None,
                            "subject": "hit",
                            "evidence_quote": "born 1980",
                        }
                    ),
                    total_tokens=10,
                )
            ),
            sleep_fn=lambda _: None,
        )
        self.assertEqual(
            client_calls.extract_hit_p1_dob("born 1980", "H1").status, FactStatus.VERIFIED
        )
        r_cap = client_calls.extract_hit_p1_dob("born 1981", "H2")
        self.assertEqual(r_cap.status, FactStatus.PENDING_CLUE)
        self.assertEqual(r_cap.error, "cap_max_calls")

        client_tok = OpenRouterClient(
            api_key="k",
            max_total_tokens=50,
            http_opener=lambda *a, **kw: _FakeResponse(
                _chat_envelope(
                    json.dumps(
                        {
                            "birth_date": "1980",
                            "age": None,
                            "reference_date": None,
                            "subject": "hit",
                            "evidence_quote": "born 1980",
                        }
                    ),
                    total_tokens=60,
                )
            ),
            sleep_fn=lambda _: None,
        )
        client_tok.extract_hit_p1_dob("born 1980", "H1")
        r_tok = client_tok.extract_hit_p1_dob("born 1981", "H2")
        self.assertEqual(r_tok.error, "cap_max_tokens")

        ticks = iter([0.0, 100.0])
        client_clock = OpenRouterClient(
            api_key="k",
            max_wall_clock_seconds=60.0,
            clock_fn=lambda: next(ticks),
            sleep_fn=lambda _: None,
        )
        r_clk = client_clock.extract_hit_p1_dob("born 1980", "H1")
        self.assertEqual(r_clk.error, "cap_wall_clock")

    def test_batch_extract_deterministic_ordering(self) -> None:
        seen_texts: list[str] = []

        def opener(req, timeout=15.0):
            body = json.loads(req.data.decode("utf-8"))
            prompt = body["messages"][1]["content"]
            yr = "1980" if "1980" in prompt else "1975"
            seen_texts.append(yr)
            return _FakeResponse(
                _chat_envelope(
                    json.dumps(
                        {
                            "birth_date": yr,
                            "age": None,
                            "reference_date": None,
                            "subject": "hit",
                            "evidence_quote": f"born {yr}",
                        }
                    )
                )
            )

        client = OpenRouterClient(api_key="k", http_opener=opener, sleep_fn=lambda _: None)
        reqs = [
            ExtractionRequest("hit_p1_dob", "born 1980", "SRC-B"),
            ExtractionRequest("hit_p1_dob", "born 1975", "SRC-A"),
            ExtractionRequest("hit_p1_dob", "born 1980", "SRC-C"),
        ]
        results = client.batch_extract(reqs)
        self.assertEqual([r.source_id for r in results], ["SRC-A", "SRC-B", "SRC-C"])
        self.assertEqual(seen_texts, ["1975", "1980"])
        self.assertTrue(results[2].cached)


class PostValidationGateTest(unittest.TestCase):
    """Tests for deterministic post-validation gates across all 4 tasks."""

    def test_fabricated_quote_rejected(self) -> None:
        status, _, _, err = validate_extraction(
            "hit_p1_dob",
            "Subject was born in 1972.",
            {
                "birth_date": "1972",
                "age": None,
                "reference_date": None,
                "subject": "hit",
                "evidence_quote": "completely invented quote",
            },
        )
        self.assertEqual(status, FactStatus.PENDING_CLUE)
        self.assertEqual(err, "missing_or_non_verbatim_quote")

    def test_third_party_and_negation_rejected(self) -> None:
        # Mislabeled customer on brother's residence
        s1, _, _, e1 = validate_extraction(
            "residence_note",
            "His brother lived in Peru for five years.",
            {
                "lived_countries": [
                    {
                        "country": "Peru",
                        "subject": "customer",
                        "evidence_quote": "His brother lived in Peru",
                    }
                ]
            },
        )
        self.assertEqual(s1, FactStatus.PENDING_CLUE)
        self.assertEqual(e1, "third_party_or_negated")

        # Negated residence
        s2, _, _, _ = validate_extraction(
            "residence_note",
            "Customer never lived in Brazil.",
            {
                "lived_countries": [
                    {
                        "country": "Brazil",
                        "subject": "customer",
                        "evidence_quote": "never lived in Brazil",
                    }
                ]
            },
        )
        self.assertEqual(s2, FactStatus.PENDING_CLUE)

        # Third-party spouse DOB on hit profile
        s3, _, _, _ = validate_extraction(
            "hit_p1_dob",
            "His spouse born 1980 was also named.",
            {
                "birth_date": "1980",
                "age": None,
                "reference_date": None,
                "subject": "hit",
                "evidence_quote": "spouse born 1980",
            },
        )
        self.assertEqual(s3, FactStatus.PENDING_CLUE)

    def test_kyc_negative_contradiction_and_hallucination_rejected(self) -> None:
        s_neg, _, _, err_neg = validate_extraction(
            "kyc_note",
            "Identity verification failed for JANE DOE, DOB 12/03/1980.",
            {
                "status": "approved",
                "verified_name": "JANE DOE",
                "verified_dob": "12/03/1980",
                "document_country": None,
                "document_number": None,
                "checked_at": None,
                "evidence_quote": "verification failed for JANE DOE, DOB 12/03/1980",
            },
        )
        self.assertEqual(s_neg, FactStatus.PENDING_CLUE)
        self.assertEqual(err_neg, "kyc_negative_contradiction")

        s_name, _, _, err_name = validate_extraction(
            "kyc_note",
            "Onfido approved JANE DOE on 2024-01-01.",
            {
                "status": "approved",
                "verified_name": "JOHN SMITH",
                "verified_dob": None,
                "document_country": None,
                "document_number": None,
                "checked_at": "2024-01-01",
                "evidence_quote": "Onfido approved JANE DOE on 2024-01-01.",
            },
        )
        self.assertEqual(s_name, FactStatus.PENDING_CLUE)
        self.assertEqual(err_name, "hallucinated_name")

    def test_code_side_age_math_and_country_territory_normalization(self) -> None:
        status, payload, _, err = validate_extraction(
            "sr_facts",
            "In June 2021, the 37-year-old Dutch official, a politically exposed person, appeared in court.",
            {
                "age": 37,
                "birth_date": None,
                "reference_date": "June 2021",
                "country": "Dutch",
                "subject": "hit",
                "evidence_quote": "In June 2021, the 37-year-old Dutch official",
            },
            context={
                "category_phrase": "a politically exposed person",
                "hit_countries": ["Netherlands"],
            },
        )
        self.assertIsNone(err)
        self.assertEqual(status, FactStatus.VERIFIED)
        self.assertEqual(payload["possible_years"], [1983, 1984])
        self.assertEqual(payload["country"], "Netherlands")

        # Europe / International dropped; Puerto Rico kept independent from US
        s_res, p_res, _, _ = validate_extraction(
            "residence_note",
            "Customer moved from Europe to Puerto Rico in 2019.",
            {
                "lived_countries": [
                    {
                        "country": "Europe",
                        "subject": "customer",
                        "evidence_quote": "from Europe",
                    },
                    {
                        "country": "Puerto Rico",
                        "subject": "customer",
                        "evidence_quote": "to Puerto Rico in 2019",
                    },
                ]
            },
        )
        self.assertEqual(s_res, FactStatus.VERIFIED)
        self.assertEqual(len(p_res["lived_countries"]), 1)
        self.assertEqual(p_res["lived_countries"][0]["country"], "Puerto Rico")

    def test_hygiene_no_leakage_and_lf_endings(self) -> None:
        import re

        mod_path = os.path.join(SYSTEM_DIR, "llm_fallback.py")
        test_path = os.path.abspath(__file__)
        for path in (mod_path, test_path):
            with open(path, "rb") as fh:
                self.assertNotIn(b"\r\n", fh.read(), f"CRLF found in {path}")

        with open(mod_path, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("dev/", "practice_labels"):
            self.assertNotIn(forbidden, source)
        self.assertIsNone(
            re.search(r"\b(?:H|CUS|NOTE|A)-[0-9A-Fa-f]{4,}\b", source)
        )


class PolicyGateRegressionTest(unittest.TestCase):
    """Regression tests for the review of PR #8: the gates must enforce POLICY 4 and 5 in code,
    not trust the model's judgement."""

    PEP = "a politically exposed person"

    def _sr(self, text, *, age, ref, country, hit_countries=("Brazil",), phrase=PEP):
        return validate_extraction(
            "sr_facts",
            text,
            {"age": age, "birth_date": None, "reference_date": ref, "country": country,
             "subject": "hit", "evidence_quote": text},
            context={"category_phrase": phrase, "hit_countries": list(hit_countries)},
        )

    def test_sr_namesake_with_wrong_listing_or_country_is_absent(self) -> None:
        # Wrong kind of listing and a country not tied to the hit: a namesake.
        s1, p1, _, e1 = self._sr(
            "In August 2025, 79 year old MARCOS ROBERTO COSTA, named in adverse media in "
            "United Kingdom, appeared in a published list.", age=79, ref="August 2025",
            country="United Kingdom")
        self.assertEqual((s1, e1), (FactStatus.ABSENT, "listing_phrase_absent"))
        self.assertEqual(p1, {})
        # Right phrase, wrong country.
        s2, _, _, e2 = self._sr(
            "In May 2019, MARCOS ROBERTO COSTA, 33, is a politically exposed person in Canada.",
            age=33, ref="May 2019", country="Canada")
        self.assertEqual((s2, e2), (FactStatus.ABSENT, "no_hit_country_in_snippet"))
        # No country and no phrase at all.
        s3, _, _, _ = self._sr("Maria Lopez, 44, a nurse, was interviewed in June 2013.",
                               age=44, ref="June 2013", country=None)
        self.assertEqual(s3, FactStatus.ABSENT)
        # Missing context is never treated as a match.
        s4, _, _, e4 = self._sr("MARCOS ROBERTO COSTA, 33, a politically exposed person in Brazil.",
                                age=33, ref="2019", country="Brazil", phrase="")
        self.assertEqual((s4, e4), (FactStatus.ABSENT, "missing_category_phrase"))

    def test_sr_qualifying_snippet_is_verified_and_country_must_be_tied(self) -> None:
        text = ("May 2019 news reports profiled 33 year old MARCOS ROBERTO COSTA, who is a "
                "politically exposed person in Brazil.")
        st, p, _, err = self._sr(text, age=33, ref="May 2019", country="Brazil")
        self.assertIsNone(err)
        self.assertEqual(st, FactStatus.VERIFIED)
        self.assertEqual(p["possible_years"], [1985, 1986])
        self.assertEqual(p["country"], "Brazil")
        # Model names a second country from the snippet that is not tied to the hit.
        text2 = text.replace("in Brazil.", "in Brazil, after years in Portugal.")
        st2, _, _, err2 = self._sr(text2, age=33, ref="May 2019", country="Portugal")
        self.assertEqual((st2, err2), (FactStatus.PENDING_CLUE, "country_not_tied_to_hit"))

    def test_extract_sr_facts_makes_no_call_for_non_qualifying_snippet(self) -> None:
        calls = []
        client = OpenRouterClient(api_key="k", http_opener=lambda *a, **kw: calls.append(1),
                                  sleep_fn=lambda _: None)
        res = client.extract_sr_facts("Eva Lopez, 33, a taxi driver in Netherlands.", "SR-X",
                                      category_phrase=self.PEP, hit_countries=("Spain",))
        self.assertEqual(res.status, FactStatus.ABSENT)
        self.assertEqual(calls, [])

    def test_kyc_dob_must_equal_the_date_in_the_quote(self) -> None:
        text = "AMAKA AFOLABI's identity was confirmed via Veriff on 2024-04-26. DOB recorded as 10/08/1988."
        def kyc(dob):
            return validate_extraction("kyc_note", text, {"status": "approved", "verified_dob": dob,
                                                          "evidence_quote": text})
        s_ok, p_ok, _, _ = kyc("1988-08-10")
        self.assertEqual(s_ok, FactStatus.VERIFIED)
        self.assertEqual(p_ok["verified_dob"], "1988-08-10")
        for wrong in ("1988-10-08", "1988-01-01", "10 October 1988"):
            s, _, _, e = kyc(wrong)
            self.assertEqual((s, e), (FactStatus.PENDING_CLUE, "ungrounded_dob"), wrong)

    def test_hit_birth_date_parts_must_match_quote(self) -> None:
        text = "Subject was born on 5 May 1980 in Lagos."
        def hit(bd):
            return validate_extraction("hit_p1_dob", text, {"birth_date": bd, "age": None,
                                       "reference_date": None, "subject": "hit", "evidence_quote": text})
        self.assertEqual(hit("1980-05-05")[0], FactStatus.VERIFIED)
        self.assertEqual(hit("1980")[0], FactStatus.VERIFIED)
        self.assertEqual(hit("1980-06-05")[3], "ungrounded_birth_date")

    def test_reworded_failed_verifications_never_verified_as_approved(self) -> None:
        fails = [
            "Onfido could not confirm the identity of MARIA LOPEZ, DOB 12/03/1980, passport from Spain.",
            "Identity verification for JOHN SMITH (DOB 1975-02-11) was unable to be completed by Veriff.",
            "Sumsub flagged the driving licence of ANA SILVA, born 4 June 1990, Portugal, as a mismatch.",
            "Persona check for LI WEI, DOB 1979-09-09: verification incomplete, documents refused.",
            "Veriff did not approve the national ID of OMAR ALI, DOB 05/05/1985, from Pakistan.",
            "The identity of PAUL MARTIN (born March 3, 1970) could not be validated by Onfido.",
            "Verification attempt for KATE BROWN, DOB 1988-12-01, UK passport, was abandoned.",
            "Sumsub returned a negative result for the passport of JUAN PEREZ, DOB 1966-06-16, Mexico.",
        ]
        for text in fails:
            s, _, _, e = validate_extraction("kyc_note", text, {"status": "approved", "evidence_quote": text})
            self.assertEqual((s, e), (FactStatus.PENDING_CLUE, "kyc_negative_contradiction"), text)

    def test_negated_or_visited_residence_rejected(self) -> None:
        for text, country in [
            ("Customer confirmed they were not living in Italy between 2010 and 2015.", "Italy"),
            ("The account holder denied ever living in France.", "France"),
            ("No residency in Nigeria was reported for 2012 to 2014.", "Nigeria"),
            ("Customer clarified he was only visiting Brazil, not residing there, in 2019.", "Brazil"),
            ("The user refuted claims of working in Ghana from 2011 to 2013.", "Ghana"),
            ("The client has never resided in Spain.", "Spain"),
        ]:
            s, _, _, _ = validate_extraction("residence_note", text, {"lived_countries": [
                {"country": country, "subject": "customer", "evidence_quote": text}]})
            self.assertEqual(s, FactStatus.PENDING_CLUE, text)

    def test_trimmed_quote_cannot_hide_third_party_or_other_reference_year(self) -> None:
        s, _, _, e = validate_extraction(
            "hit_p1_dob", "X was born in 1960 and her son, aged 30, was charged in 2001.",
            {"age": 30, "reference_date": "2001", "subject": "hit",
             "evidence_quote": "aged 30, was charged in 2001"})
        self.assertEqual((s, e), (FactStatus.PENDING_CLUE, "third_party_or_negated"))
        s2, _, _, e2 = validate_extraction(
            "hit_p1_dob",
            "Formal charges were brought against X, aged 30, in November 1998. "
            "Member of Congress (July 22, 2019 - 2027).",
            {"age": 30, "reference_date": "2019-07-22", "subject": "hit",
             "evidence_quote": "Formal charges were brought against X, aged 30"})
        self.assertEqual((s2, e2), (FactStatus.PENDING_CLUE, "ungrounded_reference_date"))


if __name__ == "__main__":
    unittest.main()
