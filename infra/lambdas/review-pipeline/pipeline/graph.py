"""Builds the LangGraph StateGraph wiring supervisor + worker nodes together.

    supervisor_decompose
            |
      name_matcher -> dob_matcher -> identifier_matcher   (specialized workers)
            |
    supervisor_aggregate
            |
    route_after_aggregate  (the self-check gate)
       /        |         \\
resolve_match  resolve_no_match  needs_human_review
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from . import nodes
from .state import ReviewState


def build_graph():
    graph = StateGraph(ReviewState)

    graph.add_node("supervisor_decompose", nodes.supervisor_decompose)
    graph.add_node("name_matcher", nodes.name_matcher)
    graph.add_node("dob_matcher", nodes.dob_matcher)
    graph.add_node("identifier_matcher", nodes.identifier_matcher)
    graph.add_node("supervisor_aggregate", nodes.supervisor_aggregate)
    graph.add_node("resolve_match", nodes.resolve_match)
    graph.add_node("resolve_no_match", nodes.resolve_no_match)
    graph.add_node("needs_human_review", nodes.needs_human_review)

    graph.set_entry_point("supervisor_decompose")
    graph.add_edge("supervisor_decompose", "name_matcher")
    graph.add_edge("name_matcher", "dob_matcher")
    graph.add_edge("dob_matcher", "identifier_matcher")
    graph.add_edge("identifier_matcher", "supervisor_aggregate")

    graph.add_conditional_edges(
        "supervisor_aggregate",
        nodes.route_after_aggregate,
        {
            "resolve_match": "resolve_match",
            "resolve_no_match": "resolve_no_match",
            "needs_human_review": "needs_human_review",
        },
    )
    graph.add_edge("resolve_match", END)
    graph.add_edge("resolve_no_match", END)
    graph.add_edge("needs_human_review", END)

    return graph.compile()


_COMPILED_GRAPH = None


def run_review(document: dict, candidates: list, gate_enabled: bool = True) -> dict:
    """Runs the pipeline once and returns the final state (result + full trace)."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        _COMPILED_GRAPH = build_graph()

    initial_state: ReviewState = {
        "document": document,
        "candidates": candidates,
        "gate_enabled": gate_enabled,
        "trace": [],
        "worker_results": {},
        "worker_confidence": {},
    }
    return _COMPILED_GRAPH.invoke(initial_state)
