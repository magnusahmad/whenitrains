from __future__ import annotations

from dataclasses import dataclass

from .polymarket import Outcome
from .signals import (
    DirectionalImpact,
    PriceResponse,
    classify_directional_impact,
    classify_price_response,
)


@dataclass(frozen=True)
class TradeCandidate:
    outcome: Outcome
    side: str
    impact: DirectionalImpact
    price_response: PriceResponse
    prior_yes_ask: float
    current_yes_ask: float
    reason: str


def build_trade_candidates(
    outcomes: list[Outcome],
    old_forecast_max_c: float,
    new_forecast_max_c: float,
    prior_yes_asks: dict[str, float],
    current_yes_asks: dict[str, float],
    min_move: float,
) -> list[TradeCandidate]:
    candidates: list[TradeCandidate] = []
    for outcome in outcomes:
        prior = prior_yes_asks.get(outcome.market_id)
        current = current_yes_asks.get(outcome.market_id)
        if prior is None or current is None:
            continue
        impact = classify_directional_impact(
            outcome.predicate, old_forecast_max_c, new_forecast_max_c
        )
        if impact == DirectionalImpact.NO_MATERIAL_IMPACT:
            continue
        response = classify_price_response(impact, prior, current, min_move)
        if response != PriceResponse.PRICE_NOT_MOVED_WITH_EVENT:
            continue
        side = (
            "BUY_YES"
            if impact == DirectionalImpact.INCREASES_YES_PROBABILITY
            else "BUY_NO"
        )
        candidates.append(
            TradeCandidate(
                outcome=outcome,
                side=side,
                impact=impact,
                price_response=response,
                prior_yes_ask=prior,
                current_yes_ask=current,
                reason="price has not moved with HKO event",
            )
        )
    return candidates

