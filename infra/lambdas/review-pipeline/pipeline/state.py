"""LangGraph state schema for the review/matching pipeline."""
from __future__ import annotations

from typing import Literal, Optional, TypedDict


class DocumentRecord(TypedDict):
    doc_id: str
    name: str
    dob: str
    mrn: str


class CandidateRecord(TypedDict):
    patient_id: str
    name: str
    dob: str
    mrn: str


class FieldMatch(TypedDict):
    score: Optional[float]
    confidence: float


class CandidateAggregate(TypedDict):
    patient_id: str
    composite_score: float
    evidence_fraction: float
    fields: dict  # field_name -> FieldMatch


class Aggregate(TypedDict):
    ranked: list  # list[CandidateAggregate], best first
    best_score: float
    second_best_score: float
    margin: float
    overall_confidence: float


class FinalResult(TypedDict):
    status: Literal["matched", "no_match", "needs_human_review"]
    patient_id: Optional[str]
    score: float
    confidence: float
    reason: str
    weakest_link: Optional[str]


class ReviewState(TypedDict, total=False):
    document: DocumentRecord
    candidates: list  # list[CandidateRecord]
    gate_enabled: bool

    subtasks: list
    worker_results: dict  # worker_name -> {patient_id: FieldMatch}
    worker_confidence: dict  # worker_name -> float, 0 if field unusable for every candidate

    aggregate: Aggregate
    trace: list
    final: FinalResult
