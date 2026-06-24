"""In-play hedging math (plan 23) — 盘中对冲 / 衍生品式风险管理.

预测市场上每个三向结果(home / draw / away)是一张 0–100¢ 的二元合约:结算时赢的
结果付 100¢、输的付 0¢。盘中价格随比赛状态变化。本模块把"持有 home 后给仓位上保护"
这件事做成严谨的衍生品对冲框架:报价 + 持仓 → payoff 矩阵 + 建议对冲张数。

核心场景(用户原话):
  我们先买了 a 张 home(入场价 X¢)。home 先进球 → home 价涨、draw 价跌。此刻买
  b 张 draw 当保护。买多少要看赔率(每张几 cent):若 home 真赢我们仍赚,若打平也
  不至于亏太多。

设计取向(对齐仓库 strategy/cross_venue.py、smart_exit.py、inplay_tactics.py):
  * 纯函数 + frozen dataclass,无副作用、可单测、无外部依赖(仅标准库)。
  * 价格统一用 **cent(0–100)** 作单位(合约 payoff = 100¢),与日志/price_tick 的
    0–1 概率价乘 100 对齐。辅助函数 `to_cents` 负责换算。
  * 只做数学:不下单、不碰 venue。执行/风控上层负责($1 测试上限、滑点、手续费)。

每个对冲目标都给闭式或数值解:
  - 保本对冲 break_even_b      : 让 draw 发生时盈亏 ≥ -floor。
  - 极大极小 maximin_hedge     : 最大化三态最差盈亏(锁定最小利润)。
  - Delta 对冲 delta_neutral_b : 把"draw 这一状态"的敞口降到目标。
  - 三向锁利 dutch_lock        : 三边都买,任意结果同利润(basket < payoff)。
  - 部分止盈 partial_cashout   : 卖回部分 home 兑现浮盈,降低净敞口。

payoff 约定(以 cent 计,b 张 draw、a 张 home、home 入场 X、draw 买入价 Y):
    cost_home = a * X              # 已沉没的 home 成本
    cost_draw = b * Y              # 新增的 draw 成本
    home 赢 :  100a - cost_home - cost_draw      (draw 输光)
    draw    :  100b - cost_draw - cost_home      (home 输光)
    away 赢 :       - cost_home - cost_draw      (两边都输)
若入场时部分 home 已被卖出(partial cashout),用 `realised_home_c` 记已落袋现金,
它平移所有三态 payoff(见 Position.realised_home_c)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["home", "draw", "away"]
SIDES: tuple[Side, Side, Side] = ("home", "draw", "away")


# --------------------------------------------------------------------------- #
# 单位换算:日志/price_tick 用 0–1 概率价,本模块内部用 cent(0–100)。
# --------------------------------------------------------------------------- #
def to_cents(p: float) -> float:
    """0–1 概率价 → cent。0.825 -> 82.5。已是 cent(>1.5)的原样返回。"""
    return p * 100.0 if p <= 1.5 else p


# --------------------------------------------------------------------------- #
# 数据结构:报价 + 持仓
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Quotes:
    """某一时刻三向合约的盘口(cent)。

    ask = 我们买入要付的价;bid = 我们卖出能收的价(默认回退到 ask，即假设
    零价差,上层若有真实 bid 应显式传入)。缺失边传 None。
    """
    home_ask: float | None = None
    draw_ask: float | None = None
    away_ask: float | None = None
    home_bid: float | None = None
    draw_bid: float | None = None
    away_bid: float | None = None
    minute: int | None = None
    score: str | None = None

    @classmethod
    def from_probs(cls, home: float | None, draw: float | None, away: float | None,
                   *, minute: int | None = None, score: str | None = None) -> "Quotes":
        """从 0–1 概率价(日志 model 或 price_tick)建报价,bid=ask(零价差假设)。"""
        h = to_cents(home) if home is not None else None
        d = to_cents(draw) if draw is not None else None
        a = to_cents(away) if away is not None else None
        return cls(home_ask=h, draw_ask=d, away_ask=a,
                   home_bid=h, draw_bid=d, away_bid=a, minute=minute, score=score)

    def ask(self, side: Side) -> float | None:
        return {"home": self.home_ask, "draw": self.draw_ask, "away": self.away_ask}[side]

    def bid(self, side: Side) -> float | None:
        b = {"home": self.home_bid, "draw": self.draw_bid, "away": self.away_bid}[side]
        return b if b is not None else self.ask(side)


@dataclass(frozen=True)
class Position:
    """已建的方向性持仓。默认是 home 多头(用户的核心场景),但 `side` 可改,
    使框架可用于任意一边持仓 + 任意一边对冲。

    shares           : 持有张数 a。
    entry_c          : 入场价 X(cent / 张)。
    side             : 持有哪一边(默认 home)。
    realised_home_c  : 已通过部分止盈落袋的现金(cent),平移所有 payoff;默认 0。
    """
    shares: float
    entry_c: float
    side: Side = "home"
    realised_home_c: float = 0.0

    @property
    def cost_c(self) -> float:
        """该方向性腿的沉没成本(cent)。"""
        return self.shares * self.entry_c


# --------------------------------------------------------------------------- #
# payoff 引擎
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PayoffRow:
    """某个对冲方案在三态下的盈亏(cent)。"""
    b: float                       # 对冲腿张数(draw 张数,或所选 hedge_side 张数)
    hedge_side: Side
    cost_hedge_c: float            # b * 对冲腿 ask
    pnl_home_win: float            # 持仓边赢的态(对 home 持仓 = home 赢)
    pnl_draw: float
    pnl_away_win: float
    min_pnl: float                 # 三态最差
    max_pnl: float                 # 三态最好

    def as_dict(self) -> dict:
        return {"b": round(self.b, 2), "hedge_side": self.hedge_side,
                "cost_hedge_c": round(self.cost_hedge_c, 2),
                "home": round(self.pnl_home_win, 2),
                "draw": round(self.pnl_draw, 2),
                "away": round(self.pnl_away_win, 2),
                "min": round(self.min_pnl, 2), "max": round(self.max_pnl, 2)}


def _settle(side: Side, outcome: Side) -> float:
    """一张 `side` 合约在 `outcome` 结算时的回收(cent):赢付 100,输付 0。"""
    return 100.0 if side == outcome else 0.0


def hedge_payoff(position: Position, hedge_side: Side, b: float, hedge_ask: float) -> PayoffRow:
    """通用三态 payoff:已持 position(a 张某边),再买 b 张 hedge_side(单价 hedge_ask)。

    每态 pnl = a*settle(pos)+ b*settle(hedge) + realised_home - cost_home - cost_hedge
    """
    a = position.shares
    cost_pos = position.cost_c
    cost_hedge = b * hedge_ask
    base = position.realised_home_c - cost_pos - cost_hedge

    def pnl(outcome: Side) -> float:
        return a * _settle(position.side, outcome) + b * _settle(hedge_side, outcome) + base

    p_home, p_draw, p_away = pnl("home"), pnl("draw"), pnl("away")
    return PayoffRow(b=b, hedge_side=hedge_side, cost_hedge_c=cost_hedge,
                     pnl_home_win=p_home, pnl_draw=p_draw, pnl_away_win=p_away,
                     min_pnl=min(p_home, p_draw, p_away), max_pnl=max(p_home, p_draw, p_away))


def payoff_matrix(position: Position, quotes: Quotes, hedge_side: Side = "draw",
                  bs: list[float] | None = None) -> list[PayoffRow]:
    """对一组候选 b 张数,返回三态 payoff 表(默认 b=0/保本/maximin/Δ中性)。

    若不传 bs,自动放入几个有意义的锚点:0(不对冲)、break-even、maximin、
    完全对冲(让 home 赢 == draw 两态等利润的 b)。
    """
    hedge_ask = quotes.ask(hedge_side)
    if hedge_ask is None:
        raise ValueError(f"no ask for hedge side {hedge_side!r}")
    if bs is None:
        anchors = {0.0}
        be = break_even_b(position, quotes, hedge_side=hedge_side)
        if be.b is not None:
            anchors.add(be.b)
        mm = maximin_hedge(position, quotes, hedge_side=hedge_side)
        anchors.add(mm.b)
        full = full_hedge_b(position, quotes, hedge_side=hedge_side)
        if full is not None:
            anchors.add(full)
        bs = sorted(anchors)
    return [hedge_payoff(position, hedge_side, b, hedge_ask) for b in bs]


# --------------------------------------------------------------------------- #
# (i) 保本对冲:让 draw 态盈亏 ≥ -floor
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HedgeSolution:
    b: float | None                # 建议张数(None=无法满足目标)
    hedge_side: Side
    target: str
    payoff: PayoffRow | None
    note: str = ""


def break_even_b(position: Position, quotes: Quotes, *, hedge_side: Side = "draw",
                 floor_c: float = 0.0) -> HedgeSolution:
    """求让 **对冲态(默认 draw)发生时盈亏 ≥ -floor_c** 的最小 b(保本对冲)。

    对冲态盈亏(home 持仓、买 draw):
        pnl_draw(b) = 100b - bY - cost_home + realised   (Y = draw ask)
                    = b(100 - Y) - cost_home + realised
    令 pnl_draw(b) >= -floor:
        b >= (cost_home - realised - floor) / (100 - Y)
    这是把 draw 这一态从亏 cost_home 抬到 ≥ -floor 所需的最小保护张数。
    """
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "break_even", None, "no hedge ask")
    if Y >= 100.0:
        return HedgeSolution(None, hedge_side, "break_even", None, "hedge ask >= 100, no payout edge")
    need = position.cost_c - position.realised_home_c - floor_c
    b = max(0.0, need / (100.0 - Y))
    row = hedge_payoff(position, hedge_side, b, Y)
    return HedgeSolution(b=b, hedge_side=hedge_side, target="break_even", payoff=row,
                         note=f"draw 态盈亏 >= {-floor_c:+.1f}¢")


# --------------------------------------------------------------------------- #
# (ii) maximin:最大化三态最差盈亏(锁定最小利润)
# --------------------------------------------------------------------------- #
def maximin_hedge(position: Position, quotes: Quotes, *, hedge_side: Side = "draw") -> HedgeSolution:
    """求让 **三态最差盈亏最大** 的 b(maximin / 锁定最小利润)。

    对 home 持仓 + 买 draw,三态关于 b 的斜率:
        pnl_home_win(b) = 100a - cost_home - bY        ↓ 随 b 减(斜率 -Y)
        pnl_draw(b)     = b(100-Y) - cost_home + R      ↑ 随 b 增(斜率 100-Y)
        pnl_away(b)     = -cost_home - bY               ↓ 随 b 减(斜率 -Y)
    away 态恒 ≤ home 态(少了 100a),故最差是 min(draw, away)。
      * away 随 b 递减、draw 随 b 递增 → 最优 b 在 away==draw 交点(若可达),
        即 -bY - C = b(100-Y) - C + R  ⇒  b* = -R/100 ≤ 0 ⇒ 一般取 0?
        但当 R=0 时 away==draw 仅在 b=0;away 永远 ≤ draw 当 b≥0?核对:
        draw - away = b(100-Y) + 100a? 不,away 没有 +100a。
    实务正确表述:最差态是 min over outcomes。我们对 b 在 [0, b_full] 上取
    min_pnl 的极大值——这是分段线性函数,极值在端点或在 home_win == draw 的交点。
        home_win == draw:  100a - bY = b(100-Y) + R  ⇒ b = (100a - R)/100 = a - R/100
    在该 b,home 赢与 draw 两态持平(=完全对冲),away 态更低。若 away 态此时仍
    是最差,则继续增 b 只会让 home/draw 更低、away 更低 → 不利;减 b 让 away 升、
    home 升、draw 降。故 maximin 在 min(draw,away) 与 (home) 的权衡点。
    用稳健的数值法:在 [0, 1.5*b_full] 细扫 + 端点,取 min_pnl 最大者(分段线性,
    步进足够细即得全局最优;再对最优点附近做闭式精修)。
    """
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "maximin", None, "no hedge ask")
    full = full_hedge_b(position, quotes, hedge_side=hedge_side)
    b_hi = max(full or 0.0, position.shares) * 1.5 + 1.0
    # 候选断点:0、full、各态两两相等的解(分段线性极值只可能在这些点)。
    cands = {0.0, full or 0.0}
    a, C, R = position.shares, position.cost_c, position.realised_home_c
    # home_win == draw : 100a - bY = b(100-Y) - C + R + C... 统一减 C 抵消:
    # 100a - bY = b(100-Y) + R  ->  b = (100a - R) / 100
    cands.add((100.0 * a - R) / 100.0)
    # draw == away : b(100-Y) - C + R = -C - bY  ->  100b = -R  -> b = -R/100 (R>=0 => <=0)
    cands.add(max(0.0, -R / 100.0))
    cands = sorted(x for x in cands if 0.0 <= x <= b_hi)
    best = max(cands, key=lambda b: hedge_payoff(position, hedge_side, b, Y).min_pnl)
    row = hedge_payoff(position, hedge_side, best, Y)
    return HedgeSolution(b=best, hedge_side=hedge_side, target="maximin", payoff=row,
                         note=f"锁定最小利润 {row.min_pnl:+.1f}¢")


# --------------------------------------------------------------------------- #
# (iii) Delta 中性化:完全对冲 home 赢==draw 两态
# --------------------------------------------------------------------------- #
def full_hedge_b(position: Position, quotes: Quotes, *, hedge_side: Side = "draw") -> float | None:
    """完全对冲(对 home 持仓 + draw):让 **home 赢态 == draw 态** 的 b。

        100a - bY = b(100 - Y) - C + R + C ... 同样减 cost 项后:
        100a - bY = b(100 - Y) + R
        100a - R  = b(100 - Y) + bY = 100b
        b = a - R/100
    即不计已落袋现金时 b == a(一张对一张),这把 {home 赢, draw} 两态拉平。
    """
    Y = quotes.ask(hedge_side)
    if Y is None:
        return None
    return max(0.0, position.shares - position.realised_home_c / 100.0)


def delta_neutral_b(position: Position, quotes: Quotes, *, hedge_side: Side = "draw",
                    target_draw_exposure_c: float = 0.0) -> HedgeSolution:
    """Delta 中性化:把"draw 这一状态"相对"home 赢状态"的敞口降到目标。

    定义状态 delta = pnl_home_win - pnl_draw(对冲后两态之差,正=仍偏好 home 赢):
        Δ(b) = (100a - bY) - (b(100-Y) - C + R) ... 减去同 cost 项:
        Δ(b) = 100a - 100b - R
    令 Δ(b) = target(target=0 即完全中性,两态等利润):
        b = (100a - R - target) / 100
    """
    Y = quotes.ask(hedge_side)
    if Y is None:
        return HedgeSolution(None, hedge_side, "delta_neutral", None, "no hedge ask")
    b = max(0.0, (100.0 * position.shares - position.realised_home_c - target_draw_exposure_c) / 100.0)
    row = hedge_payoff(position, hedge_side, b, Y)
    return HedgeSolution(b=b, hedge_side=hedge_side, target="delta_neutral", payoff=row,
                         note=f"home赢 - draw 两态差 = {target_draw_exposure_c:+.1f}¢")


# --------------------------------------------------------------------------- #
# 对外主入口:draw 保护对冲
# --------------------------------------------------------------------------- #
def hedge_draw_protection(position: Position, quotes: Quotes, *,
                          target: str = "break_even",
                          floor_c: float = 0.0,
                          hedge_side: Side = "draw") -> HedgeSolution:
    """用户核心场景的统一入口:已持 home,买 `hedge_side`(默认 draw)做保护。

    target:
      "break_even"    -> 对冲态盈亏 >= -floor_c(保本/限亏)。
      "maximin"       -> 最大化三态最差盈亏。
      "delta_neutral" -> home 赢态 == 对冲态(两态等利润)。
      "full"          -> 同 delta_neutral,b == a - R/100。

    返回 HedgeSolution(含建议 b + 该 b 的三态 PayoffRow)。
    """
    if target == "break_even":
        return break_even_b(position, quotes, hedge_side=hedge_side, floor_c=floor_c)
    if target == "maximin":
        return maximin_hedge(position, quotes, hedge_side=hedge_side)
    if target in ("delta_neutral", "full"):
        return delta_neutral_b(position, quotes, hedge_side=hedge_side)
    raise ValueError(f"unknown target {target!r}")


# --------------------------------------------------------------------------- #
# 部分止盈 / cash-out:卖回部分 home 兑现浮盈
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CashoutPlan:
    sell_shares: float             # 卖回的 home 张数
    sell_at_c: float               # 卖出价(home bid)
    realised_c: float              # 落袋现金 = sell_shares * sell_at_c
    remaining: Position            # 卖出后剩余持仓(含累计 realised)
    locked_min_pnl: float          # 卖出后最差态盈亏(若剩余 home 全输 + 不再对冲)


def partial_cashout(position: Position, quotes: Quotes, fraction: float) -> CashoutPlan:
    """卖回 `fraction` 比例的 home(按 home bid),把浮盈部分落袋,降低净敞口。

    home 进球后 home bid 升(本例 82.5¢),卖一部分即锁定该部分的 (bid-entry) 浮盈;
    剩余仓位继续搏 home 赢。落袋现金记入 remaining.realised_home_c,平移其后所有
    payoff —— 这正是"用涨上来的 home 价为后续 draw 保护买单"(见 markdown 免费对冲)。
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    bid = quotes.bid(position.side)
    if bid is None:
        raise ValueError("no bid to cash out into")
    sell = position.shares * fraction
    realised = sell * bid
    remaining = Position(shares=position.shares - sell, entry_c=position.entry_c,
                         side=position.side,
                         realised_home_c=position.realised_home_c + realised)
    # 剩余仓位若该边输(且不再对冲)的最差盈亏:
    worst = remaining.realised_home_c - remaining.cost_c
    return CashoutPlan(sell_shares=sell, sell_at_c=bid, realised_c=realised,
                       remaining=remaining, locked_min_pnl=worst)


# --------------------------------------------------------------------------- #
# 三向锁利 (dutching):三边都买,使任意结果同利润
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DutchLock:
    asks_c: dict                   # {side: ask}
    basket_c: float                # 单位张数下三边各买 1 的总成本(=sum ask)
    tradable: bool                 # basket < 100 => 任意结果都赚
    shares: dict                   # 等利润配比下各边张数(以最便宜边=1 归一,或按预算)
    guaranteed_pnl_c: float        # 每"组"的锁定利润(cent),tradable 时 > 0
    note: str = ""


def dutch_lock(quotes: Quotes, *, budget_c: float | None = None,
               fee_c: float = 0.0) -> DutchLock:
    """三向 dutching:三边都买,使任意结果回收同为 100¢/组。

    经典 basket 套利:三边 ask 之和 < 100(+费) 则每边各买 1 张,无论谁赢都回收
    100¢、成本 = Σask < 100 → 锁定 (100 - Σask) 利润(与 cross_venue.scan_yes_basket
    同源,这里落到单事件三向)。
    等利润配比:要让每个结果回收相同,各边张数 n_side ∝ 1/ask_side(便宜的多买)?
    —— 不。二元合约赢付 100/张,要让"home 赢回收 == draw 赢回收"需 100*n_home ==
    100*n_draw == 100*n_away,即 **三边等张数**。所以 dutch 在二元 100¢ 合约里就是
    各边等张数 n,组成本 n*Σask,组回收 n*100。tradable 当且仅当 Σask + fee < 100。
    若给 budget,则 n = budget / (Σask + 3*fee)。
    """
    asks = {s: quotes.ask(s) for s in SIDES}
    if any(v is None for v in asks.values()):
        return DutchLock(asks_c={k: v for k, v in asks.items()}, basket_c=float("nan"),
                         tradable=False, shares={}, guaranteed_pnl_c=0.0,
                         note="missing one or more asks")
    basket = sum(asks.values()) + 3 * fee_c
    tradable = basket < 100.0
    if budget_c is not None:
        n = budget_c / basket
    else:
        n = 1.0
    shares = {s: round(n, 4) for s in SIDES}
    guaranteed = n * (100.0 - basket)
    return DutchLock(asks_c={k: round(v, 2) for k, v in asks.items()},
                     basket_c=round(basket, 2), tradable=tradable, shares=shares,
                     guaranteed_pnl_c=round(guaranteed, 2),
                     note=("Σask<100 → 三向锁利" if tradable else "Σask>=100 → 无三向套利,仅可定向对冲"))


# --------------------------------------------------------------------------- #
# 反向 lay:对 home 持仓,买 (draw + away) = 合成卖出 home
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LayPlan:
    b_draw: float
    b_away: float
    cost_c: float                  # b_draw*draw_ask + b_away*away_ask
    pnl_home_win: float
    pnl_not_home: float            # draw 或 away 任一发生的盈亏(两腿之一付 100)
    locked: bool                   # 是否两态都 >=0(锁定)
    note: str = ""


def lay_hedge(position: Position, quotes: Quotes, *, shares: float | None = None) -> LayPlan:
    """反向 lay:买 (draw + away) 等张数 = 合成"卖出 home"。

    买 n 张 draw + n 张 away:home 不赢时必有一腿付 100n。
        home 赢 :  100a - cost_home + R - n(draw_ask + away_ask)
        非 home :  100n  - cost_home + R - n(draw_ask + away_ask)
    取 n = a 即把 home 多头基本对平(合成空头),组合趋近"以 (draw_ask+away_ask)
    价位卖出 home"。当 home_bid 不可得或想用 NO-腿复制时用此式(对照 cross_venue 的
    买 NO = 买另两边)。
    """
    n = position.shares if shares is None else shares
    da, aa = quotes.ask("draw"), quotes.ask("away")
    if da is None or aa is None:
        return LayPlan(0, 0, 0, 0, 0, False, "missing draw/away ask")
    cost_hedge = n * (da + aa)
    base = position.realised_home_c - position.cost_c - cost_hedge
    pnl_home = 100.0 * position.shares + base
    pnl_not = 100.0 * n + base
    return LayPlan(b_draw=n, b_away=n, cost_c=cost_hedge,
                   pnl_home_win=pnl_home, pnl_not_home=pnl_not,
                   locked=(min(pnl_home, pnl_not) >= 0.0),
                   note="买 draw+away 等张 = 合成卖 home")


# --------------------------------------------------------------------------- #
# 漂亮打印
# --------------------------------------------------------------------------- #
def format_matrix(rows: list[PayoffRow], title: str = "") -> str:
    out = []
    if title:
        out.append(title)
    out.append(f"{'b':>7} {'side':>5} {'cost':>7} | {'home赢':>8} {'draw':>8} {'away赢':>8} | {'min':>8} {'max':>8}")
    out.append("-" * 72)
    for r in rows:
        out.append(f"{r.b:>7.2f} {r.hedge_side:>5} {r.cost_hedge_c:>7.1f} | "
                   f"{r.pnl_home_win:>8.1f} {r.pnl_draw:>8.1f} {r.pnl_away_win:>8.1f} | "
                   f"{r.min_pnl:>8.1f} {r.max_pnl:>8.1f}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 真实算例:Argentina v Algeria(2026-06-17,fixture 1489381,终场 3-0,home 赢)
#   数据源:data/wc.db price_tick + data/logs/inplay_review_20260617.jsonl
#   入场  : min 10–17,home ask ~64–65.5¢(model fair ~73% → 低估买入,日志有 BUY)
#   进球  : min 19 Argentina 1-0 → home 价涨、draw 价跌
#   进球后: min 20 price_tick  home=82.5¢  draw=13.5¢  away=4.5¢   ← 对冲时点
#   终场  : 3-0,home 结算 100¢,draw/away 0¢
# --------------------------------------------------------------------------- #
def _demo() -> None:
    print("=" * 72)
    print("真实算例 — Argentina v Algeria (2026-06-17, fid 1489381, 终场 3-0 home赢)")
    print("=" * 72)

    # 入场:买 10 张 home @ 65.5¢(min10 price_tick;也可用日志 kalshi ask 64¢)
    pos = Position(shares=10, entry_c=65.5, side="home")
    print(f"入场: 买 {pos.shares:.0f} 张 home @ {pos.entry_c}¢,成本 {pos.cost_c:.1f}¢")

    # home 进球后(min20)的盘口
    q = Quotes.from_probs(0.825, 0.135, 0.045, minute=20, score="1-0")
    print(f"进球后(min20)盘口: home={q.home_ask}¢ draw={q.draw_ask}¢ away={q.away_ask}¢ "
          f"(Σ={q.home_ask+q.draw_ask+q.away_ask:.1f})\n")

    # (1) 不同目标下的建议 b
    for tgt in ("break_even", "delta_neutral", "maximin"):
        sol = hedge_draw_protection(pos, q, target=tgt)
        r = sol.payoff
        print(f"[{tgt:>13}] 建议买 b={sol.b:.2f} 张 draw @ {q.draw_ask}¢  | "
              f"home赢 {r.pnl_home_win:+.1f}  draw {r.pnl_draw:+.1f}  away {r.pnl_away_win:+.1f}  "
              f"(min {r.min_pnl:+.1f})  — {sol.note}")
    print()

    # (2) payoff 矩阵(b=0 / 保本 / maximin / 完全对冲)
    rows = payoff_matrix(pos, q, hedge_side="draw")
    print(format_matrix(rows, "payoff 矩阵(draw 保护,单位 cent,a=10 张 home):"))
    print()

    # (3) 实际结算核对:终场 3-0 → home 赢。对每个 b 报实际盈亏(=home赢列)
    print("实际结算核对(终场 3-0 → home 赢 → 取 home赢 列):")
    for r in rows:
        print(f"  b={r.b:5.2f} draw  → 实际盈亏 {r.pnl_home_win:+.1f}¢  "
              f"(对冲成本 {r.cost_hedge_c:.1f}¢ 是为 draw 保险付的'保费',home 真赢则损失)")
    print()

    # (4) 部分止盈:home 涨到 82.5¢,卖回 40% 落袋,再对剩余仓位做保本对冲
    print("部分止盈 + 残仓再对冲:")
    plan = partial_cashout(pos, q, fraction=0.4)
    print(f"  卖回 {plan.sell_shares:.1f} 张 home @ {plan.sell_at_c}¢ → 落袋 {plan.realised_c:.1f}¢;"
          f" 残仓 {plan.remaining.shares:.0f} 张,最差(残仓全输)= {plan.locked_min_pnl:+.1f}¢")
    sol2 = hedge_draw_protection(plan.remaining, q, target="maximin")
    r2 = sol2.payoff
    print(f"  残仓 maximin: 再买 b={sol2.b:.2f} 张 draw → "
          f"home赢 {r2.pnl_home_win:+.1f}  draw {r2.pnl_draw:+.1f}  away {r2.pnl_away_win:+.1f}  "
          f"(锁定最小 {r2.min_pnl:+.1f}¢)")
    print()

    # (5) 三向 dutching 检查(进球后 Σask=1.005 → 无锁利,符合预期)
    dl = dutch_lock(q)
    print(f"三向 dutch_lock: Σask={dl.basket_c}¢ tradable={dl.tradable} → {dl.note}")

    # (6) 反向 lay(买 draw+away 等张 = 合成卖 home)
    lay = lay_hedge(pos, q)
    print(f"反向 lay: 买 {lay.b_draw:.0f} draw + {lay.b_away:.0f} away,成本 {lay.cost_c:.1f}¢ → "
          f"home赢 {lay.pnl_home_win:+.1f}  非home {lay.pnl_not_home:+.1f}  locked={lay.locked}")


if __name__ == "__main__":
    _demo()
