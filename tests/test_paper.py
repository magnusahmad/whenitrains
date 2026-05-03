import unittest

from whenitrains.paper import PaperTrader, RiskConfig


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


if __name__ == "__main__":
    unittest.main()
