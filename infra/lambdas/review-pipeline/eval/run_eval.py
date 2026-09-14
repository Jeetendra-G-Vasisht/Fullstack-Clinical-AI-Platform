"""
Evaluation harness: baseline (no self-check gate) vs gated, on dataset.jsonl.

Baseline  -- gate_enabled=False. The pipeline always commits to its best
             candidate, however weak the evidence. Metric: plain accuracy
             over all cases (a "no_match" ground truth is always counted
             wrong here, since baseline never abstains).

Gated     -- gate_enabled=True. Cases below the confidence threshold are
             routed to needs_human_review instead of auto-resolved.
             Metrics: accuracy on the auto-resolved subset only, and the
             escalation rate (fraction routed to human review).

No LLM calls, no network, no AWS -- this only exercises the deterministic
pipeline in pipeline/, so the numbers below are exactly reproducible by
re-running this script against the committed dataset.jsonl.

Usage: python run_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline import run_review  # noqa: E402


def load_dataset() -> list[dict]:
    path = Path(__file__).parent / "dataset.jsonl"
    with path.open() as f:
        return [json.loads(line) for line in f]


def run_baseline(cases: list[dict]) -> dict:
    correct = 0
    per_case = []
    for case in cases:
        result = run_review(case["document"], case["candidates"], gate_enabled=False)
        final = result["final"]
        is_correct = final["patient_id"] == case["expected_patient_id"]
        correct += is_correct
        per_case.append({
            "case_id": case["case_id"], "category": case["category"],
            "expected": case["expected_patient_id"], "got": final["patient_id"],
            "status": final["status"], "confidence": final["confidence"], "correct": is_correct,
        })
    return {
        "mode": "baseline_no_gate",
        "total_cases": len(cases),
        "accuracy": correct / len(cases) if cases else 0.0,
        "per_case": per_case,
    }


def run_gated(cases: list[dict]) -> dict:
    auto_resolved_correct = 0
    auto_resolved_total = 0
    escalated_total = 0
    escalated_would_have_been_wrong = 0
    per_case = []
    for case in cases:
        result = run_review(case["document"], case["candidates"], gate_enabled=True)
        final = result["final"]
        expected = case["expected_patient_id"]

        if final["status"] == "needs_human_review":
            escalated_total += 1
            # Diagnostic only: would the pipeline's own best guess (the
            # candidate it would have committed to without the gate) have
            # been correct? Shows whether escalation is targeting genuinely
            # hard cases rather than firing at random.
            would_have_been_correct = final["patient_id"] == expected
            escalated_would_have_been_wrong += (not would_have_been_correct)
            is_correct = None
        else:
            auto_resolved_total += 1
            is_correct = final["patient_id"] == expected
            auto_resolved_correct += bool(is_correct)

        per_case.append({
            "case_id": case["case_id"], "category": case["category"],
            "expected": expected, "got": final["patient_id"],
            "status": final["status"], "confidence": final["confidence"], "correct": is_correct,
        })

    return {
        "mode": "gated_self_check",
        "total_cases": len(cases),
        "auto_resolved_count": auto_resolved_total,
        "accuracy_on_auto_resolved": (auto_resolved_correct / auto_resolved_total) if auto_resolved_total else None,
        "escalated_count": escalated_total,
        "escalation_rate": escalated_total / len(cases) if cases else 0.0,
        "escalated_would_have_been_wrong_fraction": (
            escalated_would_have_been_wrong / escalated_total if escalated_total else None
        ),
        "per_case": per_case,
    }


def summarize_by_category(per_case: list[dict]) -> dict:
    by_cat: dict[str, dict] = {}
    for row in per_case:
        cat = by_cat.setdefault(row["category"], {"total": 0, "correct": 0, "escalated": 0})
        cat["total"] += 1
        if row["status"] == "needs_human_review":
            cat["escalated"] += 1
        elif row["correct"]:
            cat["correct"] += 1
    return by_cat


def main() -> None:
    cases = load_dataset()
    baseline = run_baseline(cases)
    gated = run_gated(cases)

    print(f"Dataset: {len(cases)} cases\n")

    print("=== Baseline (no self-check gate) ===")
    print(f"Accuracy: {baseline['accuracy']:.1%} ({sum(r['correct'] for r in baseline['per_case'])}/{baseline['total_cases']})")
    print("By category:")
    for cat, stats in summarize_by_category(baseline["per_case"]).items():
        print(f"  {cat:15s} {stats['correct']}/{stats['total']} correct")

    print("\n=== Gated (self-check gate enabled) ===")
    print(f"Auto-resolved: {gated['auto_resolved_count']}/{gated['total_cases']}")
    if gated["accuracy_on_auto_resolved"] is not None:
        print(f"Accuracy on auto-resolved: {gated['accuracy_on_auto_resolved']:.1%}")
    print(f"Escalated to human review: {gated['escalated_count']}/{gated['total_cases']} ({gated['escalation_rate']:.1%})")
    if gated["escalated_would_have_been_wrong_fraction"] is not None:
        print(f"Of escalated cases, would-have-been-wrong if forced to guess: "
              f"{gated['escalated_would_have_been_wrong_fraction']:.1%}")
    print("By category:")
    for cat, stats in summarize_by_category(gated["per_case"]).items():
        print(f"  {cat:15s} {stats['correct']} correct / {stats['escalated']} escalated / {stats['total']} total")

    out_path = Path(__file__).parent / "results.json"
    with out_path.open("w") as f:
        json.dump({"baseline": baseline, "gated": gated}, f, indent=2)
    print(f"\nFull per-case results written to {out_path}")


if __name__ == "__main__":
    main()
