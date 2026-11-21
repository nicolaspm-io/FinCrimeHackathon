"""Grounded OpenRouter API fallback for the 4 narrow free-text extraction tasks."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Iterable
import unicodedata
import urllib.error
import urllib.request

try:
    from . import countries, dates
    from .contracts import (
        ExtractionRequest,
        ExtractionResult,
        FactStatus,
        LLMFallback,
        NullLLMFallback,
        TASK_HIT_P1_DOB,
        TASK_KYC_NOTE,
        TASK_RESIDENCE_NOTE,
        TASK_SR_FACTS,
    )
except ImportError:
    import countries
    import dates
    from contracts import (
        ExtractionRequest,
        ExtractionResult,
        FactStatus,
        LLMFallback,
        NullLLMFallback,
        TASK_HIT_P1_DOB,
        TASK_KYC_NOTE,
        TASK_RESIDENCE_NOTE,
        TASK_SR_FACTS,
    )

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "baidu/ernie-5.5-flash-preview:free"
PROMPT_VERSION = "v1"

_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)

_NEGATION_RE = re.compile(
    r"\b(?:never\s+(?:lived|resided|worked|stayed)|"
    r"did\s+not\s+(?:live|reside|work|stay|verify|pass)|"
    r"has\s+not\s+(?:lived|resided|worked)|"
    r"not\s+a\s+resident|denied\s+(?:living|residing|any\s+residence)|"
    r"no\s+residence|not\s+(?:lived|approved|verified))\b",
    re.IGNORECASE,
)

_THIRD_PARTY_RE = re.compile(
    r"\b(?:brother|sister|spouse|wife|husband|mother|father|parent|"
    r"son|daughter|uncle|aunt|cousin|friend|colleague|relative|"
    r"neighbor|neighbour)\b",
    re.IGNORECASE,
)

_KYC_NEGATIVE_RE = re.compile(
    r"\b(?:rejected|failed|expired|declined|denied|unverified|"
    r"invalid|unsuccessful|not\s+approved|not\s+verified|did\s+not\s+pass|"
    r"could\s+not\s+verify|no\s+longer\s+valid)\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a strict compliance information extractor. "
    "Extract ONLY facts explicitly stated in the provided text. "
    "Never guess, infer unstated facts, or perform birth-year subtraction yourself. "
    "Every extracted claim MUST include an exact verbatim evidence_quote copied from the text. "
    "Respond ONLY with a valid JSON object matching the requested schema."
)


def _fold_words(text: str) -> set[str]:
    """Fold accents and lowercase into Unicode alphanumeric word tokens."""
    nfkd = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()
    words: set[str] = set()
    current: list[str] = []
    for ch in f"{plain} ":
        if unicodedata.category(ch)[0] in ("L", "M", "N"):
            current.append(ch)
        elif current:
            words.add("".join(current))
            current.clear()
    return words


def _norm_ws(text: str) -> str:
    """Normalize NFKC and collapse whitespace for verbatim quote comparison."""
    cleaned = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _is_verbatim_quote(quote: Any, text: str) -> bool:
    """Return True when quote is a non-empty verbatim substring of text."""
    if not isinstance(quote, str) or not quote.strip():
        return False
    if quote in text:
        return True
    norm_quote = _norm_ws(quote)
    return bool(norm_quote) and norm_quote in _norm_ws(text)


def _contains_number(text: str, value: int) -> bool:
    """Return True when integer value appears as a standalone number in text."""
    return bool(re.search(rf"(?<!\d){value}(?!\d)", text))


def _normalize_country_value(raw: Any) -> str | None:
    """Normalize a country string via countries.py; return None if unknown/non-country."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return countries.normalize_country(raw) or countries.country_from_location(raw)


def _is_country_grounded(
    raw_country: Any, norm_country: str | None, text: str, quote: str
) -> bool:
    """Return True when normalized country is grounded in text or quote."""
    if norm_country is None:
        return False
    if norm_country in countries.country_mentions(text):
        return True
    if isinstance(raw_country, str) and raw_country.strip():
        if _norm_ws(raw_country) in _norm_ws(text):
            return True
    code2 = countries.iso2(norm_country)
    if code2 and re.search(rf"(?<![a-zA-Z]){re.escape(code2)}(?![a-zA-Z])", f"{text} {quote}"):
        return True
    return False


def _build_prompt(
    task: str,
    text: str,
    *,
    created_at: str = "",
    reference_date: str = "",
    category_phrase: str = "",
    hit_countries: tuple[str, ...] = (),
) -> str:
    """Build task-specific user prompt with exact target JSON schema."""
    if task == TASK_KYC_NOTE:
        return (
            "Task: Extract identity verification fields from this KYC note.\n"
            f"Note created_at: {created_at or 'unknown'}\n"
            "Return JSON with exact keys:\n"
            '{"status": "approved|rejected|expired|unknown", '
            '"verified_name": str|null, "verified_dob": str|null, '
            '"document_country": str|null, "document_number": str|null, '
            '"checked_at": str|null, "evidence_quote": str|null}\n'
            f"Text:\n{text}"
        )
    if task == TASK_RESIDENCE_NOTE:
        return (
            "Task: Extract countries where the customer lived or worked (NOT mere transfers or relatives).\n"
            f"Note created_at: {created_at or 'unknown'}\n"
            "Return JSON with exact keys:\n"
            '{"lived_countries": [{"country": str, "subject": "customer|third_party|unknown", '
            '"evidence_quote": str}]}\n'
            f"Text:\n{text}"
        )
    if task == TASK_HIT_P1_DOB:
        return (
            "Task: Extract birth date or stated age+reference date for the listed person in paragraph 1.\n"
            f"Default reference_date context: {reference_date or 'none'}\n"
            "Return JSON with exact keys:\n"
            '{"birth_date": str|null, "age": int|null, "reference_date": str|null, '
            '"subject": "hit|third_party|unknown", "evidence_quote": str|null}\n'
            f"Text:\n{text}"
        )
    if task == TASK_SR_FACTS:
        countries_str = ", ".join(hit_countries) if hit_countries else "none"
        return (
            "Task: Extract birth date or age+reference date and country for the listed person in this search snippet.\n"
            f"Target listing phrase: {category_phrase or 'unknown'}\n"
            f"Target hit countries: {countries_str}\n"
            "Return JSON with exact keys:\n"
            '{"age": int|null, "birth_date": str|null, "reference_date": str|null, '
            '"country": str|null, "subject": "hit|third_party|unknown", "evidence_quote": str|null}\n'
            f"Text:\n{text}"
        )
    raise ValueError(f"Unknown extraction task: {task!r}")


def _extract_json_object(raw_content: str) -> dict[str, Any]:
    """Defensively strip reasoning tags or code fences and parse JSON object."""
    cleaned = _THINK_RE.sub("", raw_content).strip()
    fence_match = _FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Response content did not contain a valid JSON object")


def _validate_kyc_note(
    text: str, payload: dict[str, Any], context: dict[str, Any]
) -> tuple[FactStatus, dict[str, Any], str | None, str | None]:
    """Post-validate kyc_note payload."""
    status = payload.get("status")
    if status not in ("approved", "rejected", "expired", "unknown"):
        return FactStatus.PENDING_CLUE, {}, None, "invalid_kyc_status"

    quote = payload.get("evidence_quote")
    if not _is_verbatim_quote(quote, text):
        return FactStatus.PENDING_CLUE, {}, None, "missing_or_non_verbatim_quote"

    if status == "approved" and (_KYC_NEGATIVE_RE.search(quote) or _KYC_NEGATIVE_RE.search(text)):
        return FactStatus.PENDING_CLUE, {}, None, "kyc_negative_contradiction"

    verified_name = payload.get("verified_name")
    if verified_name is not None:
        if not isinstance(verified_name, str) or not verified_name.strip():
            verified_name = None
        else:
            name_words = _fold_words(verified_name)
            text_words = _fold_words(text)
            if not name_words or not name_words.issubset(text_words):
                return FactStatus.PENDING_CLUE, {}, None, "hallucinated_name"
            verified_name = verified_name.strip()

    raw_dob = payload.get("verified_dob")
    dob_iso: str | None = None
    if raw_dob is not None:
        if not isinstance(raw_dob, str) or not raw_dob.strip():
            raw_dob = None
        else:
            alts = dates.parse_date_alternatives(raw_dob, dates.BANK)
            if not alts or not _contains_number(quote, alts[0].year):
                return FactStatus.PENDING_CLUE, {}, None, "ungrounded_dob"
            dob_iso = alts[0].iso_str

    raw_country = payload.get("document_country")
    norm_country = _normalize_country_value(raw_country)
    if norm_country is not None and not _is_country_grounded(
        raw_country, norm_country, text, quote
    ):
        return FactStatus.PENDING_CLUE, {}, None, "hallucinated_country"

    doc_number = payload.get("document_number")
    if doc_number is not None:
        if not isinstance(doc_number, str) or not doc_number.strip():
            doc_number = None
        else:
            doc_number = doc_number.strip()
            if doc_number.lower() not in _norm_ws(text):
                return FactStatus.PENDING_CLUE, {}, None, "hallucinated_doc_number"

    raw_checked = payload.get("checked_at") or context.get("created_at") or ""
    checked_iso = ""
    if isinstance(raw_checked, str) and raw_checked.strip():
        checked_parsed = dates.parse_date(raw_checked, dates.BANK)
        checked_iso = checked_parsed.iso_str if checked_parsed else raw_checked.strip()

    if status == "unknown" and not any((verified_name, dob_iso, norm_country, doc_number)):
        return FactStatus.PENDING_CLUE, {}, quote, "unknown_kyc_status"

    validated = {
        "status": status,
        "verified_name": verified_name,
        "verified_dob": dob_iso,
        "verified_dob_raw": raw_dob or "",
        "document_country": norm_country,
        "document_number": doc_number,
        "checked_at": checked_iso,
        "evidence_quote": quote,
    }
    return FactStatus.VERIFIED, validated, quote, None


def _validate_residence_note(
    text: str, payload: dict[str, Any]
) -> tuple[FactStatus, dict[str, Any], str | None, str | None]:
    """Post-validate residence_note payload."""
    entries = payload.get("lived_countries")
    if not isinstance(entries, list):
        return FactStatus.PENDING_CLUE, {}, None, "invalid_lived_countries_type"

    if not entries:
        return FactStatus.VERIFIED, {"lived_countries": []}, None, None

    validated_entries: list[dict[str, str]] = []
    first_quote: str | None = None
    saw_rejected_entry = False

    for entry in entries:
        if not isinstance(entry, dict):
            return FactStatus.PENDING_CLUE, {}, None, "invalid_residence_entry"
        quote = entry.get("evidence_quote")
        if not _is_verbatim_quote(quote, text):
            return FactStatus.PENDING_CLUE, {}, None, "missing_or_non_verbatim_quote"
        if first_quote is None:
            first_quote = quote
        subject = entry.get("subject")
        if subject != "customer":
            saw_rejected_entry = True
            continue
        if _NEGATION_RE.search(quote) or _THIRD_PARTY_RE.search(quote):
            saw_rejected_entry = True
            continue
        raw_country = entry.get("country")
        norm_country = _normalize_country_value(raw_country)
        if norm_country is None:
            continue
        if not _is_country_grounded(raw_country, norm_country, text, quote):
            return FactStatus.PENDING_CLUE, {}, quote, "hallucinated_country"
        validated_entries.append(
            {
                "country": norm_country,
                "subject": "customer",
                "evidence_quote": quote,
            }
        )

    if not validated_entries and saw_rejected_entry:
        return FactStatus.PENDING_CLUE, {}, first_quote, "third_party_or_negated"

    return (
        FactStatus.VERIFIED,
        {"lived_countries": validated_entries},
        first_quote,
        None,
    )


def _validate_temporal_hit_payload(
    task: str, text: str, payload: dict[str, Any], context: dict[str, Any]
) -> tuple[FactStatus, dict[str, Any], str | None, str | None]:
    """Post-validate hit_p1_dob and sr_facts with code-side age arithmetic."""
    quote = payload.get("evidence_quote")
    if not _is_verbatim_quote(quote, text):
        return FactStatus.PENDING_CLUE, {}, None, "missing_or_non_verbatim_quote"

    subject = payload.get("subject")
    if subject != "hit":
        return FactStatus.PENDING_CLUE, {}, quote, "non_hit_subject"
    if _NEGATION_RE.search(quote) or _THIRD_PARTY_RE.search(quote):
        return FactStatus.PENDING_CLUE, {}, quote, "third_party_or_negated"

    possible_years: list[int] = []
    exact_dates: list[str] = []

    raw_birth_date = payload.get("birth_date")
    birth_date_str: str | None = None
    if raw_birth_date is not None:
        if not isinstance(raw_birth_date, str) or not raw_birth_date.strip():
            return FactStatus.PENDING_CLUE, {}, quote, "invalid_birth_date"
        birth_date_str = raw_birth_date.strip()
        alts = dates.parse_date_alternatives(birth_date_str, dates.HIT)
        if not alts or not _contains_number(quote, alts[0].year):
            return FactStatus.PENDING_CLUE, {}, quote, "ungrounded_birth_date"
        for alt in alts:
            if alt.year not in possible_years:
                possible_years.append(alt.year)
            if alt.iso_str not in exact_dates:
                exact_dates.append(alt.iso_str)

    raw_age = payload.get("age")
    valid_age: int | None = None
    ref_iso: str | None = None
    if raw_age is not None:
        if isinstance(raw_age, bool) or not isinstance(raw_age, int):
            return FactStatus.PENDING_CLUE, {}, quote, "invalid_age_type"
        if not (0 < raw_age < 130) or not _contains_number(quote, raw_age):
            return FactStatus.PENDING_CLUE, {}, quote, "ungrounded_age"
        raw_ref = payload.get("reference_date") or context.get("reference_date")
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            return FactStatus.PENDING_CLUE, {}, quote, "missing_reference_date"
        ref_alts = dates.parse_date_alternatives(raw_ref.strip(), dates.HIT)
        if not ref_alts:
            return FactStatus.PENDING_CLUE, {}, quote, "invalid_reference_date"
        ref_in_text = _contains_number(quote, ref_alts[0].year) or _contains_number(
            text, ref_alts[0].year
        )
        ctx_ref = str(context.get("reference_date") or "")
        ref_from_ctx = bool(ctx_ref) and _contains_number(ctx_ref, ref_alts[0].year)
        if not (ref_in_text or ref_from_ctx):
            return FactStatus.PENDING_CLUE, {}, quote, "ungrounded_reference_date"
        valid_age = raw_age
        ref_iso = ref_alts[0].iso_str
        for ref_alt in ref_alts:
            for yr in dates.birth_years_from_age(valid_age, ref_alt):
                if yr not in possible_years:
                    possible_years.append(yr)

    norm_country: str | None = None
    if task == TASK_SR_FACTS:
        raw_country = payload.get("country")
        norm_country = _normalize_country_value(raw_country)
        if norm_country is not None and not _is_country_grounded(
            raw_country, norm_country, text, quote
        ):
            return FactStatus.PENDING_CLUE, {}, quote, "hallucinated_country"

    if not possible_years and norm_country is None:
        return FactStatus.PENDING_CLUE, {}, quote, "no_extractable_fact"

    possible_years.sort()
    validated: dict[str, Any] = {
        "birth_date": birth_date_str,
        "age": valid_age,
        "reference_date": ref_iso,
        "possible_years": possible_years,
        "exact_dates": exact_dates,
        "subject": "hit",
        "evidence_quote": quote,
    }
    if task == TASK_SR_FACTS:
        validated["country"] = norm_country
    return FactStatus.VERIFIED, validated, quote, None


def validate_extraction(
    task: str,
    text: str,
    payload: Any,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[FactStatus, dict[str, Any], str | None, str | None]:
    """Run strict deterministic post-validation on parsed LLM JSON output."""
    if not isinstance(payload, dict):
        return FactStatus.PENDING_CLUE, {}, None, "non_dict_payload"
    ctx = context or {}
    if task == TASK_KYC_NOTE:
        return _validate_kyc_note(text, payload, ctx)
    if task == TASK_RESIDENCE_NOTE:
        return _validate_residence_note(text, payload)
    if task in (TASK_HIT_P1_DOB, TASK_SR_FACTS):
        return _validate_temporal_hit_payload(task, text, payload, ctx)
    return FactStatus.PENDING_CLUE, {}, None, "unsupported_task"


def _parse_retry_after(headers: Any) -> float | None:
    """Parse numeric Retry-After header seconds when present."""
    if headers is None:
        return None
    val = headers.get("Retry-After") if hasattr(headers, "get") else None
    if val is None:
        return None
    try:
        seconds = float(str(val).strip())
    except ValueError:
        return None
    return max(0.0, min(seconds, 60.0))


class OpenRouterClient:
    """Stdlib OpenRouter client implementing LLMFallback with caching and caps."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str | None = None,
        endpoint: str = OPENROUTER_URL,
        temperature: float = 0.0,
        top_p: float = 1.0,
        seed: int = 42,
        max_tokens: int = 256,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        max_calls: int = 200,
        max_total_tokens: int = 200_000,
        max_wall_clock_seconds: float = 1200.0,
        http_opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        env_model = os.environ.get("OPENROUTER_MODEL", "").strip()
        self.api_key = (api_key if api_key is not None else env_key).strip()
        self.model_id = (model_id if model_id is not None else env_model).strip() or DEFAULT_MODEL
        self.endpoint = endpoint
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_calls = max_calls
        self.max_total_tokens = max_total_tokens
        self.max_wall_clock_seconds = max_wall_clock_seconds
        self._http_opener = http_opener or urllib.request.urlopen
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._start_time = self._clock_fn()

        self._lock = threading.Lock()
        self._cache: dict[str, ExtractionResult] = {}
        self._calls_made = 0
        self._total_tokens = 0
        self._cache_hits = 0

    def stats(self) -> dict[str, Any]:
        """Return runtime metrics for reproducibility and cost logging."""
        with self._lock:
            return {
                "model_id": self.model_id,
                "prompt_version": PROMPT_VERSION,
                "calls_made": self._calls_made,
                "total_tokens": self._total_tokens,
                "cache_hits": self._cache_hits,
                "cache_size": len(self._cache),
                "elapsed_seconds": round(self._clock_fn() - self._start_time, 3),
            }

    def _cache_key(self, task: str, text: str, context: dict[str, Any]) -> str:
        """Build SHA-256 per-run cache key."""
        canonical_ctx = json.dumps(context, sort_keys=True, ensure_ascii=False)
        raw = f"{self.model_id}\0{PROMPT_VERSION}\0{task}\0{canonical_ctx}\0{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _pending_result(
        self, task: str, source_id: str, error: str
    ) -> ExtractionResult:
        """Build a standardized PENDING_CLUE degradation result."""
        return ExtractionResult(
            task=task,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            error=error,
        )

    def _post_chat(self, user_prompt: str) -> tuple[dict[str, Any], int]:
        """Execute one OpenRouter chat completion request with bounded retries."""
        body = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/FinCrimeHackathon",
            "X-Title": "EntityScreeningFallback",
        }

        last_err = "unknown_http_error"
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                self.endpoint, data=data, headers=headers, method="POST"
            )
            try:
                with self._http_opener(req, timeout=self.timeout_seconds) as resp:
                    raw_bytes = resp.read()
                parsed_resp = json.loads(raw_bytes.decode("utf-8"))
                usage = parsed_resp.get("usage") or {}
                tokens = int(usage.get("total_tokens") or 0)
                choices = parsed_resp.get("choices") or []
                if not choices:
                    raise ValueError("Missing choices in OpenRouter response")
                content = (choices[0].get("message") or {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty content in OpenRouter response")
                return _extract_json_object(content), tokens
            except urllib.error.HTTPError as exc:
                try:
                    code = exc.code
                    retry_after = _parse_retry_after(getattr(exc, "headers", None))
                finally:
                    exc.close()
                last_err = f"http_{code}"
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    delay = (
                        retry_after
                        if retry_after is not None
                        else min(1.0 * (2**attempt), 30.0)
                    )
                    self._sleep_fn(delay)
                    continue
                raise RuntimeError(last_err) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = f"network_error:{type(exc).__name__}"
                if attempt < self.max_retries:
                    self._sleep_fn(min(1.0 * (2**attempt), 30.0))
                    continue
                raise RuntimeError(last_err) from exc
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid_response:{exc}") from exc

        raise RuntimeError(last_err)

    def _run_task(
        self,
        task: str,
        text: str,
        source_id: str,
        *,
        context: dict[str, Any] | None = None,
        prompt_kwargs: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        """Execute cached, capped, post-validated extraction for one task."""
        if not self.api_key:
            return self._pending_result(task, source_id, "missing_api_key")
        if not isinstance(text, str) or not text.strip():
            return self._pending_result(task, source_id, "empty_text")

        ctx = context or {}
        cache_key = self._cache_key(task, text, ctx)

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return replace(cached, source_id=source_id, cached=True)
            if self._calls_made >= self.max_calls:
                return self._pending_result(task, source_id, "cap_max_calls")
            if self._total_tokens >= self.max_total_tokens:
                return self._pending_result(task, source_id, "cap_max_tokens")
            if (self._clock_fn() - self._start_time) >= self.max_wall_clock_seconds:
                return self._pending_result(task, source_id, "cap_wall_clock")
            self._calls_made += 1

        prompt = _build_prompt(task, text, **(prompt_kwargs or {}))
        try:
            raw_payload, tokens_used = self._post_chat(prompt)
        except RuntimeError as exc:
            result = self._pending_result(task, source_id, str(exc))
            with self._lock:
                self._cache[cache_key] = result
            return result

        status, validated_payload, quote, err = validate_extraction(
            task, text, raw_payload, context=ctx
        )
        result = ExtractionResult(
            task=task,
            source_id=source_id,
            status=status,
            payload=validated_payload,
            evidence_quote=quote,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            error=err,
            cached=False,
        )
        with self._lock:
            self._total_tokens += tokens_used
            self._cache[cache_key] = result
        return result

    def extract_kyc_note(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        """Extract KYC verification fields from an unstructured note."""
        return self._run_task(
            TASK_KYC_NOTE,
            text,
            source_id,
            context={"created_at": created_at},
            prompt_kwargs={"created_at": created_at},
        )

    def extract_residence_countries(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        """Extract customer residence countries from an unstructured support note."""
        return self._run_task(
            TASK_RESIDENCE_NOTE,
            text,
            source_id,
            context={"created_at": created_at},
            prompt_kwargs={"created_at": created_at},
        )

    def extract_hit_p1_dob(
        self, text: str, source_id: str, *, reference_date: str = ""
    ) -> ExtractionResult:
        """Extract birth date or age/reference date from hit profile paragraph 1."""
        return self._run_task(
            TASK_HIT_P1_DOB,
            text,
            source_id,
            context={"reference_date": reference_date},
            prompt_kwargs={"reference_date": reference_date},
        )

    def extract_sr_facts(
        self,
        text: str,
        source_id: str,
        *,
        category_phrase: str = "",
        hit_countries: tuple[str, ...] = (),
    ) -> ExtractionResult:
        """Extract age, birth date, reference date, and country from a search snippet."""
        countries_tuple = tuple(hit_countries)
        return self._run_task(
            TASK_SR_FACTS,
            text,
            source_id,
            context={
                "category_phrase": category_phrase,
                "hit_countries": list(countries_tuple),
            },
            prompt_kwargs={
                "category_phrase": category_phrase,
                "hit_countries": countries_tuple,
            },
        )

    def batch_extract(
        self, requests: Iterable[ExtractionRequest]
    ) -> list[ExtractionResult]:
        """Deduplicate and execute requests in deterministic sorted order."""
        ordered = sorted(
            requests,
            key=lambda r: (
                r.task,
                json.dumps(r.context, sort_keys=True, ensure_ascii=False),
                r.text,
                r.source_id,
            ),
        )
        results: list[ExtractionResult] = []
        for req in ordered:
            ctx = req.context
            if req.task == TASK_KYC_NOTE:
                res = self.extract_kyc_note(
                    req.text, req.source_id, created_at=str(ctx.get("created_at", ""))
                )
            elif req.task == TASK_RESIDENCE_NOTE:
                res = self.extract_residence_countries(
                    req.text, req.source_id, created_at=str(ctx.get("created_at", ""))
                )
            elif req.task == TASK_HIT_P1_DOB:
                res = self.extract_hit_p1_dob(
                    req.text,
                    req.source_id,
                    reference_date=str(ctx.get("reference_date", "")),
                )
            elif req.task == TASK_SR_FACTS:
                res = self.extract_sr_facts(
                    req.text,
                    req.source_id,
                    category_phrase=str(ctx.get("category_phrase", "")),
                    hit_countries=tuple(ctx.get("hit_countries") or ()),
                )
            else:
                res = self._pending_result(req.task, req.source_id, "unsupported_task")
            results.append(res)
        return results


def create_llm_fallback(**kwargs: Any) -> LLMFallback:
    """Return OpenRouterClient when OPENROUTER_API_KEY is available, else NullLLMFallback."""
    explicit_key = kwargs.get("api_key")
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    effective_key = explicit_key.strip() if isinstance(explicit_key, str) else env_key
    if effective_key:
        return OpenRouterClient(**kwargs)
    return NullLLMFallback()
