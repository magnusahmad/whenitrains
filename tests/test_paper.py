import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from whenitrains.paper import PaperTrader, RiskConfig
from whenitrains.paper_db import (
    calculate_entry,
    calculate_exit,
    execute_paper_buy,
    execute_paper_sell,
)
from whenitrains.storage import connect, migrate


class PaperTraderTests(unittest.TestCase):
    def test_buy_fills_through_ask_depth_and_updates_position(self):
        trader = PaperTrader(RiskConfig(bankroll_usd=5000, max_order_usd=250))
        result = trader.buy(
            outcome_id="25C",
            limit_price=0.40,
            size_usd=100,
            asks=[(0.38, 100), (0.39, 200)],
            reason="stale price",
        )
        self.assertEqual(result.status, "filled")
        self.assertGreater(trader.positions["25C"].shares, 0)

    def test_rejects_order_over_max_size(self):
        trader = PaperTrader(RiskConfig(bankroll_usd=5000, max_order_usd=250))
        result = trader.buy("25C", 0.40, 251, [(0.38, 1000)], "too large")
        self.assertEqual(result.status, "rejected")

    def test_drawdown_freezes_new_entries_at_80_percent(self):
        trader = PaperTrader(RiskConfig(bankroll_usd=5000, max_daily_drawdown_usd=4000))
        trader.realized_pnl = -4001
        result = trader.buy("25C", 0.40, 10, [(0.38, 1000)], "drawdown")
        self.assertEqual(result.status, "rejected")
        self.assertIn("drawdown", result.reason)


class PaperDbTests(unittest.TestCase):
    def test_calculate_entry_uses_visible_ask_depth(self):
        asks = [(0.38, 100), (0.39, 100)]
        entry = calculate_entry("yes", 100, asks, max_order_usd=250)
        self.assertEqual(entry.status, "fillable")
        self.assertAlmostEqual(entry.limit_price, 0.39)
        self.assertAlmostEqual(entry.estimated_avg_price, 0.385)

    def test_calculate_entry_respects_max_price(self):
        asks = [(0.30, 100), (0.31, 1000)]
        entry = calculate_entry("yes", 250, asks, max_order_usd=250, max_price=0.30)

        self.assertEqual(entry.status, "fillable")
        self.assertAlmostEqual(entry.limit_price, 0.30)
        self.assertAlmostEqual(entry.estimated_cost_usd, 30.0)
        self.assertAlmostEqual(entry.estimated_shares, 100.0)

    def test_paper_buy_and_sell_persist_position_and_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            buy = execute_paper_buy(
                db,
                token_id="yes25",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test buy",
            )
            self.assertEqual(buy.status, "filled")
            exit_check = calculate_exit(db, "yes25", current_bid=0.45, take_profit=0.03)
            self.assertTrue(exit_check.should_sell)
            sell = execute_paper_sell(
                db,
                token_id="yes25",
                bids=[(0.45, 1000)],
                reason="test sell",
            )
            self.assertEqual(sell.status, "filled")
            pnl = db.execute(
                "select realized_pnl from paper_positions where outcome_id = 'yes25'"
            ).fetchone()[0]
            self.assertGreater(pnl, 0)

    def test_calculate_exit_sells_after_max_hold_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            execute_paper_buy(
                db,
                token_id="yes25",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test buy",
            )
            entry_time = datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc)
            db.execute(
                "update paper_positions set updated_at_utc = ? where outcome_id = ?",
                (entry_time.isoformat(), "yes25"),
            )
            db.commit()
            exit_check = calculate_exit(
                db,
                "yes25",
                current_bid=0.40,
                take_profit=0.03,
                max_hold_minutes=10,
                now=entry_time + timedelta(minutes=10),
            )
            self.assertTrue(exit_check.should_sell)
            self.assertEqual(exit_check.reason, "max hold time reached")


if __name__ == "__main__":
    unittest.main()
