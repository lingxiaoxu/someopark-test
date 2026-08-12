"""registry 单测(plan §六 ID 体系条目)。输出只进 /tmp。
运行: conda run -n someopark_run python -m pytest controller/tests/test_registry.py -q
  或: conda run -n someopark_run python controller/tests/test_registry.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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

print(f"\nall {N} checks passed")
