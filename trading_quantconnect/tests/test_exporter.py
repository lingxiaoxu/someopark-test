"""exporter 状态机(tmp 全沙箱: SOURCE_FILES/state 全部重定向,零推送)。"""
import json
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DIR))

import exporter  # noqa: E402
import inventory_source as isrc  # noqa: E402
import qc_api  # noqa: E402


class _NetBan:
    """测试网络禁令: 任何触碰 QcClient 即 fail(2026-08-16 事故: golive 测试
    曾把沙箱合成 target 真推上 QC ObjectStore —— 外部副作用也是'生产')。"""
    def __init__(self, *a, **k):
        raise AssertionError("TEST NETWORK BAN: QcClient touched in unit test")


@pytest.fixture(autouse=True)
def _ban_network(monkeypatch):
    monkeypatch.setattr(qc_api, "QcClient", _NetBan)
    yield


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    files = {}
    base = {
        "mrpt": {"pairs": {"AAA/BBB": {"direction": "long", "s1_shares": 10,
                                       "s2_shares": -20,
                                       "open_date": "2026-08-01"}}},
        "mtfs": {"pairs": {}},
        "aiss": {"positions": {"NVDA": {"shares": 3}}},
        "ssrs": {"holdings": {"XLK": {"shares": 2}}},
        "bdc": {"holdings": {"GBDC": {"shares": 5.5}},
                "cash": {"ticker": "BIL", "shares": 1.2}},
    }
    for st, doc in base.items():
        p = src / f"{st}.json"
        p.write_text(json.dumps(doc))
        files[st] = p
    monkeypatch.setattr(isrc, "SOURCE_FILES", files)
    state = tmp_path / "state"
    for name in ("STATE_DIR", "LEGACY_PATH", "SCALED_PATH", "EXPORTER_STATE",
                 "TARGET_COPY", "RESIDUAL_PATH"):
        default = getattr(exporter, name)
        monkeypatch.setattr(exporter, name, state / Path(default).name
                            if name != "STATE_DIR" else state)
    # 账本 equity(scalar 分母)
    for n, eq in (("account_mrpt.json", 100.0), ("account_mtfs.json", 200.0)):
        (src / n).write_text(json.dumps({"equity": eq}))
    (src / "qlib-main/semiconductor_strategy").mkdir(parents=True)
    (src / "qlib-main/sector_rotation").mkdir(parents=True)
    (src / "qlib-main/semiconductor_strategy/account_aiss.json").write_text(
        json.dumps({"equity": 300.0}))
    (src / "qlib-main/sector_rotation/account_ssrs.json").write_text(
        json.dumps({"equity": 400.0}))
    (src / "account_bdc.json").write_text(json.dumps({"equity": 500.0}))
    # 官方 perf json(scalar 分子): aiss=2x 账本,其余 1x → 缩放语义可断言
    data = src / "someo-park-investment-management/public/data"
    data.mkdir(parents=True)
    (data / "strategy_performance.json").write_text(json.dumps(
        [{"date": "2026-08-14", "mrpt_equity": 100.0, "mtfs_equity": 200.0}]))
    (data / "master_portfolio_performance.json").write_text(json.dumps(
        [{"date": "2026-08-14", "sr_equity": 400.0, "aiss_equity": 600.0}]))
    (data / "private_credit_bdc_performance.json").write_text(json.dumps(
        [{"date": "2026-08-14", "bdc_equity": 500.0}]))
    return files, base


def test_golive_freezes_and_exports(sandbox):
    files, base = sandbox
    r = exporter.golive(push=False)
    assert r["version"] == 1 and r["changed"]
    leg = json.loads(exporter.LEGACY_PATH.read_text())
    assert leg["frozen"]["mrpt"][0]["pair"] == "AAA/BBB"
    st = json.loads(exporter.EXPORTER_STATE.read_text())
    assert st["initial_cash"] == 1800.0                 # C0 = Σ 官方(aiss 2x)
    assert st["scalars"] == {"mrpt": 1.0, "mtfs": 1.0, "aiss": 2.0,
                             "ssrs": 1.0, "bdc": 1.0}
    doc = json.loads(exporter.TARGET_COPY.read_text())
    # legacy 剔除 → 只有 aiss/ssrs/bdc;aiss 股数按 2x 缩放(缩放镜像)
    assert set(doc["targets"]) == {"NVDA", "XLK", "GBDC", "BIL"}
    assert doc["targets"]["NVDA"] == 6                  # 3 × scalar 2.0
    assert doc["initial_cash"] == 1800.0


def test_golive_is_once_only(sandbox):
    exporter.golive(push=False)
    with pytest.raises(isrc.SourceError, match="only|已|once|exists"):
        exporter.golive(push=False)


def test_versioning_and_idempotence(sandbox):
    files, base = sandbox
    exporter.golive(push=False)
    r2 = exporter.export_once(push=False)
    assert not r2["changed"]                            # 无变化不升版
    # 新对开仓 → 版本 +1,target 出现新腿
    base["mrpt"]["pairs"]["CCC/DDD"] = {"direction": "short",
                                        "s1_shares": -7, "s2_shares": 8,
                                        "open_date": "2026-08-18"}
    files["mrpt"].write_text(json.dumps(base["mrpt"]))
    r3 = exporter.export_once(push=False)
    assert r3["changed"] and r3["version"] == 2
    doc = json.loads(exporter.TARGET_COPY.read_text())
    assert doc["targets"]["CCC"] == -7 and doc["targets"]["DDD"] == 8
    # legacy 对平仓 → target 哈希不变 → 不升版(QC 零动作)
    del base["mrpt"]["pairs"]["AAA/BBB"]
    files["mrpt"].write_text(json.dumps(base["mrpt"]))
    r4 = exporter.export_once(push=False)
    assert not r4["changed"] and r4["version"] == 2
    assert r4["legacy_alive"] == {"mrpt": 0, "mtfs": 0}


def test_no_export_without_golive(sandbox):
    with pytest.raises(isrc.SourceError, match="golive"):
        exporter.export_once(push=False)


def test_residual_written(sandbox):
    exporter.golive(push=False)
    res = json.loads(exporter.RESIDUAL_PATH.read_text())["residual"]
    assert abs(res["GBDC"] - 0.5) < 1e-9 or abs(res["GBDC"] + 0.5) < 1e-9
    assert abs(abs(res["BIL"]) - 0.2) < 1e-9
