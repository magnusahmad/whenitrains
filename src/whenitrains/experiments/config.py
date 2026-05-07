from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExperimentExecutionConfig:
    max_order_usd: float = 250.0
    order_size_usd: float = 250.0
    min_fill_usd: float = 25.0
    max_entry_price: float = 0.30


@dataclass(frozen=True)
class ExperimentConfig:
    name: str = "forecast-cheap-v1"
    strategy: str = "forecast_bucket_cheap_yes"
    execution: ExperimentExecutionConfig = ExperimentExecutionConfig()

    @classmethod
    def from_json_text(cls, text: str) -> "ExperimentConfig":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("experiment config must be a JSON object")
        allowed = {"name", "strategy", "execution"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown experiment config keys: {sorted(unknown)}")
        execution_raw = raw.get("execution", {})
        if not isinstance(execution_raw, dict):
            raise ValueError("execution config must be a JSON object")
        execution_allowed = {
            "max_order_usd",
            "order_size_usd",
            "min_fill_usd",
            "max_entry_price",
        }
        execution_unknown = set(execution_raw) - execution_allowed
        if execution_unknown:
            raise ValueError(
                f"unknown execution config keys: {sorted(execution_unknown)}"
            )
        return cls(
            name=str(raw.get("name", cls.name)),
            strategy=str(raw.get("strategy", cls.strategy)),
            execution=ExperimentExecutionConfig(**execution_raw),
        )

    @classmethod
    def from_path(cls, path: Path | None) -> "ExperimentConfig":
        if path is None:
            return cls()
        return cls.from_json_text(path.read_text())

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

