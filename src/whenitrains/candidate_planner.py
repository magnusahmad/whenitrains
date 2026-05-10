from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .execution_scheduler import CandidateAction


@dataclass(frozen=True)
class ActualCrossEvent:
    event_key: str
    target_date_hkt: str
    kind: str
    old_value: float
    new_value: float


@dataclass(frozen=True)
class ActualCrossTokenSet:
    crossed_bucket_yes_token_id: str | None = None
    invalidated_yes_position_token_ids: tuple[str, ...] = ()
    invalidated_bucket_no_token_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedCandidateAction:
    source_event_key: str
    candidate_key: str
    intent: str
    token_id: str
    side: str
    conflict_keys: frozenset[str]


def plan_actual_cross_actions(
    event: ActualCrossEvent,
    tokens: ActualCrossTokenSet,
) -> list[PlannedCandidateAction]:
    actions: list[PlannedCandidateAction] = []
    for token_id in tokens.invalidated_yes_position_token_ids:
        actions.append(
            _action(
                event,
                "sell_invalidated_position",
                token_id,
                "SELL",
                extra_conflicts=(f"position:{token_id}",),
            )
        )
    if tokens.crossed_bucket_yes_token_id is not None:
        actions.append(
            _action(
                event,
                "buy_crossed_bucket_yes",
                tokens.crossed_bucket_yes_token_id,
                "BUY_YES",
                extra_conflicts=("risk:entry_budget",),
            )
        )
    for token_id in tokens.invalidated_bucket_no_token_ids:
        actions.append(
            _action(
                event,
                "buy_invalidated_bucket_no",
                token_id,
                "BUY_NO",
                extra_conflicts=("risk:entry_budget",),
            )
        )
    return actions


def executable_candidate_actions(
    actions: list[PlannedCandidateAction],
    executor: Callable[[PlannedCandidateAction], object],
) -> list[CandidateAction]:
    return [
        CandidateAction(
            action.candidate_key,
            conflict_keys=action.conflict_keys,
            run=lambda action=action: executor(action),
        )
        for action in actions
    ]


def _action(
    event: ActualCrossEvent,
    intent: str,
    token_id: str,
    side: str,
    *,
    extra_conflicts: tuple[str, ...] = (),
) -> PlannedCandidateAction:
    return PlannedCandidateAction(
        source_event_key=event.event_key,
        candidate_key=f"{event.event_key}:{intent}:{token_id}",
        intent=intent,
        token_id=token_id,
        side=side,
        conflict_keys=frozenset((f"token:{token_id}",) + extra_conflicts),
    )
