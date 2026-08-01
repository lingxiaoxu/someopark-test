"""
universe — PIT "R3K 代理" 宇宙(§7.3 细则) + 服务宇宙扩集(§6.1)
=============================================================
规则(逐条实现,不简化):
- 每年 6 月最后一个交易日为重构日,按**前 12 个月日均美元量**排名取 TOP N(3000),
  次年重构前冻结(PIT);成员表逐 vintage 落盘 outputs/universe/
- 附加过滤: 价格≥$1(重构日收盘,原始价——美元量口径下与复权无关的对比用原始);
  普通股/ADR(type∈{CS,ADRC},Polygon reference PIT 快照);IPO 满 60 交易日
- 退市自然保留(grouped 原始 bar 含全体)→ 幸存者控制
- 服务宇宙 = 当期 R3K 代理 ∪ config.service_extra(ETF 打 etf 旗标)∪ 四策略动态标的
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import requests

from VolumePrediction.common import REPO, DATA_ROOT, OUT, load_config, get_logger
from VolumePrediction.data import polygon_loader as pl

log = get_logger("universe")
REF_DIR = DATA_ROOT / "reference"
UNI_DIR = OUT / "universe"


def rebalance_day(year: int) -> str:
    """该年 6 月最后一个 NYSE 交易日。"""
    days = pl.trading_days(f"{year}-06-01", f"{year}-06-30")
    return days[-1]


def _ref_snapshot_path(asof: str) -> Path:
    return REF_DIR / f"tickers_{asof}.parquet"


def fetch_reference_snapshot(asof: str, force: bool = False) -> pd.DataFrame:
    """Polygon v3/reference/tickers 全量 PIT 快照(date=asof)→ 缓存。
    字段: ticker/type/active/list_date/primary_exchange。分页全取。"""
    p = _ref_snapshot_path(asof)
    if p.exists() and not force:
        return pd.read_parquet(p)
    from VolumePrediction.data.polygon_loader import _api_key, _sanitize
    REF_DIR.mkdir(parents=True, exist_ok=True)
    rows, url = [], "https://api.polygon.io/v3/reference/tickers"
    params = {"market": "stocks", "date": asof, "limit": 1000,
              "apiKey": _api_key()}
    n_page = 0
    while url:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        body = r.json()
        for t in body.get("results") or []:
            rows.append((t.get("ticker"), t.get("type"), t.get("active"),
                         t.get("list_date"), t.get("primary_exchange")))
        url = body.get("next_url")
        params = {"apiKey": _api_key()}
        n_page += 1
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ticker", "type", "active",
                                     "list_date", "exchange"])
    tmp = p.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.rename(p)
    log.info(f"reference snapshot {asof}: {len(df)} tickers, {n_page} pages")
    return df


def build_vintage(year: int, top_n: Optional[int] = None,
                  force: bool = False) -> pd.DataFrame:
    """构建 year 年 6 月重构的成员表(生效 [重构日+1, 次年重构日])。"""
    cfg = load_config()["universe"]
    top_n = top_n or cfg["top_n"]
    asof = rebalance_day(year)
    out_p = UNI_DIR / f"vintage_{year}.parquet"
    if out_p.exists() and not force:
        return pd.read_parquet(out_p)

    lookback_start = (pd.Timestamp(asof) - pd.DateOffset(months=12)).strftime("%Y-%m-%d")
    px = pl.load_range(lookback_start, asof)
    if px.empty:
        raise RuntimeError(f"raw cache insufficient for vintage {year} "
                           f"({lookback_start}→{asof}); run backfill first")
    # 前 12 个月日均美元量
    adv = (px.groupby("ticker")["dollar_volume"].agg(["mean", "count"])
             .rename(columns={"mean": "adv_dollar", "count": "n_days"}))
    # IPO 满 60 交易日(以样本内出现天数近似 PIT 上市时长下限——不足 60 天必为新上市)
    adv = adv[adv["n_days"] >= cfg["ipo_seasoning_days"]]
    # 重构日原始收盘价 ≥ $1
    day_px = pl.load_day(asof).set_index("ticker")
    adv = adv.join(day_px["c"].rename("close_asof"), how="inner")
    adv = adv[adv["close_asof"] >= cfg["min_price"]]
    # 类型过滤(PIT reference 快照)
    ref = fetch_reference_snapshot(asof)
    typed = ref.set_index("ticker")["type"]
    adv = adv.join(typed, how="left")
    adv = adv[adv["type"].isin(cfg["types_allowed"])]
    members = (adv.sort_values("adv_dollar", ascending=False)
                  .head(top_n).reset_index())
    members["vintage_year"] = year
    members["effective_from"] = asof
    UNI_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out_p.with_suffix(".tmp")
    members.to_parquet(tmp, index=False)
    tmp.rename(out_p)
    log.info(f"vintage {year}: {len(members)} members (asof {asof})")
    return members


_VINTAGE_CACHE: Dict[int, Set[str]] = {}


def _load_vintage_members(year: int) -> Set[str]:
    if year not in _VINTAGE_CACHE:
        p = UNI_DIR / f"vintage_{year}.parquet"
        if not p.exists():
            alt = UNI_DIR / f"vintage_{year + 1}.parquet"
            if alt.exists():
                log.warning(f"vintage {year} missing → 用 {year+1} 回溯代理(P0 留档)")
                _VINTAGE_CACHE[year] = set(pd.read_parquet(alt)["ticker"])
                return _VINTAGE_CACHE[year]
            raise FileNotFoundError(f"vintage {year} not built")
        _VINTAGE_CACHE[year] = set(pd.read_parquet(p)["ticker"])
    return _VINTAGE_CACHE[year]


def membership(d: str | date) -> Set[str]:
    """给定日期 → 生效 vintage 的成员集合。2019 年样本起点用 2018 vintage
    (若 2018 原始数据不足则用 2019 vintage 回溯代理并标注——P0 审计核)。"""
    y = pd.Timestamp(d).year
    reb = rebalance_day(y) if y >= 2019 else None
    vintage_year = y if (reb and str(pd.Timestamp(d).date()) > reb) else y - 1
    return _load_vintage_members(vintage_year)


def strategy_symbols() -> Dict[str, Set[str]]:
    """四策略动态标的(只读 consume_paths)+ 静态 ETF 清单。"""
    cfg = load_config()
    cp = cfg["consume_paths"]
    out: Dict[str, Set[str]] = {"etf": set(cfg["universe"]["service_extra"]["etfs"])}
    pairs: Set[str] = set()
    for f in cp["pair_universe"]:
        p = REPO / f
        if p.exists():
            try:
                d = json.loads(p.read_text())
                # 顶层可能是 list(pair_universe_mrpt/mtfs 实际格式)或 {"pairs": [...]}
                prs = d if isinstance(d, list) else d.get("pairs", [])
                for pr in prs:
                    if isinstance(pr, dict):
                        pairs |= {pr.get("s1"), pr.get("s2")}
                    elif isinstance(pr, str) and "/" in pr:
                        pairs |= set(pr.split("/"))
            except Exception as e:  # noqa: BLE001
                log.warning(f"pair universe read {f}: {e}")
    out["pairs"] = {x for x in pairs if x}
    inv_aiss = REPO / cp["inventories"]["aiss"]
    aiss: Set[str] = set()
    if inv_aiss.exists():
        try:
            d = json.loads(inv_aiss.read_text())
            aiss |= set((d.get("stock_holdings") or {}).keys())
            for h in (d.get("holdings") or {}).values():
                if isinstance(h, dict):
                    aiss |= set((h.get("stocks") or {}).keys())
        except Exception as e:  # noqa: BLE001
            log.warning(f"aiss inventory read: {e}")
    out["aiss"] = aiss
    return out


def service_universe(d: str | date) -> pd.DataFrame:
    """当日服务宇宙: [ticker, in_r3k, is_etf, source]。"""
    r3k = membership(d)
    strat = strategy_symbols()
    rows = [(t, True, False, "r3k") for t in r3k]
    extra = (strat["etf"] | strat["pairs"] | strat["aiss"]) - r3k
    for t in sorted(extra):
        rows.append((t, False, t in strat["etf"], "strategy"))
    return pd.DataFrame(rows, columns=["ticker", "in_r3k", "is_etf", "source"])
