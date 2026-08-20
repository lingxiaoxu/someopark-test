"""inventory_source 纯函数矩阵(零 IO;真数据用例只读生产文件)。"""
import copy
import json
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DIR))

from inventory_source import (SOURCE_FILES, SourceError, build_target,  # noqa: E402
                              content_hash, freeze_legacy, open_pairs,
                              read_snapshot, stable_read)


def _snap():
    """合成五策略快照(覆盖: 空头/跨策略同票/化石槽/小数股)。"""
    return {
        "mrpt": {"pairs": {
            "AAA/BBB": {"direction": "long", "s1_shares": 100,
                        "s2_shares": -200, "open_date": "2026-08-01"},
            "OLD/DEAD": {"direction": None, "s1_shares": None,
                         "s2_shares": None, "open_date": None},   # 化石槽
        }},
        "mtfs": {"pairs": {
            "CCC/AAA": {"direction": "short", "s1_shares": -50,
                        "s2_shares": 30, "open_date": "2026-08-05"},
        }},
        "aiss": {"positions": {"NVDA": {"shares": 10, "avg_cost": 1.0},
                               "AAA": {"shares": 5, "avg_cost": 1.0}}},
        "ssrs": {"holdings": {"XLK": {"shares": 7}}},
        "bdc": {"holdings": {"GBDC": {"shares": 28686.3968}},
                "cash": {"ticker": "BIL", "shares": 5173.0829}},
    }


def test_flatten_netting_and_attribution():
    b = build_target(_snap())
    t = b["targets"]
    # AAA: mrpt +100, mtfs +30, aiss +5 = 135(跨策略同票净额)
    assert t["AAA"] == 135
    assert t["BBB"] == -200 and t["CCC"] == -50          # 空头负股数
    assert b["attribution"]["AAA"] == {"mrpt": 100.0, "mtfs": 30.0,
                                       "aiss": 5.0}
    assert "OLD" not in t and "DEAD" not in t            # 化石槽过滤


def test_bdc_fractional_residual():
    b = build_target(_snap())
    assert b["targets"]["GBDC"] == 28686                 # 整数化
    assert abs(b["residual"]["GBDC"] - 0.3968) < 1e-6    # 残差账
    assert b["targets"]["BIL"] == 5173
    assert abs(b["residual"]["BIL"] - 0.0829) < 1e-6


def test_non_integer_non_bdc_raises():
    s = _snap()
    s["aiss"]["positions"]["NVDA"]["shares"] = 10.5
    with pytest.raises(SourceError, match="non-integer"):
        build_target(s)


def test_legacy_subtraction_full_matrix():
    """用户冷启动规格四情形逐一断言。"""
    s = _snap()
    legacy = freeze_legacy(s)                            # 冻结当前两对
    b0 = build_target(s, legacy=legacy)
    # ① 既有仓全部剔除: pairs 腿不在 target(AAA 只剩 aiss 5 股)
    assert b0["targets"]["AAA"] == 5
    assert "BBB" not in b0["targets"] and "CCC" not in b0["targets"]
    assert {k: len(v) for k, v in b0["legacy_alive"].items()} == \
        {"mrpt": 1, "mtfs": 1}
    h0 = content_hash(b0["targets"])

    # ② legacy 平仓 → target 哈希不变(QC 无操作 = "无仓可平不交易")
    s2 = copy.deepcopy(s)
    s2["mrpt"]["pairs"]["AAA/BBB"]["direction"] = None
    b2 = build_target(s2, legacy=legacy)
    assert content_hash(b2["targets"]) == h0
    assert b2["legacy_alive"].get("mrpt", []) == []      # 存活数下降可观测

    # ③ 新对开仓(与 legacy 同票 AAA)→ 只镜像新对贡献
    s3 = copy.deepcopy(s)
    s3["mrpt"]["pairs"]["AAA/ZZZ"] = {"direction": "long", "s1_shares": 40,
                                      "s2_shares": -60,
                                      "open_date": "2026-08-18"}
    b3 = build_target(s3, legacy=legacy)
    assert b3["targets"]["AAA"] == 45                    # 5(aiss)+40(新对)
    assert b3["targets"]["ZZZ"] == -60
    assert "BBB" not in b3["targets"]                    # legacy 腿仍被剔

    # ④ 同名重开(open_date 变)= 新实例 → 按新仓镜像
    s4 = copy.deepcopy(s)
    s4["mrpt"]["pairs"]["AAA/BBB"]["open_date"] = "2026-08-20"
    b4 = build_target(s4, legacy=legacy)
    assert b4["targets"]["AAA"] == 105 and b4["targets"]["BBB"] == -200
    assert b4["legacy_alive"].get("mrpt", []) == []


def test_stable_read_rejects_torn_write(tmp_path):
    p = tmp_path / "inv.json"
    p.write_text('{"pairs": {')                          # 撕裂 JSON
    with pytest.raises(SourceError):
        stable_read(p, settle_ms=10, attempts=2)
    p.write_text(json.dumps({"pairs": {}}))
    assert stable_read(p, settle_ms=10) == {"pairs": {}}


def test_real_snapshot_readonly_prego_live():
    """真数据(只读): 冻结当前全部 pairs → target 不含任何 pairs 腿,
    且 AISS/SSRS/BDC 全量在列(用户规格: 三者立即建仓)。"""
    for p in SOURCE_FILES.values():
        if not p.exists():
            pytest.skip(f"missing {p}")
    snap = read_snapshot()
    legacy = freeze_legacy(snap)
    b = build_target(snap, legacy=legacy)
    pair_legs = set()
    for st in ("mrpt", "mtfs"):
        for _, info in open_pairs(snap[st]).items():
            pair_legs |= set(info["legs"])
    aiss = set(snap["aiss"]["positions"])
    overlap_free_legs = pair_legs - aiss                 # 与 AISS 重叠票除外
    assert not (overlap_free_legs & set(b["targets"])), \
        f"legacy 腿泄漏进 target: {overlap_free_legs & set(b['targets'])}"
    assert aiss <= set(b["targets"])
    assert set(snap["ssrs"]["holdings"]) <= set(b["targets"])
    assert "BIL" in b["targets"] and b["targets"]["BIL"] > 0
    # 与 AISS 重叠的 legacy 腿: 净额应恰等于 AISS 股数
    for t in (pair_legs & aiss):
        assert b["targets"][t] == int(round(
            snap["aiss"]["positions"][t]["shares"]))


_SCALARS = {"mrpt": 0.6, "mtfs": 0.4, "aiss": 2.681, "ssrs": 1.0, "bdc": 1.0}


def test_scaled_mirror_semantics():
    """缩放镜像(2026-08-17 用户规格)+ 三队列(2026-08-19 修订)。

    非 pairs 策略仍按 scalar 缩放;pairs 的倍数改由队列决定,
    "旧口径 = 全部在手仓都在 S 队列"这一等价关系在此钉死。"""
    s = _snap()
    scalars = _SCALARS
    # ① 无冻结集: pairs 全落 F(m=1),只有非 pairs 被缩放
    b = build_target(_snap(), scalars=scalars)
    # AAA: mrpt 100×1 + mtfs 30×1 + aiss 5×2.681 = 100+30+13.405 = 143.405
    assert b["targets"]["AAA"] == 143
    assert abs(b["residual"]["AAA"] - 0.405) < 1e-6
    assert b["targets"]["BBB"] == -200                  # F 队列全额
    # ② 全部在手仓进 S 队列 == 2026-08-17 旧口径(策略层 ×k)
    allpairs = freeze_legacy(s)
    bS = build_target(_snap(), scalars=scalars, scaled=allpairs)
    assert bS["targets"]["AAA"] == 85                   # 60+12+13.405
    assert abs(bS["residual"]["AAA"] - 0.405) < 1e-6
    assert bS["targets"]["BBB"] == -120                 # -200×0.6
    # legacy 冻结 + 缩放: legacy 腿按 m=0 剔除,target 仍零 pairs 腿
    import copy
    legacy = freeze_legacy(s)
    b2 = build_target(s, legacy=legacy, scalars=scalars)
    assert "BBB" not in b2["targets"] and "CCC" not in b2["targets"]
    assert b2["targets"]["AAA"] == 13                   # 只剩 aiss 5×2.681
    # legacy 平仓在缩放下同样 target 不变
    s3 = copy.deepcopy(s)
    s3["mrpt"]["pairs"]["AAA/BBB"]["direction"] = None
    b3 = build_target(s3, legacy=legacy, scalars=scalars)
    assert content_hash(b3["targets"]) == content_hash(b2["targets"])


# ── 三队列(2026-08-19 用户规格: 不动 QC 既有仓,新仓全额,legacy 自然退场)──

def _snap3():
    """L=AAA/BBB(legacy) · S=CCC/AAA(已 ×k 镜像) · F 由用例追加。"""
    s = _snap()
    s["aiss"]["positions"] = {"NVDA": {"shares": 10, "avg_cost": 1.0}}
    s["ssrs"]["holdings"] = {}
    s["bdc"] = {"holdings": {}, "cash": {"ticker": "BIL", "shares": 1.0}}
    return s


_L3 = {"mrpt": [{"pair": "AAA/BBB", "open_date": "2026-08-01"}]}
_S3 = {"mtfs": [{"pair": "CCC/AAA", "open_date": "2026-08-05"}]}


def test_three_cohorts_multipliers():
    """L→0 股、S→×k、F→×1,三种倍数在同一次 build 里并存。"""
    s = _snap3()
    s["mtfs"]["pairs"]["DDD/EEE"] = {"direction": "long", "s1_shares": 11,
                                     "s2_shares": -22,
                                     "open_date": "2026-08-19"}   # F
    b = build_target(s, legacy=_L3, scalars=_SCALARS, scaled=_S3)
    assert "BBB" not in b["targets"]                     # L: m=0
    assert b["targets"]["CCC"] == -20                    # S: -50×0.4
    assert b["targets"]["AAA"] == 12                     # S: 30×0.4 = 12.0
    assert b["targets"]["DDD"] == 11 and b["targets"]["EEE"] == -22   # F: ×1
    assert [i["pair"] for i in b["legacy_alive"]["mrpt"]] == ["AAA/BBB"]
    assert [i["pair"] for i in b["scaled_alive"]["mtfs"]] == ["CCC/AAA"]


def test_new_pair_does_not_disturb_existing_legs():
    """核心承诺: 明早开新仓,只加新腿,S/L 队列既有腿一股不动。"""
    s = _snap3()
    before = build_target(s, legacy=_L3, scalars=_SCALARS, scaled=_S3)
    s2 = copy.deepcopy(s)
    s2["mtfs"]["pairs"]["DDD/EEE"] = {"direction": "long", "s1_shares": 11,
                                      "s2_shares": -22,
                                      "open_date": "2026-08-19"}
    after = build_target(s2, legacy=_L3, scalars=_SCALARS, scaled=_S3)
    assert set(after["targets"]) - set(before["targets"]) == {"DDD", "EEE"}
    for t, v in before["targets"].items():
        assert after["targets"][t] == v, f"{t} 被新仓扰动: {v} → {after['targets'][t]}"


def test_cohort_membership_survives_reopen_of_same_pair():
    """同名 pair 平掉再开(新 open_date)→ 掉出 S,按 F 全额镜像。"""
    s = copy.deepcopy(_snap3())
    s["mtfs"]["pairs"]["CCC/AAA"]["open_date"] = "2026-08-25"       # 重开
    b = build_target(s, legacy=_L3, scalars=_SCALARS, scaled=_S3)
    assert b["scaled_alive"]["mtfs"] == []
    assert b["targets"]["CCC"] == -50                    # 全额,不是 -20


def test_cohort_collision_rejected():
    """同一 pair 同时在 L 与 S → 倍数不唯一,必须大声拒绝。"""
    with pytest.raises(SourceError, match="倍数不唯一"):
        build_target(_snap3(), legacy=_L3, scalars=_SCALARS,
                     scaled={"mrpt": [{"pair": "AAA/BBB",
                                       "open_date": "2026-08-01"}]})


def test_scaled_alive_shrinks_as_s_queue_closes():
    """S 队列平仓 → scaled_alive 减少,其腿从 target 消失(K 就此定格)。"""
    s = copy.deepcopy(_snap3())
    s["mtfs"]["pairs"]["CCC/AAA"]["direction"] = None
    b = build_target(s, legacy=_L3, scalars=_SCALARS, scaled=_S3)
    assert b["scaled_alive"]["mtfs"] == []
    assert "CCC" not in b["targets"]


def test_insane_scalar_rejected():
    with pytest.raises(SourceError, match="scalar"):
        build_target(_snap(), scalars={"aiss": -1.0})
