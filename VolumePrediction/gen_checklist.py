"""
gen_checklist — 附录 A 58 步勾验文档生成器(Plan §7.5 回归清单)
================================================================
解析 VOLUME_PREDICTION_MODULE_PLAN.md 附录 A 的 58 步映射表(计划为唯一权威源),
逐步核验:
  1. 实现位置: 对每步的映射目标(模块.函数)在 VolumePrediction/**/*.py 的
     def/class 索引中实际查证(不存在即标注,绝不臆测);
  2. 测试覆盖: tests/*.py 中出现该步符号/模块名的测试文件清单;
  3. 状态: done(符号全部查证)/ partial(部分)/ missing(全无)/
     deferred(I58 文本线,用户令推迟——非缺失)。

产物: VolumePrediction/outputs/appendixA_checklist.md;运行后打印统计。
运行(仓库根): conda run -n someopark_run python -m VolumePrediction.gen_checklist
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from VolumePrediction.common import PKG, REPO, get_logger

log = get_logger("gen_checklist")

PLAN = (REPO / ".claude/plan/systemic-strategies-plan/"
               "VOLUME_PREDICTION_MODULE_PLAN.md")
OUT_MD = PKG / "outputs" / "appendixA_checklist.md"

# §1.4 类别边界(A16+B7+C6+D4+E8+F2+G8+H5+I2 = 58)
CATEGORIES = [("A", "数据管道", 1, 16), ("B", "EDA/基线", 17, 23),
              ("C", "时序", 24, 29), ("D", "无监督", 30, 33),
              ("E", "回归/ML", 34, 41), ("F", "深度", 42, 43),
              ("G", "经济学习", 44, 51), ("H", "记录输出", 52, 56),
              ("I", "未完成", 57, 58)]

# 逐 range 的核验符号表(映射文本→具体 def/class 名;偏差在 note 留档)
VERIFY: dict[str, dict] = {
    "A1-A5": {"symbols": ["ensure_day", "backfill", "load_range",
                          "earnings_surprises", "market_cap",
                          "stock_data_reference"],
              "note": "源替换: 旧四 parquet → Polygon grouped(§二定案)+Mongo 对拍"},
    "A6": {"symbols": ["create_earnings_dummies"]},
    "A7-A9": {"symbols": ["add_volume_features", "add_return_rollups"]},
    "A10": {"symbols": ["add_calendar_flags"]},
    "A11-A12": {"symbols": ["fill_with_stock_past_median",
                            "fill_with_stock_global_median"],
                "note": "legacy 轨;paper 轨零填充经 pipeline.fill(policy) 分流(§7.2)"},
    "A13": {"symbols": ["zscore_normalize"]},
    "A14": {"symbols": ["shift_volume_columns_and_drop_last"]},
    "A15": {"symbols": ["save_panel", "load_panel"]},
    "A16": {"symbols": ["fred_macro"],
            "note": "计划名 fred_merge → 实现名 inhouse_loader.fred_macro"
                    "(系列清单在 config.fred.series)"},
    "B17-B23": {"symbols": ["baseline_predictability_table",
                            "fig1_distributions", "correlation_matrix",
                            "MA5Model", "PrevDayModel"]},
    "C24-C29": {"symbols": ["ARIMAPerTicker", "SARIMAPerTicker", "acf_pacf",
                            "significant_lags", "panel_significant_lags"]},
    "D30-D33": {"symbols": ["LSTMAutoencoder", "cluster_latent"]},
    "E34-E41": {"symbols": ["LassoCVModel", "PCRModel", "PLSModel",
                            "ForwardStepwiseModel", "AdaBoostModel",
                            "NN2Model", "oos_r2_eta",
                            "feature_importance_knockout"]},
    "F42-F43": {"symbols": ["SingleLSTM", "ClusteredLSTM"]},
    "G44-G51": {"symbols": ["losscon", "s_opt", "mel", "mel_normalized",
                            "EconNN", "EconAda", "TransferEconNN",
                            "simulate", "run_experiment1"],
                "note": "计划文件名 econ/trading_sim.py → 交易模拟实现于"
                        " replication_trading.py(simulate/run_experiment1)"},
    "H52-H56": {"symbols": ["Registry", "record_model", "promote",
                            "production", "resid_std"]},
    "I57": {"symbols": ["TFT"]},
    "I58": {"symbols": ["daily_sentiment", "lda_topics"],
            "deferred": True,
            "note": "文本线用户令推迟(§1.1);text_features.py 为显式占位 stub"},
}


def parse_appendix_a(text: str) -> list[tuple[str, str]]:
    """附录 A 块 → [(range_str, mapping_text)];以计划文件为唯一权威源。"""
    m = re.search(r"### 附录 A.*?\n(.*?)\n### 附录 B", text, re.S)
    if not m:
        raise RuntimeError(f"附录 A 块未找到: {PLAN}")
    body = " ".join(line.strip() for line in m.group(1).splitlines()
                    if line.strip())
    entries = []
    for chunk in body.split(";"):
        chunk = chunk.strip().rstrip(";,")
        mm = re.match(r"([A-I]\d+(?:-[A-I]?\d+)?)→(.+)", chunk)
        if mm:
            entries.append((mm.group(1), mm.group(2).strip()))
    return entries


def expand_range(rng: str) -> list[str]:
    mm = re.match(r"([A-I])(\d+)(?:-[A-I]?(\d+))?$", rng)
    if not mm:
        raise ValueError(f"bad range: {rng}")
    letter, lo, hi = mm.group(1), int(mm.group(2)), int(mm.group(3) or mm.group(2))
    return [f"{letter}{n}" for n in range(lo, hi + 1)]


def build_symbol_index() -> dict[str, list[str]]:
    """VolumePrediction/**/*.py(除 tests/__pycache__)的 def/class → 相对路径列表。"""
    idx: dict[str, list[str]] = {}
    pat = re.compile(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M)
    for p in sorted(PKG.rglob("*.py")):
        rp = p.relative_to(PKG)
        if "__pycache__" in rp.parts or rp.parts[0] == "tests":
            continue
        try:
            src = p.read_text()
        except Exception:  # noqa: BLE001
            continue
        for name in pat.findall(src):
            idx.setdefault(name, []).append(str(rp))
    return idx


def build_test_index() -> dict[str, str]:
    return {p.name: p.read_text()
            for p in sorted((PKG / "tests").glob("test_*.py"))}


def category_of(step: str) -> str:
    letter, num = step[0], int(step[1:])
    for lt, name, lo, hi in CATEGORIES:
        if lt == letter and lo <= num <= hi:
            return name
    return "?"


def run() -> dict:
    entries = parse_appendix_a(PLAN.read_text())
    sym_idx = build_symbol_index()
    test_idx = build_test_index()

    rows, stats = [], {"done": 0, "partial": 0, "missing": 0, "deferred": 0}
    seen: list[str] = []
    for rng, mapping in entries:
        spec = VERIFY.get(rng)
        if spec is None:
            log.warning(f"range {rng} 无核验表条目 — 标 missing 待补")
            spec = {"symbols": [], "note": "核验表未覆盖(需人工补)"}
        symbols = spec.get("symbols", [])
        found = {s: sorted(set(sym_idx.get(s, []))) for s in symbols}
        n_hit = sum(1 for v in found.values() if v)
        # 测试覆盖: 符号名或其实现模块名出现在测试文件中
        mods = {Path(f).stem for v in found.values() for f in v}
        terms = set(symbols) | {m for m in mods if m != "__init__"}
        tests = sorted({fn for fn, src in test_idx.items()
                        if any(t in src for t in terms)})
        if spec.get("deferred"):
            status = "deferred"
        elif symbols and n_hit == len(symbols):
            status = "done"
        elif n_hit > 0:
            status = "partial"
        else:
            status = "missing"
        impl = "; ".join(
            f"`{s}` → {', '.join(v) if v else '**NOT FOUND**'}"
            for s, v in found.items()) or "—"
        for step in expand_range(rng):
            seen.append(step)
            stats[status] += 1
            rows.append({"step": step, "category": category_of(step),
                         "range": rng, "mapping": mapping, "impl": impl,
                         "tests": tests, "status": status,
                         "note": spec.get("note", "")})

    if len(seen) != 58 or len(set(seen)) != 58:
        raise RuntimeError(f"步数核对失败: 展开 {len(seen)} 步(去重 "
                           f"{len(set(seen))}),应为 58 — 附录解析或计划文本变动")

    badge = {"done": "✅ done", "partial": "🟡 partial",
             "missing": "❌ MISSING", "deferred": "⏸ deferred(用户令)"}
    lines = [
        "# 附录 A — 58 步勾验清单(自动生成)",
        "",
        f"- 生成器: `VolumePrediction/gen_checklist.py`(权威源: 计划附录 A)",
        f"- 统计: **done {stats['done']} / partial {stats['partial']} / "
        f"deferred {stats['deferred']} / missing {stats['missing']}**(共 58 步)",
        "- 判据: done=映射符号全部在包内查证存在;partial=部分;"
        "missing=全无;deferred=I58 文本线(§1.1 用户令推迟,非缺失)",
        "",
        "| 步 | 类别 | 计划映射(附录 A 原文) | 实现位置(逐符号查证) | 测试覆盖 | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        note = f"<br>_{r['note']}_" if r["note"] else ""
        tests = ", ".join(f"`{t}`" for t in r["tests"]) or "—"
        lines.append(f"| {r['step']} | {r['category']} | {r['mapping']}{note} "
                     f"| {r['impl']} | {tests} | {badge[r['status']]} |")
    missing_rows = [r for r in rows if r["status"] == "missing"]
    lines += ["", "## 缺失项明细", ""]
    if missing_rows:
        lines += [f"- **{r['step']}**({r['range']}): {r['mapping']}"
                  for r in missing_rows]
    else:
        lines.append("(无 — 除 I58 用户令推迟外,58 步全部有已查证实现)")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_MD.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(OUT_MD)
    log.info(f"checklist → {OUT_MD} | {stats}")
    return {"artifact": str(OUT_MD), "stats": stats, "n_steps": len(seen),
            "missing": [r["step"] for r in missing_rows]}


if __name__ == "__main__":
    res = run()
    print(json.dumps(res, indent=2, ensure_ascii=False))
    sys.exit(0 if not res["missing"] else 1)
