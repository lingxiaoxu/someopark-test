"""
controller/registry.py — 标识体系(plan §2.5)+ security master + node registry。

叶子(个股/ETF/BDC 股)= ISIN(US+CUSIP9+Luhn 校验位)。CUSIP 来源:
  ① Polygon reference(当前订阅不含 cusip,字段留空自动升级)
  ② SEC CNS Fails-to-Deliver 公开档(ticker→CUSIP,多档叠加;2026-08-12 实测
     全书 220 票 100% 覆盖)
  仍缺 → 'XF'+FIGI 派生占位(XF 非法定国别码,机器可识别)+ 大声报警人工补录。

非叶(portfolio/strategy/pair/subsector)= SPID,11 字符定长:
  SP + TT(PF/ST/PR/SS) + 6 位 base36(canonical key 的 sha1 派生,冲突线性探测)
  + 1 位 base36-Luhn 校验位。
身份锚定 canonical key(§2.5.2/2.5.2a):
  PF: "PORTFOLIO"                      ST: 策略代号
  PR: "{strategy}|L:{多腿ISIN}|S:{空腿ISIN}"(方向有序;由 direction+shares 符号
      规范化,矛盾 ABORT——见 normalize_pair_direction)
  SS: "{strategy}|SS:{qlib config 子板块键}"

纪律:全部文件只在 controller/ 下;对外只读数据文件与 API;解析不出即 raise,
绝不静默 fallback。registry 数据 append-only,ID 永不复用,退役不删。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
REG_DIR = os.path.join(_HERE, "registry")
MASTER_PATH = os.path.join(REG_DIR, "security_master.json")
NODES_PATH = os.path.join(REG_DIR, "node_registry.json")
CHANGELOG = os.path.join(REG_DIR, "changelog.jsonl")

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
STRATEGIES = ("mrpt", "mtfs", "aiss", "ssrs", "bdc")


class RegistryError(RuntimeError):
    """解析/校验失败 —— 装配层收到必须 abort,不容错。"""


# ═══════════════════════ ISIN(叶子)═══════════════════════════════════════

def isin_check_digit(body11: str) -> str:
    """ISIN 标准校验位(Luhn over 字母展开)。body11 = 2 位国别 + 9 位 NSIN。"""
    if len(body11) != 11:
        raise RegistryError(f"ISIN body must be 11 chars, got {body11!r}")
    digits = ""
    for ch in body11:
        if ch.isdigit():
            digits += ch
        elif ch.isalpha():
            digits += str(ord(ch.upper()) - 55)      # A=10 … Z=35
        else:
            raise RegistryError(f"bad ISIN char {ch!r} in {body11!r}")
    total = 0
    # Luhn:从右往左,奇数位(最右为第 1 位)×2
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def isin_from_cusip(cusip9: str, country: str = "US") -> str:
    cusip9 = cusip9.strip().upper()
    if not re.fullmatch(r"[0-9A-Z@#*]{9}", cusip9):
        raise RegistryError(f"bad CUSIP {cusip9!r}")
    body = country + cusip9
    return body + isin_check_digit(body)


def validate_isin(isin: str) -> bool:
    return (len(isin) == 12 and isin[:11].isalnum()
            and isin[-1] == isin_check_digit(isin[:11]))


# ═══════════════════════ SPID(非叶)═══════════════════════════════════════

_KIND_TT = {"portfolio": "PF", "strategy": "ST", "pair": "PR", "subsector": "SS"}
_TT_KIND = {v: k for k, v in _KIND_TT.items()}


def _b36_luhn(payload: str) -> str:
    """base36 字符集上的 Luhn 变体校验字符(SP 前缀在内一起校)。"""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        v = _B36.index(ch)
        if i % 2 == 0:
            v *= 2
            v = v // 36 + v % 36
        total += v
    return _B36[(36 - total % 36) % 36]


def _payload6(canonical_key: str, probe: int = 0) -> str:
    h = hashlib.sha1(f"{canonical_key}#{probe}".encode()).hexdigest()
    n = int(h[:12], 16)
    out = ""
    for _ in range(6):
        out = _B36[n % 36] + out
        n //= 36
    return out


def make_spid(kind: str, canonical_key: str, probe: int = 0) -> str:
    tt = _KIND_TT.get(kind)
    if tt is None:
        raise RegistryError(f"unknown node kind {kind!r}")
    body = "SP" + tt + _payload6(canonical_key, probe)
    return body + _b36_luhn(body)


def validate_spid(spid: str) -> bool:
    return (len(spid) == 11 and spid.startswith("SP")
            and spid[2:4] in _TT_KIND
            and all(c in _B36 for c in spid[4:])
            and spid[-1] == _b36_luhn(spid[:10]))


# ═══════════════ pair 方向规范化(§2.5.2a,ABORT 语义)═══════════════════════

def normalize_pair_direction(direction: str, s1_shares, s2_shares):
    """(direction, s1_shares, s2_shares) → ('long_is_s1'|'long_is_s2')。
    交叉校验 direction 与 shares 符号,矛盾即 raise(绝不猜方向)。
    实证口径:long → s1>0,s2<0;short → s1<0,s2>0(F/PG: short,-6426/+500)。"""
    if direction == "long":
        expect = (s1_shares is None or s1_shares > 0) and (s2_shares is None or s2_shares < 0)
        if not expect:
            raise RegistryError(
                f"direction=long but shares signs s1={s1_shares}, s2={s2_shares}")
        return "long_is_s1"
    if direction == "short":
        expect = (s1_shares is None or s1_shares < 0) and (s2_shares is None or s2_shares > 0)
        if not expect:
            raise RegistryError(
                f"direction=short but shares signs s1={s1_shares}, s2={s2_shares}")
        return "long_is_s2"
    raise RegistryError(f"unknown pair direction {direction!r}")


def pair_canonical_key(strategy: str, s1_isin: str, s2_isin: str,
                       direction: str, s1_shares, s2_shares) -> str:
    side = normalize_pair_direction(direction, s1_shares, s2_shares)
    long_leg, short_leg = ((s1_isin, s2_isin) if side == "long_is_s1"
                           else (s2_isin, s1_isin))
    return f"{strategy}|L:{long_leg}|S:{short_leg}"


def strategy_canonical_key(strategy: str) -> str:
    if strategy not in STRATEGIES:
        raise RegistryError(f"unknown strategy {strategy!r}")
    return f"strategy:{strategy}"


def subsector_canonical_key(strategy: str, subsector_cfg_key: str) -> str:
    return f"{strategy}|SS:{subsector_cfg_key}"


PORTFOLIO_KEY = "PORTFOLIO"


# ═══════════════════════ 持久层(append-only)══════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: str) -> dict:
    return json.load(open(path)) if os.path.exists(path) else {}


def _atomic_dump(obj, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, path)


def _log_change(event: str, **kw) -> None:
    os.makedirs(REG_DIR, exist_ok=True)
    with open(CHANGELOG, "a") as fh:
        fh.write(json.dumps({"ts": _now(), "event": event, **kw}) + "\n")


class Registry:
    """security master + node registry 的统一读写口(装配层唯一入口)。"""

    def __init__(self):
        self.master: dict = _load(MASTER_PATH)          # isin -> {…}
        self.nodes: dict = _load(NODES_PATH)            # spid -> {…}
        self._by_ticker = {v["polygon_ticker"]: k for k, v in self.master.items()}
        self._by_key = {v["canonical_key"]: k for k, v in self.nodes.items()}

    # ── 叶子解析 ────────────────────────────────────────────────────────────
    def isin_of(self, ticker: str) -> str:
        isin = self._by_ticker.get(ticker)
        if isin is None:
            raise RegistryError(
                f"ticker {ticker!r} not in security master — run "
                f"`python -m controller.registry --build-master` (new position?)")
        return isin

    def register_security(self, ticker: str, cusip: str | None,
                          figi: str | None, cik, name: str,
                          asset_class: str) -> str:
        if cusip:
            isin = isin_from_cusip(cusip)
        elif figi:
            # 占位:XF + FIGI 后 9 位 + 校验位(machine-detectable,须人工补录)
            body = "XF" + re.sub(r"[^0-9A-Z]", "", figi.upper())[-9:].rjust(9, "0")
            isin = body + isin_check_digit(body)
            print(f"!!!! [registry ALERT] {ticker}: no CUSIP anywhere — placeholder "
                  f"{isin}; manual backfill required")
        else:
            raise RegistryError(f"{ticker}: neither CUSIP nor FIGI — cannot identify")
        prev = self.master.get(isin)
        if prev and prev["polygon_ticker"] != ticker:
            # ticker 漂移:报警,人工确认路径(此处只记录,不自动改)
            print(f"!!!! [registry ALERT] ISIN {isin} ticker drift: "
                  f"{prev['polygon_ticker']} -> {ticker} (manual confirm)")
        self.master[isin] = {
            "cusip": cusip, "figi": figi, "cik": cik, "name": name,
            "asset_class": asset_class, "polygon_ticker": ticker,
            "ticker_history": (prev or {}).get("ticker_history",
                                               [{"ticker": ticker, "from": _now()[:10], "to": None}]),
            "status": "active",
            "registered_at": (prev or {}).get("registered_at", _now()),
        }
        self._by_ticker[ticker] = isin
        if not prev:
            _log_change("register_security", isin=isin, ticker=ticker, cusip=cusip)
        return isin

    # ── 非叶解析/注册 ───────────────────────────────────────────────────────
    def spid_of(self, kind: str, canonical_key: str,
                display_name: str | None = None,
                attrs: dict | None = None,
                register_if_new: bool = True) -> str:
        spid = self._by_key.get(canonical_key)
        if spid:
            node = self.nodes[spid]
            if node["status"] == "retired":
                node["status"] = "active"
                node["retired_at"] = None
                _log_change("reactivate", spid=spid, key=canonical_key)
            if display_name and display_name not in node["aliases"]:
                node["aliases"].append(display_name)
            return spid
        if not register_if_new:
            raise RegistryError(f"node not registered: {canonical_key!r}")
        probe = 0
        while True:
            spid = make_spid(kind, canonical_key, probe)
            if spid not in self.nodes:
                break
            probe += 1                                   # 冲突线性探测(确定性)
        self.nodes[spid] = {
            "kind": kind, "canonical_key": canonical_key,
            "display_name": display_name or canonical_key,
            "aliases": [display_name] if display_name else [],
            "attrs": attrs or {}, "status": "active",
            "first_seen": _now(), "retired_at": None, "probe": probe,
        }
        self._by_key[canonical_key] = spid
        _log_change("register_node", spid=spid, kind=kind, key=canonical_key,
                    display=display_name)
        return spid

    def retire(self, spid: str) -> None:
        node = self.nodes.get(spid)
        if node and node["status"] == "active":
            node["status"] = "retired"
            node["retired_at"] = _now()
            _log_change("retire", spid=spid, key=node["canonical_key"])

    def render(self, node_id: str) -> str:
        """显示边界:ID → 人读名(§2.5.4)。"""
        if node_id in self.nodes:
            return self.nodes[node_id]["display_name"]
        if node_id in self.master:
            return self.master[node_id]["polygon_ticker"]
        raise RegistryError(f"unknown id {node_id!r}")

    def save(self) -> None:
        _atomic_dump(self.master, MASTER_PATH)
        _atomic_dump(self.nodes, NODES_PATH)


# ═══════════════════ security master 构建(数据获取)═══════════════════════

_SEC_UA = {"User-Agent": "someopark-research controller admin@someopark.com"}
_FTD_INDEX = "https://www.sec.gov/data/foiadocsfailsdatahtm"


def collect_universe() -> list[str]:
    """全书 ticker 并集(五策略持仓文件,只读;含 pairs 候选池与 AISS 全宇宙)。"""
    import yaml
    tickers: set[str] = set()
    for s in ("mrpt", "mtfs"):
        inv = json.load(open(os.path.join(REPO, f"inventory_{s}.json")))
        for name in inv["pairs"]:
            legs = name.split("/")
            if len(legs) != 2:
                raise RegistryError(f"unparseable pair name {name!r}")
            tickers.update(legs)
        tickers.update(json.load(open(os.path.join(REPO, f"account_{s}.json")))
                       .get("positions", {}))
    tickers.update(json.load(open(os.path.join(
        REPO, "qlib-main/semiconductor_strategy/account_aiss.json")))["positions"])
    cfg = yaml.safe_load(open(os.path.join(
        REPO, "qlib-main/semiconductor_strategy/config.yaml")))
    for names in cfg["universe"]["subsectors"].values():
        tickers.update(names)
    tickers.update(json.load(open(os.path.join(
        REPO, "qlib-main/sector_rotation/account_ssrs.json")))["positions"])
    tickers.update(json.load(open(os.path.join(
        REPO, "qlib-main/sector_rotation/inventory_sector_rotation.json")))["holdings"])
    binv = json.load(open(os.path.join(REPO, "inventory_bdc.json")))
    tickers.update(binv["holdings"])
    tickers.add(binv["cash"]["ticker"])
    return sorted(tickers)


def fetch_ftd_cusip_map(max_files: int = 6) -> dict[str, str]:
    """SEC CNS fails-to-deliver 公开档(多档叠加)→ {ticker: cusip}。"""
    import requests
    r = requests.get(_FTD_INDEX, headers=_SEC_UA, timeout=30)
    r.raise_for_status()
    links = re.findall(r'href="(/files/data/[^"]*fails[^"]*\.zip)"', r.text)[:max_files]
    if not links:
        raise RegistryError("no FTD archives found on SEC index page")
    out: dict[str, str] = {}
    for link in links:
        rr = requests.get("https://www.sec.gov" + link, headers=_SEC_UA, timeout=90)
        if rr.status_code != 200:
            continue
        z = zipfile.ZipFile(io.BytesIO(rr.content))
        raw = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
        for line in raw.splitlines()[1:]:
            p = line.split("|")
            if len(p) >= 3 and len(p[1].strip()) == 9:
                out.setdefault(p[2].strip(), p[1].strip())
    return out


def build_master() -> dict:
    """构建/刷新 security master:FTD CUSIP + Polygon reference,全宇宙注册。"""
    import requests
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        raise RegistryError("POLYGON_API_KEY not visible (source .env)")
    reg = Registry()
    universe = collect_universe()
    cusips = fetch_ftd_cusip_map()
    stats = {"total": len(universe), "cusip_hit": 0, "placeholder": 0}
    for t in universe:
        r = requests.get(f"https://api.polygon.io/v3/reference/tickers/{t}",
                         params={"apiKey": key}, timeout=15)
        d = (r.json() or {}).get("results", {}) if r.status_code == 200 else {}
        cusip = d.get("cusip") or cusips.get(t)          # Polygon 优先(将来订阅升级)
        figi = d.get("composite_figi")
        asset_class = {"CS": "equity", "ETF": "etf"}.get(d.get("type"), "equity")
        # BDC 股票标记(与 inventory_bdc 对齐)
        if t in json.load(open(os.path.join(REPO, "inventory_bdc.json")))["holdings"]:
            asset_class = "bdc_equity"
        if cusip:
            stats["cusip_hit"] += 1
        elif figi:
            stats["placeholder"] += 1
        reg.register_security(t, cusip, figi, d.get("cik"),
                              d.get("name", t), asset_class)
    reg.save()
    _log_change("build_master", **stats)
    print(f"[registry] master built: {stats}")
    return stats


def seed_nodes() -> dict:
    """注册 PF/ST 固定节点 + 当前结构里的 pair/subsector。"""
    import yaml
    reg = Registry()
    reg.spid_of("portfolio", PORTFOLIO_KEY, display_name="PORTFOLIO")
    for st in STRATEGIES:
        reg.spid_of("strategy", strategy_canonical_key(st), display_name=st.upper())
    n_pairs = 0
    for st in ("mrpt", "mtfs"):
        inv = json.load(open(os.path.join(REPO, f"inventory_{st}.json")))
        for name, v in inv["pairs"].items():
            if not v.get("direction"):
                continue                                  # 未开仓不注册,首开时注册
            s1, s2 = name.split("/")
            key = pair_canonical_key(st, reg.isin_of(s1), reg.isin_of(s2),
                                     v["direction"], v.get("s1_shares"),
                                     v.get("s2_shares"))
            reg.spid_of("pair", key, display_name=name,
                        attrs={"strategy": st})
            n_pairs += 1
    cfg = yaml.safe_load(open(os.path.join(
        REPO, "qlib-main/semiconductor_strategy/config.yaml")))
    for ss in cfg["universe"]["subsectors"]:
        reg.spid_of("subsector", subsector_canonical_key("aiss", ss),
                    display_name=ss, attrs={"strategy": "aiss"})
    reg.save()
    out = {"nodes": len(reg.nodes), "pairs_registered": n_pairs}
    print(f"[registry] nodes seeded: {out}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="controller registry (plan §2.5)")
    ap.add_argument("--build-master", action="store_true")
    ap.add_argument("--seed-nodes", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="全表自校验(check digits + 索引一致性)")
    a = ap.parse_args()
    if a.build_master:
        build_master()
    if a.seed_nodes:
        seed_nodes()
    if a.verify:
        reg = Registry()
        bad = [i for i in reg.master if not (validate_isin(i) or i.startswith("XF"))]
        bad += [s for s in reg.nodes if not validate_spid(s)]
        dup = len(reg.nodes) != len({v["canonical_key"] for v in reg.nodes.values()})
        print(f"[verify] master={len(reg.master)} nodes={len(reg.nodes)} "
              f"bad_ids={bad} dup_keys={dup}")
        raise SystemExit(1 if (bad or dup) else 0)
