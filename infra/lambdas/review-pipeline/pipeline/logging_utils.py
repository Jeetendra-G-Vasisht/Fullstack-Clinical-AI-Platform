"""Structured per-stage tracing.

Every node appends one entry here instead of the state as a whole being
overwritten, so a bad final answer can always be walked back to the exact
node/field that produced the weak evidence -- not just "the pipeline was
wrong somewhere".
"""
from __future__ import annotations

from typing import Any


def append(trace: list, stage: str, node: str, **fields: Any) -> list:
    entry = {"step": len(trace) + 1, "stage": stage, "node": node, **fields}
    return [*trace, entry]
