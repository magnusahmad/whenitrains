from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class CandidateAction:
    key: str
    conflict_keys: frozenset[str]
    run: Callable[[], object]


@dataclass(frozen=True)
class CandidateResult:
    key: str
    value: object


class ExecutionScheduler:
    def __init__(self, max_workers: int = 4) -> None:
        self.max_workers = max(1, max_workers)

    def run(self, actions: Iterable[CandidateAction]) -> list[CandidateResult]:
        action_list = list(actions)
        action_positions = {id(action): index for index, action in enumerate(action_list)}
        batches = _serial_batches(action_list)
        results: list[CandidateResult | None] = [None] * len(action_list)
        for batch in batches:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(batch))) as executor:
                futures = [executor.submit(action.run) for action in batch]
                for action, future in zip(batch, futures):
                    results[action_positions[id(action)]] = CandidateResult(
                        action.key, future.result()
                    )
        return [result for result in results if result is not None]


def _serial_batches(actions: list[CandidateAction]) -> list[list[CandidateAction]]:
    batches: list[list[CandidateAction]] = []
    for action in actions:
        for batch in batches:
            used_keys = set().union(*(item.conflict_keys for item in batch))
            if action.conflict_keys.isdisjoint(used_keys):
                batch.append(action)
                break
        else:
            batches.append([action])
    return batches
