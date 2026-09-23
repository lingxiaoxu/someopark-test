"""registry 单测(plan §六 ID 体系条目)。输出只进 /tmp。
运行: conda run -n someopark_run python -m pytest controller/tests/test_registry.py -q
  或: conda run -n someopark_run python controller/tests/test_registry.py
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from controller.registry import (isin_check_digit, isin_from_cusip, validate_isin,
                                 make_spid, validate_spid, _b36_luhn,
                                 normalize_pair_direction, pair_canonical_key,
                                 RegistryError)

N = 0
def ok(name, cond):
    global N
    assert cond, f"FAILED: {name}"
    N += 1
    print(f"ok - {name}")


# ── ISIN:对拍公开已知值 ─────────────────────────────────────────────────────
KNOWN = {  # (cusip, isin) 公开事实
    "037833100": "US0378331005",   # AAPL
    "594918104": "US5949181045",   # MSFT
    "458140100": "US4581401001",   # INTC
    "064058100": "US0640581007",   # BK (BNY Mellon)
}
for cusip, isin in KNOWN.items():
    ok(f"isin_from_cusip {cusip} -> {isin}", isin_from_cusip(cusip) == isin)
    ok(f"validate_isin {isin}", validate_isin(isin))
ok("validate_isin rejects bad check", not validate_isin("US0378331004"))

# ── SPID:格式/校验/幂等/冲突探测 ────────────────────────────────────────────
s1 = make_spid("pair", "mtfs|L:US1|S:US2")
ok("spid len 11 + prefix", len(s1) == 11 and s1.startswith("SPPR"))
ok("spid validates", validate_spid(s1))
ok("spid deterministic", make_spid("pair", "mtfs|L:US1|S:US2") == s1)
ok("spid probe differs", make_spid("pair", "mtfs|L:US1|S:US2", probe=1) != s1)
corrupt = s1[:-1] + ("0" if s1[-1] != "0" else "1")
ok("spid rejects bad check digit", not validate_spid(corrupt))
ok("kind in TT", make_spid("subsector", "x")[2:4] == "SS")

# ── pair 方向真值表(§2.5.2a,含 F/PG 实证符号)──────────────────────────────
A, B = "US_AAA", "US_BBB"
kAB_long  = pair_canonical_key("mtfs", A, B, "long",  +100, -50)
kBA_short = pair_canonical_key("mtfs", B, A, "short", -50, +100)
kAB_short = pair_canonical_key("mtfs", A, B, "short", -100, +50)
kBA_long  = pair_canonical_key("mtfs", B, A, "long",  +50, -100)
ok("truth table X: (A/B,long) == (B/A,short)", kAB_long == kBA_short)
ok("truth table Y: (A/B,short) == (B/A,long)", kAB_short == kBA_long)
ok("X != Y (反向交易绝不折叠)", kAB_long != kAB_short)
ok("F/PG 实证符号: short s1<0 s2>0", normalize_pair_direction("short", -6426, 500) == "long_is_s2")
ok("long 符号", normalize_pair_direction("long", 640, -3574) == "long_is_s1")

# 符号矛盾必须 ABORT
for args in [("long", -1, -1), ("long", -5, +5), ("short", +5, -5), ("flat", 1, -1)]:
    try:
        normalize_pair_direction(*args)
        ok(f"ABORT on {args}", False)
    except RegistryError:
        ok(f"ABORT on {args}", True)

# ── SPID 与真值表结合:同 key 同 ID ─────────────────────────────────────────
ok("pair spid X shared", make_spid("pair", kAB_long) == make_spid("pair", kBA_short))
ok("pair spid X!=Y", make_spid("pair", kAB_long) != make_spid("pair", kAB_short))

# ── base36 luhn 自洽 ────────────────────────────────────────────────────────
ok("b36 luhn stable", _b36_luhn("SPPFABC123") == _b36_luhn("SPPFABC123"))

# ── 公司行为登记处接线(2026-09-21,BK→BNY 实证;全部 I/O 进 /tmp)──────────────
import json as _json
import tempfile as _tf
from contextlib import contextmanager as _contextmanager

import ticker_aliases as _ta
import controller.registry as _R


def _registry_state():
    return (_R.REG_DIR, _R.MASTER_PATH, _R.NODES_PATH, _R.CHANGELOG,
            _ta.ALIAS_PATH, dict(_ta._CACHE))


@_contextmanager
def _isolated_registry():
    # 可嵌套:恢复调用者原本的路径和已预热缓存,不假定调用者使用生产目录。
    # REG_DIR 也隔离,因为 _log_change 会 mkdir(REG_DIR)。缓存字典保留身份,
    # 同时恢复原嵌套值的引用;别名加载器只替换 data/delist,不修改旧映射。
    saved = _registry_state()
    with _tf.TemporaryDirectory(prefix="registry_alias_test_", dir="/tmp") as directory:
        try:
            _R.REG_DIR = directory
            _R.MASTER_PATH = os.path.join(directory, "security_master.json")
            _R.NODES_PATH = os.path.join(directory, "node_registry.json")
            _R.CHANGELOG = os.path.join(directory, "changelog.jsonl")
            _ta.ALIAS_PATH = _ta.Path(directory) / "ticker_aliases.json"
            _ta._CACHE.clear()
            _ta._CACHE.update(mtime=None, data={}, delist={})
            yield directory
        finally:
            (_R.REG_DIR, _R.MASTER_PATH, _R.NODES_PATH, _R.CHANGELOG,
             _ta.ALIAS_PATH, cache) = saved
            _ta._CACHE.clear()
            _ta._CACHE.update(cache)


_SAVED = _registry_state()
with _isolated_registry():
    _ta.ALIAS_PATH.write_text(_json.dumps({
        "schema": "v2",
        "aliases": {"OLDT": {"current": "NEWT", "changed": "2026-01-02",
                             "figi": "BBGTEST", "cik": "0000000001"}},
        "delistings": {"GONE": {"delisted": "2026-08-18", "name": "Gone Inc"}},
    }))
    _ta._CACHE.update(mtime=None, data={}, delist={})   # 强制重读

    reg = _R.Registry()
    _isin = reg.register_security("OLDT", "037833100", "BBGTEST", None,
                                  "Old Name", "equity")
    ok("register under old name", reg.isin_of("OLDT") == _isin)
    # 旧名→现名重注册:走 ticker 漂移分支
    reg.register_security("NEWT", "037833100", "BBGTEST", "0000000001",
                          "New Name", "equity")
    ok("drift: polygon_ticker flipped", reg.master[_isin]["polygon_ticker"] == "NEWT")
    _hist = reg.master[_isin]["ticker_history"]
    ok("drift: old window closed", _hist[0]["ticker"] == "OLDT" and _hist[0]["to"] is not None)
    ok("drift: new window open", _hist[-1]["ticker"] == "NEWT" and _hist[-1]["to"] is None)
    _events = [_json.loads(l)["event"] for l in open(_R.CHANGELOG)]
    ok("drift: changelog 留痕", "ticker_drift" in _events)
    reg.save()

    # 新实例(只认 NEWT):旧名经登记处解析,未登记名仍然 raise(绝不静默 fallback)
    reg2 = _R.Registry()
    ok("fresh load: NEWT direct", reg2.isin_of("NEWT") == _isin)
    ok("fresh load: OLDT via aliases(锚一致)", reg2.isin_of("OLDT") == _isin)
    try:
        reg2.isin_of("NOPE")
        ok("unknown ticker still raises", False)
    except RegistryError:
        ok("unknown ticker still raises", True)
    # 身份锚守卫:BADX 也映射到 NEWT,但 FIGI/CIK 与目标不符 → 必须 raise 不解析
    # 毒饵在合法解析验证后加入:同一目标的冲突应让整组拒绝,不混进正向场景。
    aliases = _json.loads(_ta.ALIAS_PATH.read_text())
    aliases["aliases"]["BADX"] = {"current": "NEWT", "changed": "2026-01-02",
                                  "figi": "BBGWRONG", "cik": "0000000999"}
    _ta.ALIAS_PATH.write_text(_json.dumps(aliases))
    _ta._CACHE.update(mtime=None, data={}, delist={})
    try:
        reg2.isin_of("BADX")
        ok("anchor mismatch raises", False)
    except RegistryError as e:
        ok("anchor mismatch raises", "身份锚不符" in str(e))
    ok("_anchor_mismatch cik 前导零归一",
       _R._anchor_mismatch({"cik": "0000000001"}, {"cik": 1}) is None)
    ok("_anchor_mismatch 缺共同身份锚拒绝",
       _R._anchor_mismatch({}, {"figi": "X"}) is not None)
    # 退市判定可用(build_master 的跳过分支依赖它)
    ok("delisting_of window", _ta.is_delisted("GONE") and not _ta.is_delisted("GONE", "2026-08-17"))
ok("调用者路径与缓存完整恢复", _registry_state() == _SAVED)

print(f"\nall {N} checks passed")


# 在同一个进程重复导入本测试模块。外层也使用 /tmp 正式形状的输入,验证内层
# 成功和异常退出都不会让后续 Registry/别名查询读到 OLDT/NEWT 假数据。
import runpy as _runpy
import pytest as _pytest


@_pytest.mark.parametrize("fail_during_import", [False, True])
def test_module_import_preserves_paths_and_warmed_cache(monkeypatch, fail_during_import):
    with _isolated_registry():
        _ta.ALIAS_PATH.write_text(_json.dumps({
            "schema": "v2",
            "aliases": {"OLD_BANK": {"current": "BNY", "changed": "2026-01-02",
                                       "figi": "BBGBASE", "cik": "0000000001"}},
            "delistings": {},
        }))
        registry = _R.Registry()
        isin = registry.register_security("BNY", "064058100", "BBGBASE",
                                          "0000000001", "Bank", "equity")
        registry.save()
        assert _ta.canonical("OLD_BANK") == "BNY"  # 预热调用者缓存
        before = _registry_state()
        cache_object, alias_map = _ta._CACHE, _ta._CACHE["data"]
        paths = [_R.MASTER_PATH, _R.NODES_PATH, _ta.ALIAS_PATH]
        before_bytes = {str(path): _ta.Path(path).read_bytes() for path in paths}

        with monkeypatch.context() as patch:
            if fail_during_import:
                def fail(*args, **kwargs):
                    raise RegistryError("injected import failure")
                patch.setattr(_R.Registry, "register_security", fail)
                with _pytest.raises(RegistryError, match="injected import failure"):
                    _runpy.run_path(__file__, run_name="registry_isolation_probe")
            else:
                _runpy.run_path(__file__, run_name="registry_isolation_probe")

        assert _registry_state() == before
        assert _ta._CACHE is cache_object and _ta._CACHE["data"] is alias_map
        assert {str(path): _ta.Path(path).read_bytes() for path in paths} == before_bytes
        assert _R.Registry().isin_of("BNY") == isin
        assert _R.Registry().isin_of("OLD_BANK") == isin
        assert _ta.canonical("OLD_BANK") == "BNY"
