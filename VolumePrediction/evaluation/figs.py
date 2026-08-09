"""
evaluation/figs — 图 3 / 图 4 生成(G4 训练曲线 + G8 迁移学习对照)
==================================================================
fig3: 深模型训练曲线(G4 训练协议诊断)——PaperNN/PaperRNN 的 train_history
      (MSE vs epoch)。quick 版: 1 seed + 面板抽样票 + 减 epochs(标注在图上);
      full 版: 论文协议 50 epochs 全面板。
fig4: 迁移学习前后 MEL 对照条形(G8)——读 outputs/replication/table3_transfer.csv
      (replication_econ.py 产物: mu / nn_stat / transfer_econ / d_mse_after_ft);
      文件缺失 → 出空图 + 显式警示文字(绝不静默)。

matplotlib Agg;产物落 outputs/replication/。
运行(仓库根): conda run -n someopark_run python -m VolumePrediction.evaluation.figs --quick
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# 中文字形回退(macOS 自带字体优先;缺失时退回 DejaVu 不报错)
matplotlib.rcParams["font.sans-serif"] = [
    "Arial Unicode MS", "Hiragino Sans GB", "PingFang HK", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

from VolumePrediction.common import OUT, get_logger  # noqa: E402

log = get_logger("figs")
REP_DIR = OUT / "replication"


# ═══════════════════════════════ fig3(G4)════════════════════════════════════

def fig3_training_curves(panel_tag: str = "latest", quick: bool = True,
                         group: str = "tech", n_tickers: int = 250,
                         seed: int = 0, out_dir: Optional[Path] = None) -> dict:
    """PaperNN/PaperRNN 训练曲线。quick: 抽 n_tickers 票 + nn 12ep / rnn 8ep。"""
    from VolumePrediction.replication_g import load_panel, paper_split, cols_upto
    from VolumePrediction.models.deep import PaperNN, PaperRNN

    od = Path(out_dir) if out_dir else REP_DIR
    od.mkdir(parents=True, exist_ok=True)

    panel = load_panel(panel_tag)
    tr, _ = paper_split(panel)
    tr = tr[tr["eta"].notna()]
    if quick:
        rng = np.random.default_rng(seed)
        all_t = tr.index.get_level_values("ticker").unique()
        pick = set(rng.choice(all_t, size=min(n_tickers, len(all_t)),
                              replace=False))
        tr = tr[tr.index.get_level_values("ticker").isin(pick)]
    cols = cols_upto(panel, group)
    if not cols:
        raise ValueError(f"panel has no columns for group {group!r}")
    Xtr, ytr = tr[cols], tr["eta"]
    log.info(f"fig3 train set: {len(tr):,} rows × {len(cols)} cols "
             f"(quick={quick}, group≤{group})")

    ep_nn, ep_rnn = (12, 8) if quick else (None, None)   # None → 论文 50ep
    histories = {}
    for name, cls, ep in [("paper_nn", PaperNN, ep_nn),
                          ("paper_rnn", PaperRNN, ep_rnn)]:
        m = cls(len(cols), seed=seed)
        m.fit(Xtr, ytr, **({"epochs": ep} if ep else {}))
        histories[name] = list(m.train_history)
        log.info(f"fig3 {name}: {len(m.train_history)} epochs, "
                 f"final MSE {m.train_history[-1]:.5f}")

    hist_df = pd.DataFrame({k: pd.Series(v, name=k,
                                         index=range(1, len(v) + 1))
                            for k, v in histories.items()})
    hist_df.index.name = "epoch"
    hist_df.to_csv(od / "fig3_training_history.csv")

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for name, h in histories.items():
        ax.plot(range(1, len(h) + 1), h, marker="o", ms=3, label=name)
    ax.set_xlabel("epoch")
    ax.set_ylabel("train MSE (eta target)")
    mode = (f"quick: 1 seed, {len(tr):,} rows, groups≤{group}"
            if quick else "paper protocol: 50 epochs, full panel")
    ax.set_title(f"Fig 3 — deep model training curves (G4)\n[{mode}]",
                 fontsize=10)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = od / "fig3_training_curves.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    log.info(f"fig3 → {p}")
    return {"path": str(p), "epochs": {k: len(v) for k, v in histories.items()},
            "final_mse": {k: round(v[-1], 6) for k, v in histories.items()},
            "n_rows": int(len(tr)), "quick": quick}


# ═══════════════════════════════ fig4(G8)════════════════════════════════════

def fig4_transfer_mel(out_dir: Optional[Path] = None,
                      table3_path: Optional[Path] = None) -> dict:
    """迁移微调前(nn_stat)后(transfer_econ)的归一化 MEL 对照条形,逐 μ。"""
    od = Path(out_dir) if out_dir else REP_DIR
    od.mkdir(parents=True, exist_ok=True)
    t3 = Path(table3_path) if table3_path else (REP_DIR / "table3_transfer.csv")
    p = od / "fig4_transfer_mel.png"

    if not t3.exists():
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.axis("off")
        ax.text(0.5, 0.5,
                "table3_transfer.csv 缺失\n"
                f"(期望路径: {t3})\n\n"
                "先运行 replication_econ.py 产出迁移学习表(G8),\n"
                "再重跑本脚本生成对照条形图。",
                ha="center", va="center", fontsize=11, wrap=True)
        ax.set_title("Fig 4 — transfer learning MEL (G8) [DATA MISSING]",
                     fontsize=10)
        fig.savefig(p, dpi=150)
        plt.close(fig)
        log.warning(f"fig4: table3 missing → placeholder written {p}")
        return {"path": str(p), "status": "missing_input", "expected": str(t3)}

    df = pd.read_csv(t3)
    need = {"mu", "nn_stat", "transfer_econ"}
    if not need.issubset(df.columns):
        raise ValueError(f"table3_transfer.csv 缺列: 需要 {sorted(need)}, "
                         f"实际 {list(df.columns)}")
    x = np.arange(len(df))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    ax.bar(x - w / 2, df["nn_stat"], w, label="nn_stat (微调前, 统计学习)")
    ax.bar(x + w / 2, df["transfer_econ"], w,
           label="transfer_econ (经济损失微调后)")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{m:g}" for m in df["mu"]], rotation=0)
    ax.set_xlabel(r"$\mu$")
    ax.set_ylabel("normalized MEL %  (oracle=100, ma5=0)")
    ax.set_title("Fig 4 — transfer learning: MEL before vs after "
                 "economic fine-tune (G8)", fontsize=10)
    if "d_mse_after_ft" in df.columns:
        for xi, (mel, dm) in enumerate(zip(df["transfer_econ"],
                                           df["d_mse_after_ft"])):
            ax.annotate(f"ΔMSE {dm:+.1e}", (xi + w / 2, mel),
                        textcoords="offset points", xytext=(0, 4),
                        ha="center", fontsize=6, rotation=90)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    log.info(f"fig4 → {p}")
    gain = (df["transfer_econ"] - df["nn_stat"]).mean()
    return {"path": str(p), "status": "ok", "n_mu": int(len(df)),
            "mean_mel_gain_pp": round(float(gain), 2)}


# ═══════════════════════════ fig1(G1)/ fig2(G6)═══════════════════════════════

def fig1_persist(panel_tag: str = "latest") -> dict:
    """图 1(v/η 分布)对真实生产面板落盘 —— eda.fig1_distributions 早已实现并有
    单测,此前只进 /tmp 测试轨;这里补 outputs/replication/ 持久化(G1 复刻产物)。"""
    from VolumePrediction.replication_g import load_panel
    from VolumePrediction.evaluation.eda import fig1_distributions
    panel = load_panel(panel_tag)
    p = fig1_distributions(panel, REP_DIR)
    log.info(f"fig1 → {p}")
    return {"path": str(p), "status": "ok", "n_rows": int(len(panel))}


def fig2_s_curve(out_dir: Optional[Path] = None) -> dict:
    """图 2 复刻(G6): 最优交易率 s(v̄;μ)=μ/(μ+λ(v̄)) 的 S 形曲线族。

    直接调 econ/policy.s_opt(G6 验收过的闭式解,非重新推导),μ 取论文网格
    (config econ.mu_grid)+ 生产 calibrated μ(mu_calibration.json,若在)叠加
    标注。x 轴 v̄=log 美元量,覆盖面板实际范围(1e5→1e12 美元)。"""
    from VolumePrediction.common import load_config
    from VolumePrediction.econ.policy import s_opt

    od = Path(out_dir) if out_dir else REP_DIR
    od.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    mu_grid = list(cfg.get("econ", {}).get("mu_grid", [1e-8, 1e-7, 1e-6, 1e-5, 1e-4]))
    vbar = np.linspace(np.log(1e5), np.log(1e12), 400)

    fig, ax = plt.subplots(figsize=(7.5, 4.6))
    for mu in mu_grid:
        ax.plot(vbar, [s_opt(v, float(mu)) for v in vbar], lw=1.4,
                label=f"μ={mu:.0e}")
    # 生产 calibrated μ(有则叠加虚线,直观看生产工作点)
    cal_f = OUT / "registry" / "mu_calibration.json"
    if cal_f.exists():
        try:
            cal = json.loads(cal_f.read_text())
            for key, entry in cal.items():
                mu = entry.get("mu")
                if mu and np.isfinite(mu) and entry.get("calibration_source") != "paper_prior":
                    ax.plot(vbar, [s_opt(v, float(mu)) for v in vbar], "--", lw=1.8,
                            label=f"{key} (calibrated {mu:.2e})")
        except Exception:  # noqa: BLE001 — 破损工件只影响叠加线,主图照出
            log.warning("mu_calibration.json 读取失败,仅画论文网格")
    ax.set_xlabel("v̄ = log dollar volume")
    ax.set_ylabel("s(v̄; μ)  optimal trade rate")
    ax.set_title("Fig.2 replication — s(v̄;μ)=μ/(μ+λ(v̄)),  λ=0.2e^{-v}")
    ax.legend(fontsize=7, loc="center right")
    ax.grid(alpha=0.3)
    p = od / "fig2_s_curve.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    log.info(f"fig2 → {p}")
    return {"path": str(p), "status": "ok", "n_mu": len(mu_grid)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="fig1(G1)/fig2(G6)/fig3(G4)/fig4(G8) 生成")
    ap.add_argument("--panel", default="latest")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--group", default="tech",
                    help="fig3 用的累进因子组上限(tech/fund1/fund2/cal/earn)")
    ap.add_argument("--n-tickers", type=int, default=250)
    ap.add_argument("--skip-fig3", action="store_true")
    ap.add_argument("--skip-fig4", action="store_true")
    ap.add_argument("--fig12", action="store_true",
                    help="只补 fig1(真面板分布)+ fig2(S 形曲线)持久化")
    a = ap.parse_args()
    out = {}
    if a.fig12:
        out["fig1"] = fig1_persist(a.panel)
        out["fig2"] = fig2_s_curve()
        print(json.dumps(out, indent=2, ensure_ascii=False))
        raise SystemExit(0)
    if not a.skip_fig3:
        out["fig3"] = fig3_training_curves(a.panel, quick=a.quick,
                                           group=a.group,
                                           n_tickers=a.n_tickers)
    if not a.skip_fig4:
        out["fig4"] = fig4_transfer_mel()
    print(json.dumps(out, indent=2, ensure_ascii=False))
