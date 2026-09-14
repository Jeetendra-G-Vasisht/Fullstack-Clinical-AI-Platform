"""Deterministic, dependency-free field-level similarity scorers.

Each matcher returns (score, confidence):
  score      -- how well the two values agree, 0.0-1.0, or None if the
                field can't be evaluated at all (missing/unparseable on
                either side).
  confidence -- how reliable this *kind* of field is as evidence, not how
                good the observed score looks. An identifier match is
                near-decisive by construction; a name match is not (common
                names collide by coincidence), so it gets a lower ceiling
                even when the observed score is high. Confidence is 0.0
                whenever score is None -- an unusable field contributes no
                evidence either way.

This mirrors classic record-linkage scoring (e.g. Fellegi-Sunter): fixed
per-field reliability weights combined with an observed agreement score.
"""
from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher
from typing import Optional

FIELD_RELIABILITY = {
    "name": 0.65,
    "dob": 0.85,
    "identifier": 0.97,
}

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y")


def _normalize_name(name: str) -> str:
    name = re.sub(r"[^a-z\s]", " ", (name or "").lower())
    return " ".join(name.split())


def name_score(a: str, b: str) -> tuple[Optional[float], float]:
    norm_a, norm_b = _normalize_name(a), _normalize_name(b)
    if not norm_a or not norm_b:
        return None, 0.0
    direct = SequenceMatcher(None, norm_a, norm_b).ratio()
    # Also compare with tokens sorted, so "Smith John" vs "John Smith"
    # (common in OCR'd forms) doesn't get penalized for word order.
    reordered = SequenceMatcher(
        None, " ".join(sorted(norm_a.split())), " ".join(sorted(norm_b.split()))
    ).ratio()
    return max(direct, reordered), FIELD_RELIABILITY["name"]


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            from datetime import datetime
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def dob_score(a: str, b: str) -> tuple[Optional[float], float]:
    date_a, date_b = _parse_date(a), _parse_date(b)
    if date_a is None or date_b is None:
        return None, 0.0
    if date_a == date_b:
        return 1.0, FIELD_RELIABILITY["dob"]
    # Day/month transposition is the single most common manual-entry error
    # (e.g. 03/07 typed for 07/03) -- worth partial credit rather than a
    # flat zero, since it's evidence *for* a match, just imperfect evidence.
    try:
        swapped = date_a.replace(month=date_a.day, day=date_a.month)
        if swapped == date_b:
            return 0.6, FIELD_RELIABILITY["dob"]
    except ValueError:
        pass
    if date_a.year == date_b.year:
        return 0.25, FIELD_RELIABILITY["dob"]
    return 0.0, FIELD_RELIABILITY["dob"]


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (value or "")).upper()


def identifier_score(a: str, b: str) -> tuple[Optional[float], float]:
    norm_a, norm_b = _normalize_identifier(a), _normalize_identifier(b)
    if not norm_a or not norm_b:
        return None, 0.0
    if norm_a == norm_b:
        return 1.0, FIELD_RELIABILITY["identifier"]
    # A near-miss (single transposed/mistyped character) is still weak
    # positive evidence; anything looser than that is treated as a clash,
    # not just an absence of evidence.
    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    return (0.5 if ratio > 0.8 else 0.0), FIELD_RELIABILITY["identifier"]


MATCHERS = {
    "name": name_score,
    "dob": dob_score,
    "identifier": identifier_score,
}
