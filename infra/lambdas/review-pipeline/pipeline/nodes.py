"""Supervisor + specialized worker nodes for the review/matching graph.

Task shape: given one extracted document record and a pool of candidate
patient records, decide which candidate the document belongs to.

  supervisor_decompose  -- breaks "match this document" into one subtask
                            per identifying field.
  name_matcher /
  dob_matcher /
  identifier_matcher     -- specialized workers; each scores the document
                            against every candidate on exactly one field
                            and reports its own confidence.
  supervisor_aggregate    -- combines worker output into a ranked
                            candidate list and one overall confidence.
  route_after_aggregate   -- the self-check gate: decides whether the case
                            is auto-resolved or escalated.
  resolve_match /
  resolve_no_match /
  needs_human_review      -- terminal nodes that set the final result.
"""
from __future__ import annotations

from . import matchers
from .logging_utils import append

FIELD_WEIGHTS = {"name": 0.40, "dob": 0.30, "identifier": 0.30}

# Worker/weight names use the semantic field ("identifier"); the record
# schema uses its concrete key ("mrn"). Keep that mapping in one place.
FIELD_RECORD_KEY = {"name": "name", "dob": "dob", "identifier": "mrn"}

# Below this composite score, no candidate resembles the document closely
# enough to call it a match at all -- independent of the confidence gate.
NO_MATCH_SCORE_FLOOR = 0.35

# Below this overall confidence, an otherwise-plausible best guess is not
# auto-resolved; it's routed to a human reviewer instead of committed to.
GATE_CONFIDENCE_THRESHOLD = 0.70

# A margin of this size or more between the best and second-best candidate
# is treated as fully decisive; smaller margins scale confidence down,
# since a close second candidate means the "best" one could easily be wrong.
MARGIN_SCALE = 0.25


def supervisor_decompose(state: dict) -> dict:
    subtasks = list(FIELD_WEIGHTS.keys())
    trace = append(
        state.get("trace", []), "supervisor", "supervisor_decompose",
        subtasks=subtasks, candidate_count=len(state["candidates"]),
    )
    return {"subtasks": subtasks, "trace": trace}


def _make_field_worker(field: str):
    matcher_fn = matchers.MATCHERS[field]

    record_key = FIELD_RECORD_KEY[field]

    def worker(state: dict) -> dict:
        document = state["document"]
        results = {}
        confidences = []
        for candidate in state["candidates"]:
            score, confidence = matcher_fn(document.get(record_key, ""), candidate.get(record_key, ""))
            results[candidate["patient_id"]] = {"score": score, "confidence": confidence}
            confidences.append(confidence)

        worker_results = dict(state.get("worker_results", {}))
        worker_results[field] = results
        worker_confidence = dict(state.get("worker_confidence", {}))
        # A worker's own confidence is the same regardless of candidate --
        # it reflects whether THIS field was usable at all (present/parseable
        # on the document side), not how any one candidate happened to score.
        worker_confidence[field] = max(confidences) if confidences else 0.0

        trace = append(
            state.get("trace", []), "worker", f"{field}_matcher",
            field=field, per_candidate=results, worker_confidence=worker_confidence[field],
        )
        return {
            "worker_results": worker_results,
            "worker_confidence": worker_confidence,
            "trace": trace,
        }

    worker.__name__ = f"{field}_matcher"
    return worker


name_matcher = _make_field_worker("name")
dob_matcher = _make_field_worker("dob")
identifier_matcher = _make_field_worker("identifier")


def supervisor_aggregate(state: dict) -> dict:
    worker_results = state["worker_results"]
    ranked = []
    for candidate in state["candidates"]:
        pid = candidate["patient_id"]
        fields = {}
        weighted_sum = 0.0
        weight_used = 0.0
        for field, weight in FIELD_WEIGHTS.items():
            field_match = worker_results.get(field, {}).get(pid, {"score": None, "confidence": 0.0})
            fields[field] = field_match
            if field_match["score"] is not None:
                weighted_sum += field_match["score"] * weight
                weight_used += weight
        composite_score = (weighted_sum / weight_used) if weight_used > 0 else 0.0
        evidence_fraction = weight_used / sum(FIELD_WEIGHTS.values())
        ranked.append({
            "patient_id": pid,
            "composite_score": composite_score,
            "evidence_fraction": evidence_fraction,
            "fields": fields,
        })

    ranked.sort(key=lambda c: c["composite_score"], reverse=True)
    best = ranked[0] if ranked else None
    best_score = best["composite_score"] if best else 0.0
    second_best_score = ranked[1]["composite_score"] if len(ranked) > 1 else 0.0
    margin = best_score - second_best_score

    margin_factor = min(1.0, margin / MARGIN_SCALE) if MARGIN_SCALE > 0 else 1.0
    evidence_factor = best["evidence_fraction"] if best else 0.0
    overall_confidence = best_score * margin_factor * evidence_factor if best else 0.0

    aggregate = {
        "ranked": ranked,
        "best_score": best_score,
        "second_best_score": second_best_score,
        "margin": margin,
        "overall_confidence": overall_confidence,
    }
    trace = append(
        state.get("trace", []), "supervisor", "supervisor_aggregate",
        best_candidate=best["patient_id"] if best else None,
        best_score=best_score, margin=margin, overall_confidence=overall_confidence,
    )
    return {"aggregate": aggregate, "trace": trace}


def _weakest_link(state: dict) -> str | None:
    """Which worker contributed the least usable evidence to this decision --
    the first place a human should look if the final answer turns out wrong."""
    worker_confidence = state.get("worker_confidence", {})
    if not worker_confidence:
        return None
    return min(worker_confidence, key=worker_confidence.get)


def route_after_aggregate(state: dict) -> str:
    aggregate = state["aggregate"]
    if not state.get("gate_enabled", True):
        # Baseline mode: commit to the best candidate no matter how weak
        # the evidence is. This path exists to measure the risk of NOT
        # gating, not because it's a good default.
        return "resolve_match"
    if aggregate["best_score"] < NO_MATCH_SCORE_FLOOR:
        return "resolve_no_match"
    if aggregate["overall_confidence"] < GATE_CONFIDENCE_THRESHOLD:
        return "needs_human_review"
    return "resolve_match"


def resolve_match(state: dict) -> dict:
    aggregate = state["aggregate"]
    best = aggregate["ranked"][0]
    final = {
        "status": "matched",
        "patient_id": best["patient_id"],
        "score": best["composite_score"],
        "confidence": aggregate["overall_confidence"],
        "reason": "Best-scoring candidate" + ("" if state.get("gate_enabled", True)
                                                else " (gate disabled -- committed without a confidence check)"),
        "weakest_link": _weakest_link(state),
    }
    trace = append(state.get("trace", []), "resolve", "resolve_match", **final)
    return {"final": final, "trace": trace}


def resolve_no_match(state: dict) -> dict:
    aggregate = state["aggregate"]
    final = {
        "status": "no_match",
        "patient_id": None,
        "score": aggregate["best_score"],
        "confidence": aggregate["overall_confidence"],
        "reason": f"Best candidate score {aggregate['best_score']:.2f} is below the "
                  f"no-match floor ({NO_MATCH_SCORE_FLOOR}) -- no candidate resembles this document.",
        "weakest_link": _weakest_link(state),
    }
    trace = append(state.get("trace", []), "resolve", "resolve_no_match", **final)
    return {"final": final, "trace": trace}


def needs_human_review(state: dict) -> dict:
    aggregate = state["aggregate"]
    best = aggregate["ranked"][0]
    final = {
        "status": "needs_human_review",
        "patient_id": best["patient_id"],  # best guess, offered for triage -- NOT auto-resolved
        "score": best["composite_score"],
        "confidence": aggregate["overall_confidence"],
        "reason": f"Overall confidence {aggregate['overall_confidence']:.2f} is below the "
                  f"auto-resolve threshold ({GATE_CONFIDENCE_THRESHOLD}); margin over the "
                  f"next candidate was {aggregate['margin']:.2f}.",
        "weakest_link": _weakest_link(state),
    }
    trace = append(state.get("trace", []), "resolve", "needs_human_review", **final)
    return {"final": final, "trace": trace}
