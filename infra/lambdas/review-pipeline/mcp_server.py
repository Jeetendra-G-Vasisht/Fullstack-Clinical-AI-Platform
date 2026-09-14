"""
kBuddhi AI — review-pipeline MCP server
-------------------------------------------------------------
Exposes the multi-agent review/matching pipeline as a single MCP tool so
it can be invoked the same way by any MCP-speaking caller (an agent
framework, an IDE assistant, a CLI) regardless of transport -- rather than
every caller writing its own ad-hoc wrapper around lambda_function.py.

Both this server and lambda_function.py call the exact same
pipeline.run_review(), so behavior is identical between the two transports
by construction, not by convention.

Run:
    python mcp_server.py                # stdio transport (default)
    python mcp_server.py --transport sse --port 8080

Local dev / MCP Inspector:
    mcp dev mcp_server.py
"""
from __future__ import annotations

import argparse
from typing import Any

from mcp.server.fastmcp import FastMCP

from pipeline import run_review

mcp = FastMCP(
    name="kbuddhi-review-pipeline",
    instructions=(
        "Matches an extracted document record against a pool of candidate "
        "patient records using a multi-agent (name/DOB/identifier) review "
        "pipeline. Use review_match for the full decision + audit trace, "
        "or list_review_thresholds to see the pipeline's tunable constants."
    ),
)


@mcp.tool(
    name="review_match",
    description=(
        "Run the review/matching pipeline on one document record against a pool "
        "of candidate records. Returns the final decision -- 'matched' (with the "
        "matched patient_id), 'no_match' (no candidate resembles the document), or "
        "'needs_human_review' (a plausible best guess exists but confidence is too "
        "low to auto-resolve, per the pipeline's self-check gate) -- plus the full "
        "per-stage trace so the decision can be audited back to the exact worker "
        "and field that drove it."
    ),
)
def review_match(
    document: dict[str, Any],
    candidates: list[dict[str, Any]],
    gate_enabled: bool = True,
) -> dict[str, Any]:
    """
    Args:
        document: {"doc_id": str, "name": str, "dob": str, "mrn": str}.
            Any missing field is treated as unusable evidence, not a crash.
        candidates: non-empty list of {"patient_id": str, "name": str,
            "dob": str, "mrn": str} records to match the document against.
        gate_enabled: when True (default), low-confidence best guesses are
            routed to needs_human_review instead of auto-resolved. Set to
            False only to see what the pipeline would have committed to
            without the self-check gate -- this is a diagnostic, not a
            safe default for production matching.

    Returns:
        {"final": {...decision...}, "trace": [...per-stage log entries...]}
    """
    if not candidates:
        raise ValueError("candidates must be a non-empty list")
    result = run_review(document, candidates, gate_enabled=gate_enabled)
    return {"final": result["final"], "trace": result["trace"]}


@mcp.tool(
    name="list_review_thresholds",
    description="Return the pipeline's tunable constants (field weights, gate threshold, "
                "no-match floor) so a caller can explain or reason about a decision "
                "without hardcoding the pipeline's internals.",
)
def list_review_thresholds() -> dict[str, Any]:
    from pipeline import nodes

    return {
        "field_weights": nodes.FIELD_WEIGHTS,
        "no_match_score_floor": nodes.NO_MATCH_SCORE_FLOOR,
        "gate_confidence_threshold": nodes.GATE_CONFIDENCE_THRESHOLD,
        "margin_scale": nodes.MARGIN_SCALE,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="kBuddhi AI review-pipeline MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport != "stdio":
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
