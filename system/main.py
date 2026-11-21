"""Baseline CLI entrypoint and screening pipeline runner."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Callable

try:
    from .contracts import DECISION_REVIEW, HitDecision
except ImportError:
    from contracts import DECISION_REVIEW, HitDecision

INSUFFICIENT_DATA_CODE = "INSUFFICIENT_DATA"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the screening runner."""
    parser = argparse.ArgumentParser(
        prog="system/run",
        description="Resolve entity-screening hits from a data directory.",
    )
    parser.add_argument("data_dir", help="Path to the input data folder")
    parser.add_argument("output_file", help="Path to the output JSONL file")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Re-raise malformed input or per-hit errors instead of isolating them.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable the OpenRouter fallback even when OPENROUTER_API_KEY is set.",
    )
    return parser.parse_args(argv)


def _clean_text(value: Any, default: str) -> str:
    """Return stripped text when non-empty, otherwise a fallback string."""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _require_non_empty_id(row: dict[str, Any], key: str) -> str:
    """Extract a required non-empty string identifier from a hit mapping."""
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid required field {key!r}")
    return value.strip()


def load_hits(data_dir: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """Load hit records from `<data_dir>/hits.jsonl`."""
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Data directory does not exist: {data_dir}")

    hits_path = os.path.join(data_dir, "hits.jsonl")
    if not os.path.isfile(hits_path):
        raise FileNotFoundError(f"Required input file not found: {hits_path}")

    hits: list[dict[str, Any]] = []
    with open(hits_path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if strict:
                    raise ValueError(
                        f"Malformed JSON at {hits_path}:{line_no}: {exc}"
                    ) from exc
                sys.stderr.write(
                    f"WARNING: skipping malformed JSON at {hits_path}:{line_no}: {exc}\n"
                )
                continue
            if not isinstance(record, dict):
                if strict:
                    raise ValueError(
                        f"Expected JSON object at {hits_path}:{line_no}, got {type(record).__name__}"
                    )
                sys.stderr.write(
                    f"WARNING: skipping non-object row at {hits_path}:{line_no}\n"
                )
                continue
            hits.append(record)
    return hits


def evaluate_hit_baseline(hit: dict[str, Any]) -> HitDecision:
    """Return a conservative REVIEW baseline decision for a single hit."""
    hit_id = _require_non_empty_id(hit, "hit_id")
    alert_id = _require_non_empty_id(hit, "alert_id")
    profile_name = _clean_text(hit.get("name"), "unnamed profile")
    entity_type = _clean_text(hit.get("entity_type"), "Unknown")

    reason = (
        f"Hit {hit_id} under alert {alert_id} ({entity_type} profile '{profile_name}') "
        f"was evaluated in baseline mode without automated customer resolution or contradiction checks. "
        f"Because no verified or declared contradiction was established, the hit is routed to manual review."
    )
    return HitDecision(
        hit_id=hit_id,
        alert_id=alert_id,
        decision=DECISION_REVIEW,
        reason_code=INSUFFICIENT_DATA_CODE,
        customer_record_id=None,
        reason=reason,
        evidence=(),
    )


def _fallback_review_decision(hit: dict[str, Any], exc: Exception) -> HitDecision:
    """Build a safe REVIEW decision when evaluating a hit raises an error."""
    hit_id = _require_non_empty_id(hit, "hit_id")
    alert_id = _require_non_empty_id(hit, "alert_id")
    reason = (
        f"Hit {hit_id} under alert {alert_id} encountered an unexpected processing error "
        f"({type(exc).__name__}) during screening evaluation. "
        f"To avoid any unsafe clearance, the hit is routed to manual review."
    )
    return HitDecision(
        hit_id=hit_id,
        alert_id=alert_id,
        decision=DECISION_REVIEW,
        reason_code=INSUFFICIENT_DATA_CODE,
        customer_record_id=None,
        reason=reason,
        evidence=(),
    )


def _hit_sort_key(hit: dict[str, Any]) -> tuple[str, str]:
    """Return a deterministic sort key independent of input row ordering."""
    raw_hit_id = hit.get("hit_id")
    raw_alert_id = hit.get("alert_id")
    hit_id = "" if raw_hit_id is None else str(raw_hit_id)
    alert_id = "" if raw_alert_id is None else str(raw_alert_id)
    return (hit_id, alert_id)


def process_hits(
    hits: list[dict[str, Any]],
    *,
    strict: bool = False,
    evaluator: Callable[[dict[str, Any]], HitDecision] = evaluate_hit_baseline,
) -> list[HitDecision]:
    """Evaluate hits deterministically with per-hit error isolation."""
    decisions: list[HitDecision] = []
    for hit in sorted(hits, key=_hit_sort_key):
        try:
            decisions.append(evaluator(hit))
        except Exception as exc:
            if strict:
                raise
            raw_hit_id = hit.get("hit_id")
            sys.stderr.write(f"ERROR evaluating hit {raw_hit_id!r}: {exc}\n")
            try:
                decisions.append(_fallback_review_decision(hit, exc))
            except ValueError as id_exc:
                sys.stderr.write(
                    f"ERROR skipping unidentifiable hit record: {id_exc}\n"
                )
    return decisions


def write_jsonl_atomic(decisions: list[HitDecision], output_file: str) -> None:
    """Write decision rows as UTF-8 LF JSONL via temp file and atomic replace."""
    parent = os.path.dirname(output_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    target_dir = parent or "."

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target_dir,
            prefix=".tmp_screening_",
            suffix=".jsonl",
            delete=False,
        ) as handle:
            tmp_path = handle.name
            for decision in decisions:
                handle.write(json.dumps(decision.to_dict(), ensure_ascii=False))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_file)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _import_pipeline():
    """Import the pipeline modules lazily so the baseline helpers stay importable alone."""
    try:
        from . import customer, decision, explain, llm_fallback, watchlist
        from .data import load_dataset
    except ImportError:
        import customer, decision, explain, llm_fallback, watchlist  # type: ignore
        from data import load_dataset  # type: ignore
    return customer, decision, explain, llm_fallback, watchlist, load_dataset


def build_evaluator(data_dir: str, *, strict: bool = False, use_llm: bool = True):
    """Load the data folder once and return (evaluator, hits, llm) for the full pipeline.

    The evaluator resolves each alert's candidate customers once (cached per alert),
    resolves the watchlist hit, applies the SOP decision engine and formats the
    output record.
    """
    customer, decision, explain, llm_fallback, watchlist, load_dataset = _import_pipeline()
    dataset = load_dataset(data_dir, strict=strict)
    llm = llm_fallback.create_llm_fallback() if use_llm else llm_fallback.NullLLMFallback()
    resolver = customer.CustomerResolver(dataset, llm_fallback=llm)
    variants = resolver.variants
    alert_cache: dict[str, tuple[Any, ...]] = {}

    def candidates_for(alert_id: str) -> tuple[Any, ...]:
        cached = alert_cache.get(alert_id)
        if cached is None:
            alert = dataset.alerts_by_id.get(alert_id)
            cached = resolver.resolve_alert(alert) if alert is not None else ()
            alert_cache[alert_id] = cached
        return cached

    def evaluate(hit: dict[str, Any]) -> HitDecision:
        hit_id = _require_non_empty_id(hit, "hit_id")
        alert_id = _require_non_empty_id(hit, "alert_id")
        alert = dataset.alerts_by_id.get(alert_id) or {}
        screened = alert.get("customer") or {}
        resolved, _detail = watchlist.resolve_hit(hit, dataset, llm)
        outcome = decision.decide(
            resolved,
            candidates_for(alert_id),
            alert.get("screened_at"),
            variants,
            screened.get("entity_type"),
        )
        result = explain.to_hit_decision(outcome, resolved)
        if result.hit_id != hit_id or result.alert_id != alert_id:
            raise ValueError(f"pipeline returned mismatched ids for hit {hit_id!r}")
        return result

    return evaluate, list(dataset.hits), llm


def run_pipeline(data_dir: str, output_file: str, *, strict: bool = False, use_llm: bool = True) -> int:
    """Load data, run screening evaluation, and write output atomically."""
    evaluator, hits, llm = build_evaluator(data_dir, strict=strict, use_llm=use_llm)
    decisions = process_hits(hits, strict=strict, evaluator=evaluator)
    write_jsonl_atomic(decisions, output_file)
    stats = getattr(llm, "stats", None)
    if callable(stats):
        try:
            sys.stderr.write(f"LLM stats: {json.dumps(stats(), sort_keys=True, default=str)}\n")
        except Exception:  # stats are diagnostic only
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point returning POSIX process exit status."""
    args = parse_args(argv)
    try:
        return run_pipeline(
            args.data_dir, args.output_file, strict=args.strict, use_llm=not args.no_llm
        )
    except OSError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
