"""In-play hedging math — 2-WAY "ADVANCE" FORK (plan 24 §4) of inplay_hedge.py.

Knockout who-advances twin of strategy/inplay_hedge.py. The advance market has only TWO
outcomes — **home advances / away advances** (no draw; ET + penalties decide it). So the hedge
for a held "home advances" position is to buy the COMPLEMENT, "away advances". This is a real
financial re-derivation, not a column drop: the 3-state payoff matrix collapses to two states
and the hedge targets simplify (full == maximin == delta-neutral == b = a, one-for-one). The
3-way inplay_hedge.py is UNCHANGED and runs in parallel.

payoff (cent; b 张 away-advances hedge, a 张 home-advances held, entry X, hedge ask Y):
    cost_held  = a * X
    cost_hedge = b * Y
    home advances:  100a - cost_held - cost_hedge      (away leg pays 0)
    away advances:  100b - cost_held - cost_hedge      (held leg pays 0)
realised_c (partial cash-out proceeds) shifts both states equally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["home", "away"]
SIDES: tuple[Side, Side] = ("home", "away")


def to_cents(p: float) -> float:
    """0–1 概率价 → cent。0.60 -> 60。已是 cent(>1.5)的原样返回。"""
    return p * 100.0 if p <= 1.5 else p


def _other(side: Side) -> Side:
    return "away" if side == "home" else "home"


@dataclass(frozen=True)
class Quotes:
    """某一时刻 2-way 晋级盘的盘口(cent)。ask=买入付价;bid=卖出收价(默认回退 ask)。"""
    home_ask: float | None = None
    away_ask: float | None = None
    home_bid: float | None = None
    away_bid: float | None = None
    minute: int | None = None
    score: str | None = None

    @classmethod
    def from_probs(cls, home: float | None, away: float | None,
                   *, minute: int | None = None, score: str | None = None) -> "Quotes":
        h = to_cents(home) if home is not None else None
        a = to_cents(away) if away is not None else None
        return cls(home_ask=h, away_ask=a, home_bid=h, away_bid=a, minute=minute, score=score)

    def ask(self, side: Side) -> float | None:
        return {"home": self.home_ask, "away": self.away_ask}[side]

    def bid(self, side: Side) -> float | None:
        b = {"home": self.home_bid, "away": self.away_bid}[side]
        return b if b is not None else self.ask(side)


@dataclass(frozen=True)
class Position:
    """已建的方向性晋级持仓(默认 home advances 多头)。"""
    shares: float
    entry_c: float
    side: Side = "home"
    realised_c: float = 0.0

    @property
    def cost_c(self) -> float:
        return self.shares * self.entry_c


@dataclass(frozen=True)
class PayoffRow:
    """某个对冲方案在 2 态下的盈亏(cent)。"""
    b: float                       # 对冲腿(对手晋级)张数
    hedge_side: Side
    cost_hedge_c: float
    pnl_home_adv: float
    pnl_away_adv: float
    min_pnl: float
    max_pnl: float

    def as_dict(self) -> dict:
        return {"b": round(self.b, 2), "hedge_side": self.hedge_side,
                "cost_hedge_c": round(self.cost_hedge_c, 2),
                "home": round(self.pnl_home_adv, 2), "away": round(self.pnl_away_adv, 2),
                "min": round(self.min_pnl, 2), "max": round(self.max_pnl, 2)}


def _settle(side: Side, outcome: Side) -> float:
    return 100.0 if side == outcome else 0.0


def hedge_payoff(position: Position, hedge_side: Side, b: float, hedge_ask: float) -> PayoffRow:
    """2 态 payoff:已持 a 张 position.side,再买 b 张 hedge_side(单价 hedge_ask)。"""
    a = position.shares
    base = position.realised_c - position.cost_c - b * hedge_ask

    def pnl(outcome: Side) -> float:
        return a * _settle(position.side, outcome) + b * _settle(hedge_side, outcome) + base

    p_home, p_away = pnl("home"), pnl("away")
    return PayoffRow(b=b, hedge_side=hedge_side, cost_hedge_c=b * hedge_ask,
                     pnl_home_adv=p_home, pnl_away_adv=p_away,
                     min_pnl=min(p_home, p_away), max_pnl=max(p_home, p_away))


@dataclass(frozen=True)
class HedgeSolution:
    b: float | None
    hedge_side: Side
    target: str
    payoff: PayoffRow | None
    note: str = ""


def break_even_b(position: Position, quotes: Quotes, *, hedge_side: Side | None = None,
                 floor_c: float = 0.0) -> HedgeSolution:
    """求让 **对手晋级态盈亏 ≥ -floor_c** 的最小 b(保本对冲)。

    持 home advances、买 away advances(Y=away ask),对手态盈亏:
        pnl_away_adv(b) = 100b - bY - cost_held + R = b(100 - Y) - cost_held + R
    令 ≥ -floor:  b ≥ (cost_held - R - floor) / (100 - Y)。
    """
    hedge_side = hedge_side or _other(position.side)
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "break_even", None, "no hedge ask")
    if Y >= 100.0:
        return HedgeSolution(None, hedge_side, "break_even", None, "hedge ask >= 100, no payout edge")
    need = position.cost_c - position.realised_c - floor_c
    b = max(0.0, need / (100.0 - Y))
    row = hedge_payoff(position, hedge_side, b, Y)
    return HedgeSolution(b=b, hedge_side=hedge_side, target="break_even", payoff=row,
                         note=f"对手晋级态盈亏 >= {-floor_c:+.1f}¢")


def full_hedge_b(position: Position, quotes: Quotes, *, hedge_side: Side | None = None) -> float | None:
    """完全对冲:让 **两态等利润** 的 b。在 2-way 这是 b = a(一对一),与已落袋现金无关
    (R 平移两态、抵消)——买等量对手腿即把两态拉平为锁定利润。"""
    hedge_side = hedge_side or _other(position.side)
    if quotes.ask(hedge_side) is None:
        return None
    return max(0.0, position.shares)


def maximin_hedge(position: Position, quotes: Quotes, *, hedge_side: Side | None = None) -> HedgeSolution:
    """最大化两态最差盈亏。home_adv 随 b 递减(斜率 -Y)、away_adv 随 b 递增(斜率 100-Y),
    交点在 b = a → 两态相等 = 100a - aX - aY + R,即最差利润的极大点(= 完全对冲)。"""
    hedge_side = hedge_side or _other(position.side)
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "maximin", None, "no hedge ask")
    b = max(0.0, position.shares)
    row = hedge_payoff(position, hedge_side, b, Y)
    return HedgeSolution(b=b, hedge_side=hedge_side, target="maximin", payoff=row,
                         note=f"锁定最小利润 {row.min_pnl:+.1f}¢")


def delta_neutral_b(position: Position, quotes: Quotes, *, hedge_side: Side | None = None,
                    target_exposure_c: float = 0.0) -> HedgeSolution:
    """把两态敞口差降到目标。Δ(b)=pnl_home_adv-pnl_away_adv = 100a - 100b。
    令 Δ=target → b = (100a - target)/100 = a - target/100。"""
    hedge_side = hedge_side or _other(position.side)
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "delta_neutral", None, "no hedge ask")
    b = max(0.0, (100.0 * position.shares - target_exposure_c) / 100.0)
    row = hedge_payoff(position, hedge_side, b, Y)
    return HedgeSolution(b=b, hedge_side=hedge_side, target="delta_neutral", payoff=row,
                         note=f"两态盈亏差 = {target_exposure_c:+.1f}¢")


def payoff_matrix(position: Position, quotes: Quotes, hedge_side: Side | None = None,
                  bs: list[float] | None = None) -> list[PayoffRow]:
    """候选 b 的 2 态 payoff 表(默认锚点:0 / 保本 / 完全对冲=maximin)。"""
    hedge_side = hedge_side or _other(position.side)
    hedge_ask = quotes.ask(hedge_side)
    if hedge_ask is None:
        raise ValueError(f"no ask for hedge side {hedge_side!r}")
    if bs is None:
        anchors = {0.0}
        be = break_even_b(position, quotes, hedge_side=hedge_side)
        if be.b is not None:
            anchors.add(be.b)
        full = full_hedge_b(position, quotes, hedge_side=hedge_side)
        if full is not None:
            anchors.add(full)
        bs = sorted(anchors)
    return [hedge_payoff(position, hedge_side, b, hedge_ask) for b in bs]


def hedge_advance_protection(position: Position, quotes: Quotes, *,
                             target: str = "break_even", floor_c: float = 0.0,
                             hedge_side: Side | None = None) -> HedgeSolution:
    """统一入口:已持 position.side 晋级,买对手晋级做保护。
    target: break_even / maximin / delta_neutral / full。"""
    hedge_side = hedge_side or _other(position.side)
    if target == "break_even":
        return break_even_b(position, quotes, hedge_side=hedge_side, floor_c=floor_c)
    if target == "maximin":
        return maximin_hedge(position, quotes, hedge_side=hedge_side)
    if target in ("delta_neutral", "full"):
        return delta_neutral_b(position, quotes, hedge_side=hedge_side)
    raise ValueError(f"unknown target {target!r}")


@dataclass(frozen=True)
class CashoutPlan:
    sell_shares: float
    sell_at_c: float
    realised_c: float
    remaining: Position
    locked_min_pnl: float


def partial_cashout(position: Position, quotes: Quotes, fraction: float) -> CashoutPlan:
    """卖回 `fraction` 的持仓(按持仓边 bid)落袋,降低净敞口。"""
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    bid = quotes.bid(position.side)
    if bid is None:
        raise ValueError("no bid to cash out into")
    sell = position.shares * fraction
    realised = sell * bid
    remaining = Position(shares=position.shares - sell, entry_c=position.entry_c,
                         side=position.side, realised_c=position.realised_c + realised)
    worst = remaining.realised_c - remaining.cost_c
    return CashoutPlan(sell_shares=sell, sell_at_c=bid, realised_c=realised,
                       remaining=remaining, locked_min_pnl=worst)


@dataclass(frozen=True)
class DutchLock:
    asks_c: dict
    basket_c: float                # home_ask + away_ask
    tradable: bool                 # < 100 → 锁利(2-way basket arb)
    shares: dict
    guaranteed_pnl_c: float
    note: str = ""


def dutch_lock(quotes: Quotes, *, budget_c: float | None = None, fee_c: float = 0.0) -> DutchLock:
    """2-way basket:两边都买,任意结果回收 100¢/组。tradable 当 home_ask+away_ask+fee < 100
    (这正是晋级盘的两腿锁利;等张数,因 100¢/张二元合约)。"""
    asks = {s: quotes.ask(s) for s in SIDES}
    if any(v is None for v in asks.values()):
        return DutchLock(asks_c=dict(asks), basket_c=float("nan"), tradable=False,
                         shares={}, guaranteed_pnl_c=0.0, note="missing one or more asks")
    basket = sum(asks.values()) + 2 * fee_c
    n = (budget_c / basket) if budget_c is not None else 1.0
    return DutchLock(asks_c={k: round(v, 2) for k, v in asks.items()}, basket_c=round(basket, 2),
                     tradable=basket < 100.0, shares={s: round(n, 4) for s in SIDES},
                     guaranteed_pnl_c=round(n * (100.0 - basket), 2),
                     note=("Σask<100 → 两腿锁利" if basket < 100.0 else "Σask>=100 → 仅可定向对冲"))


def format_matrix(rows: list[PayoffRow], title: str = "") -> str:
    out = []
    if title:
        out.append(title)
    out.append(f"{'b':>7} {'side':>5} {'cost':>7} | {'home晋级':>9} {'away晋级':>9} | {'min':>8} {'max':>8}")
    out.append("-" * 60)
    for r in rows:
        out.append(f"{r.b:>7.2f} {r.hedge_side:>5} {r.cost_hedge_c:>7.1f} | "
                   f"{r.pnl_home_adv:>9.1f} {r.pnl_away_adv:>9.1f} | {r.min_pnl:>8.1f} {r.max_pnl:>8.1f}")
    return "\n".join(out)


def _demo() -> None:
    print("=" * 60)
    print("2-way ADVANCE 对冲算例 — 持 home 晋级,买 away 晋级保护")
    print("=" * 60)
    pos = Position(shares=10, entry_c=60.0, side="home")
    print(f"入场: 买 {pos.shares:.0f} 张 home-advances @ {pos.entry_c}¢,成本 {pos.cost_c:.1f}¢")
    # home 领先后盘口:home 晋级 75¢ / away 晋级 27¢
    q = Quotes.from_probs(0.75, 0.27, minute=70, score="1-0")
    print(f"盘口: home_adv={q.home_ask}¢ away_adv={q.away_ask}¢ (Σ={q.home_ask+q.away_ask:.1f})\n")
    for tgt in ("break_even", "delta_neutral", "maximin"):
        sol = hedge_advance_protection(pos, q, target=tgt)
        r = sol.payoff
        print(f"[{tgt:>13}] 买 b={sol.b:.2f} 张 away-adv @ {q.away_ask}¢ | "
              f"home晋级 {r.pnl_home_adv:+.1f}  away晋级 {r.pnl_away_adv:+.1f}  (min {r.min_pnl:+.1f}) — {sol.note}")
    print()
    print(format_matrix(payoff_matrix(pos, q), "payoff 矩阵(away-adv 保护,a=10):"))
    dl = dutch_lock(q)
    print(f"\n两腿 dutch_lock: Σask={dl.basket_c}¢ tradable={dl.tradable} → {dl.note}")


if __name__ == "__main__":
    _demo()
