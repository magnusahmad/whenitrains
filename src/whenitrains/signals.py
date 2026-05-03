from __future__ import annotations

from enum import StrEnum

from .markets import Predicate, PredicateType


class DirectionalImpact(StrEnum):
    INCREASES_YES_PROBABILITY = "INCREASES_YES_PROBABILITY"
    DECREASES_YES_PROBABILITY = "DECREASES_YES_PROBABILITY"
    NO_MATERIAL_IMPACT = "NO_MATERIAL_IMPACT"


class PriceResponse(StrEnum):
    PRICE_MOVED_WITH_EVENT = "PRICE_MOVED_WITH_EVENT"
    PRICE_NOT_MOVED_WITH_EVENT = "PRICE_NOT_MOVED_WITH_EVENT"


def classify_directional_impact(
    predicate: Predicate, old_value: float, new_value: float, proximity_c: float = 1.0
) -> DirectionalImpact:
    if predicate.value_c is None or old_value == new_value:
        return DirectionalImpact.NO_MATERIAL_IMPACT
    target = predicate.value_c
    if min(abs(target - old_value), abs(target - new_value)) > proximity_c:
        return DirectionalImpact.NO_MATERIAL_IMPACT

    if predicate.type == PredicateType.EXACT_C:
        old_distance = abs(old_value - target)
        new_distance = abs(new_value - target)
        if new_distance < old_distance:
            return DirectionalImpact.INCREASES_YES_PROBABILITY
        if new_distance > old_distance:
            return DirectionalImpact.DECREASES_YES_PROBABILITY
        return DirectionalImpact.NO_MATERIAL_IMPACT

    if predicate.type == PredicateType.GTE_C:
        if new_value > old_value:
            return DirectionalImpact.INCREASES_YES_PROBABILITY
        return DirectionalImpact.DECREASES_YES_PROBABILITY

    if predicate.type == PredicateType.BOTTOM_BUCKET_LTE_C:
        if new_value < old_value:
            return DirectionalImpact.INCREASES_YES_PROBABILITY
        return DirectionalImpact.DECREASES_YES_PROBABILITY

    return DirectionalImpact.NO_MATERIAL_IMPACT


def classify_price_response(
    impact: DirectionalImpact,
    prior_yes_ask: float,
    current_yes_ask: float,
    min_move: float,
) -> PriceResponse:
    if impact == DirectionalImpact.NO_MATERIAL_IMPACT:
        return PriceResponse.PRICE_MOVED_WITH_EVENT
    if impact == DirectionalImpact.INCREASES_YES_PROBABILITY:
        moved = current_yes_ask - prior_yes_ask >= min_move
    else:
        moved = prior_yes_ask - current_yes_ask >= min_move
    return (
        PriceResponse.PRICE_MOVED_WITH_EVENT
        if moved
        else PriceResponse.PRICE_NOT_MOVED_WITH_EVENT
    )

