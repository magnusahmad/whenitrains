from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/whenitrains.sqlite3")
    bankroll_usd: float = 5000.0
    max_order_usd: float = 250.0
    max_daily_drawdown_usd: float = 4000.0
    stale_price_min_move: float = 0.02
    take_profit_move: float = 0.03

