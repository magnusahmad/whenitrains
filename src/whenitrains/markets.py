from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PredicateType(StrEnum):
    EXACT_C = "EXACT_C"
    GTE_C = "GTE_C"
    BOTTOM_BUCKET_LTE_C = "BOTTOM_BUCKET_LTE_C"
    OTHER = "OTHER"


@dataclass(frozen=True)
class Predicate:
    type: PredicateType
    value_c: int | None
    label: str


def parse_outcome_label(label: str) -> Predicate:
    normalized = label.strip()
    value_match = re.search(r"(-?\d+)\s*°?\s*C", normalized, re.IGNORECASE)
    if not value_match:
        return Predicate(PredicateType.OTHER, None, normalized)
    value = int(value_match.group(1))
    lower = normalized.lower()
    if "or higher" in lower:
        return Predicate(PredicateType.GTE_C, value, normalized)
    if "or below" in lower:
        return Predicate(PredicateType.BOTTOM_BUCKET_LTE_C, value, normalized)
    return Predicate(PredicateType.EXACT_C, value, normalized)


def predicate_matches(predicate: Predicate, official_max_c: float) -> bool:
    if predicate.value_c is None:
        return False
    if predicate.type == PredicateType.EXACT_C:
        return predicate.value_c <= official_max_c < predicate.value_c + 1
    if predicate.type == PredicateType.GTE_C:
        return official_max_c >= predicate.value_c
    if predicate.type == PredicateType.BOTTOM_BUCKET_LTE_C:
        return official_max_c < predicate.value_c + 1
    return False

