from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    bankroll_usd: float = 5000.0
    max_order_usd: float = 250.0
    max_daily_drawdown_usd: float = 4000.0


@dataclass
class Position:
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class PaperOrderResult:
    status: str
    side: str
    outcome_id: str
    requested_size_usd: float
    fill_price: float | None
    fill_size_usd: float
    shares: float
    reason: str


class PaperTrader:
    def __init__(self, risk: RiskConfig):
        self.risk = risk
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0

    def buy(
        self,
        outcome_id: str,
        limit_price: float,
        size_usd: float,
        asks: list[tuple[float, float]],
        reason: str,
    ) -> PaperOrderResult:
        rejection = self._risk_rejection(size_usd)
        if rejection:
            return PaperOrderResult(
                "rejected", "buy", outcome_id, size_usd, None, 0, 0, rejection
            )

        remaining_usd = size_usd
        spent = 0.0
        shares = 0.0
        for price, available_shares in sorted(asks):
            if price > limit_price or remaining_usd <= 0:
                continue
            max_shares = remaining_usd / price
            take_shares = min(max_shares, available_shares)
            spent += take_shares * price
            shares += take_shares
            remaining_usd -= take_shares * price

        if shares <= 0:
            return PaperOrderResult(
                "rejected", "buy", outcome_id, size_usd, None, 0, 0, "no executable depth"
            )

        fill_price = spent / shares
        position = self.positions.setdefault(outcome_id, Position())
        new_cost = position.avg_price * position.shares + spent
        position.shares += shares
        position.avg_price = new_cost / position.shares
        return PaperOrderResult(
            "filled", "buy", outcome_id, size_usd, fill_price, spent, shares, reason
        )

    def sell(
        self,
        outcome_id: str,
        limit_price: float,
        shares: float,
        bids: list[tuple[float, float]],
        reason: str,
    ) -> PaperOrderResult:
        position = self.positions.get(outcome_id, Position())
        shares_to_sell = min(shares, position.shares)
        if shares_to_sell <= 0:
            return PaperOrderResult(
                "rejected", "sell", outcome_id, 0, None, 0, 0, "no position"
            )

        remaining = shares_to_sell
        proceeds = 0.0
        sold = 0.0
        for price, available_shares in sorted(bids, reverse=True):
            if price < limit_price or remaining <= 0:
                continue
            take = min(remaining, available_shares)
            proceeds += take * price
            sold += take
            remaining -= take

        if sold <= 0:
            return PaperOrderResult(
                "rejected", "sell", outcome_id, 0, None, 0, 0, "no executable depth"
            )

        fill_price = proceeds / sold
        position.shares -= sold
        pnl = proceeds - sold * position.avg_price
        position.realized_pnl += pnl
        self.realized_pnl += pnl
        return PaperOrderResult(
            "filled", "sell", outcome_id, proceeds, fill_price, proceeds, sold, reason
        )

    def _risk_rejection(self, size_usd: float) -> str | None:
        if size_usd > self.risk.max_order_usd:
            return "order exceeds max_order_usd"
        if self.realized_pnl <= -self.risk.max_daily_drawdown_usd:
            return "daily drawdown limit breached"
        return None

