from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/whenitrains.sqlite3")
    bankroll_usd: float = 5000.0
    max_order_usd: float = 250.0
    max_daily_drawdown_usd: float = 4000.0
    stale_price_min_move: float = 0.02
    actual_new_bucket_stale_price_min_move: float = 0.10
    actual_new_bucket_max_entry_price: float = 0.70
    actual_invalidated_bucket_max_entry_price: float = 0.99
    take_profit_move: float = 0.20
    max_hold_minutes: float = 10.0
    max_entry_price: float = 0.98
    forecast_change_max_price_move: float = 0.20
    forecast_change_max_entry_price: float = 0.40
    forecast_change_d2_max_entry_price: float = 0.20
    ocf_forecast_freshness_max_age_minutes: float = 90.0
    max_entry_limit_slippage: float = 0.05
    min_entry_fill_usd: float = 25.0
    dust_order_epsilon_usd: float = 0.01
    forecast_value_max_yes_ask: float = 0.30
    peak_hour_actual_cross_max_yes_ask: float = 0.80
    forecast_value_max_lead_days: int = 1
    live_manual_order_cap_usd: float = 5.0
    live_scheduler_order_cap_usd: float = 20.0
    live_total_open_exposure_cap_usd: float = 200.0
    live_daily_realized_loss_cap_usd: float = 200.0
    live_keychain_service: str = "whenitrains-polymarket"
    live_keychain_account: str = "bot-private-key"
    live_kill_switch_path: Path = Path("data/KILL_SWITCH")
