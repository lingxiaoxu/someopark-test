"""ticker_aliases — 股票改名的仓库级统一处理(2026-08-15;BK→BNY 实证驱动)。

问题:改名(BK→BNY 2026-05-21、FB→META 2022-06-09)把一只票的历史劈成两段,
回测端历史断裂(新名像新上市、旧名像退市),实盘端旧名取价直接落空。

**关键坑(实测)**:旧代码会被回收 —— "FB" 自 2025-06-26 起是 ProShares 的
一只 ETF,与 META 无关。所以别名绝不能是无日期的 {old: new} 平面映射,
必须**带生效日期窗口 + 身份锚(FIGI/CIK)**:
    old 名只在 date < changed 时属于该实体;date >= changed 后 old 名要么
    无效、要么已被别的实体占用。

数据源:Polygon /vX/reference/tickers/{id}/events(权威改名事件链;
id 可用 ticker 或 CIK)。旧名直查可能 404 或命中回收实体 → 回退用
controller security master 的 CIK 再查(controller/registry/security_master.json
以 ISIN 锚身份,天然带 ticker_history)。

三个消费面(统一走本模块,谁都不许自铺平面映射):
  1) 历史归一(回测/特征): normalize_day_frame(df, date) —— date < changed
     的行 old→current,历史在 current 名下连续;
  2) 查询解析(实盘/服务): resolve(ticker, date) —— 旧名查询转 current;
  3) 发现(BAU): refresh_aliases(candidates) —— 消费票从 raw 消失时自动
     查证并落盘 ticker_aliases.json(VP daily 链挂钩,非致命)。

文件: ticker_aliases.json(仓库根,git 入库;原子写)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
ALIAS_PATH = _ROOT / "ticker_aliases.json"

_CACHE: dict = {"mtime": None, "data": {}}
_MAX_HOPS = 5      # 链式改名护栏(A→B→C…)


def load_aliases() -> dict:
    """{old_ticker: {current, changed, figi, cik, verified, asof}}(mtime 缓存)。"""
    try:
        mt = ALIAS_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _CACHE["mtime"] != mt:
        try:
            _CACHE["data"] = json.loads(ALIAS_PATH.read_text()).get("aliases", {})
            _CACHE["mtime"] = mt
        except Exception:  # noqa: BLE001 — 破损文件按无别名处理(不阻塞任何管道)
            _CACHE["data"], _CACHE["mtime"] = {}, None
    return _CACHE["data"]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def resolve(ticker: str, date: str | None = None) -> str:
    """**市场名语义**(历史工件查询用: 工件不可变,行内是"当日市场名"):
    该日市场上这个名字应写成什么。
      date < changed            → 原名(当日市场就叫这个)
      changed ≤ date < recycled → 现名(空窗期,旧名只可能指本实体)
      date ≥ recycled           → 原名(名字已被**另一实体**回收 —— FB 型:
                                   2025-06-26 起 FB=ProShares ETF,再映射
                                   META 就是张冠李戴,必须止步)
    date=None 视为今天。"""
    al = load_aliases()
    d = date or _today()
    cur = ticker
    for _ in range(_MAX_HOPS):
        e = al.get(cur)
        if not e or d < e["changed"] or (e.get("recycled") and d >= e["recycled"]):
            return cur
        cur = e["current"]
    return cur


def canonical(ticker: str, date: str | None = None) -> str:
    """**归一数据语义**(_load_day/load_day 已把历史行 old→current):
    "date 当日这个名字指的实体"的**现行规范名** —— 归一后数据全在现名下,
    改名前的历史日期也必须给现名(resolve 会给旧名 → 在归一数据里查空,
    2026-08-15 复审抓到的错位 bug)。回收后名字属新实体 → 原名。"""
    al = load_aliases()
    d = date or _today()
    cur = ticker
    for _ in range(_MAX_HOPS):
        e = al.get(cur)
        if not e or (e.get("recycled") and d >= e["recycled"]):
            return cur
        cur = e["current"]
    return cur


def rename_map(date: str) -> dict:
    """历史归一: 该日应把哪些 old→current(仅 date < changed 的条目——
    改名日后 old 名或无效或已被回收实体占用,绝不再映射)。"""
    return {old: e["current"] for old, e in load_aliases().items()
            if date < e["changed"]}


def normalize_day_frame(df, date: str, col: str = "ticker"):
    """日级数据帧的 old→current 归一(零匹配时零成本返回原帧)。

    碰撞守卫: 若该日帧里 current 名**已存在**(历史上被另一实体占用过),
    改名会造出重复票行 —— pivot(aggfunc='first')会静默取一污染数据,
    宁可不归一并大声告警。"""
    m = rename_map(date)
    if not m or df is None or len(df) == 0 or col not in df.columns:
        return df
    present = set(df[col])
    clash = {o: c for o, c in m.items() if o in present and c in present}
    if clash:
        import logging
        logging.getLogger("ticker_aliases").warning(
            f"normalize collision on {date}: {clash} — 这些票**不**归一"
            f"(current 名当日已被占用,改名会造重复行)")
        m = {o: c for o, c in m.items() if o not in clash}
        if not m:
            return df
    hit = df[col].isin(m.keys())
    if not hit.any():
        return df
    df = df.copy()
    df.loc[hit, col] = df.loc[hit, col].map(m)
    return df


# ── 发现(BAU): Polygon events 查证 + controller master CIK 回退 ─────────────

def _events(session, id_: str, key: str) -> dict | None:
    r = session.get(f"https://api.polygon.io/vX/reference/tickers/{id_}/events",
                    params={"apiKey": key, "types": "ticker_change"}, timeout=20)
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("results") or None


def _master_ids(ticker: str) -> list[str]:
    """controller security master 里该 ticker 的身份锚(CIK/CUSIP,按优先序)。
    退市旧名的 Polygon reference 查不到 → cik 常为空,但 FTD 路径的 CUSIP
    必在(ISIN 去国别码与校验位即 CUSIP9,BK 实证)。"""
    p = _ROOT / "controller" / "registry" / "security_master.json"
    ids: list[str] = []
    try:
        for isin, rec in json.loads(p.read_text()).items():
            if rec.get("polygon_ticker") == ticker:
                if rec.get("cik"):
                    ids.append(str(rec["cik"]))
                if rec.get("cusip"):
                    ids.append(str(rec["cusip"]))
                elif isin.startswith("US") and len(isin) == 12:
                    ids.append(isin[2:11])       # ISIN 中段 = CUSIP9
    except Exception:  # noqa: BLE001
        pass
    return ids


def refresh_aliases(candidates: list[str], api_key: str | None = None) -> dict:
    """候选旧名逐个查证,确证改名的写入 ticker_aliases.json(原子,幂等)。
    → {"added": {...}, "unresolved": [...]}。任何单票失败不影响其余。"""
    import requests
    key = api_key or os.environ.get("POLYGON_API_KEY")
    if not key:
        return {"added": {}, "unresolved": list(candidates),
                "note": "no POLYGON_API_KEY"}
    al = dict(load_aliases())
    added, unresolved = {}, []
    s = requests.Session()
    for t in candidates:
        if t in al:
            continue
        try:
            res = _events(s, t, key)
            entry = _entry_from_events(res, t)
            if entry is None:                    # 404 / 名字已被回收实体占用
                for id_ in _master_ids(t):
                    entry = _entry_from_events(_events(s, id_, key), t)
                    if entry:
                        break
            if entry:
                # 回收日探测(FB 型): 旧名直查若命中**另一实体**(FIGI/CIK
                # 不同),该实体拿走此名之日起,查询解析必须止步 —— 否则
                # "今天的 FB" 会被错映射到 META。
                entry["recycled"] = _recycled_date(res, entry, t)
                al[t] = entry
                added[t] = entry
            else:
                unresolved.append(t)
        except Exception:  # noqa: BLE001
            unresolved.append(t)
    if added:
        payload = {"schema": "v1",
                   "updated": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"),
                   "aliases": al}
        tmp = ALIAS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
        tmp.replace(ALIAS_PATH)
        _CACHE["mtime"] = None                   # 强制下次重读
    return {"added": added, "unresolved": unresolved}


RECHECK_DAYS = 7     # 已入册条目的回收复查周期(旧名随时可能被新实体启用)


def recheck_recycled(api_key: str | None = None) -> dict:
    """已入册且未标回收的条目,周期性直查旧名现属谁(BK 型: 今天 404 无主,
    未来某实体启用 "BK" 时必须止住 resolve,否则继续错给 BNY)。
    每条目至多 RECHECK_DAYS 一次(recycled_checked 戳),fail-open。"""
    import requests
    key = api_key or os.environ.get("POLYGON_API_KEY")
    if not key:
        return {"checked": [], "note": "no POLYGON_API_KEY"}
    al = dict(load_aliases())
    today = _today()
    checked, found = [], {}
    s = requests.Session()
    dirty = False
    for old, e in al.items():
        if e.get("recycled"):
            continue
        last = e.get("recycled_checked", "1970-01-01")
        if (datetime.strptime(today, "%Y-%m-%d")
                - datetime.strptime(last, "%Y-%m-%d")).days < RECHECK_DAYS:
            continue
        try:
            rec = _recycled_date(_events(s, old, key), e, old)
            e["recycled_checked"] = today
            if rec:
                e["recycled"] = rec
                found[old] = rec
            checked.append(old)
            dirty = True
        except Exception:  # noqa: BLE001 — 单票失败下轮再试
            continue
    if dirty:
        payload = {"schema": "v1",
                   "updated": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"),
                   "aliases": al}
        tmp = ALIAS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
        tmp.replace(ALIAS_PATH)
        _CACHE["mtime"] = None
    return {"checked": checked, "recycled_found": found}


def _recycled_date(direct_res: dict | None, entry: dict, old: str) -> str | None:
    """旧名直查结果若是**另一实体**(FIGI/CIK 与 entry 不同)→ 返回该实体
    拿走此名的日期(其事件链中 ticker==old 的最新事件);否则 None。"""
    if not direct_res:
        return None                              # 404: 旧名当前无主(BK 型)
    same = (direct_res.get("composite_figi") == entry.get("figi")
            or (direct_res.get("cik") and direct_res.get("cik") == entry.get("cik")))
    if same:
        return None
    dates = [e.get("date") for e in direct_res.get("events", [])
             if e.get("type") == "ticker_change"
             and e.get("ticker_change", {}).get("ticker") == old]
    return max(dates) if dates else None


def _entry_from_events(res: dict | None, old: str) -> dict | None:
    """events 结果 → 别名条目。判据: 事件链里 old 是**历史**名,且最新事件
    的名 != old(最新即 current)。回收实体(如今日 FB=ETF)在此天然出局:
    其事件链里 old 就是最新名。"""
    if not res:
        return None
    evs = sorted((e for e in res.get("events", [])
                  if e.get("type") == "ticker_change"),
                 key=lambda e: e.get("date", ""))
    if not evs:
        return None
    names = [e["ticker_change"]["ticker"] for e in evs]
    if old not in names[:-1] or names[-1] == old:
        return None
    return {"current": names[-1], "changed": evs[-1]["date"],
            "figi": res.get("composite_figi"), "cik": res.get("cik"),
            "name": res.get("name"), "verified": "polygon_events",
            "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
