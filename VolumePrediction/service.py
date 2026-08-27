"""
service — VolumeService 门面(Plan §五 全部 7 命名空间 ~28 方法)
================================================================
读取端零重依赖(§5.1): 顶层只 import stdlib + numpy/pandas/yaml;
torch/requests/pymongo 等一律延迟到具体重方法内 import。
qlib_run 与 someopark_run 两个 env 都能 `from VolumePrediction.service import VolumeService`。

服务保证(§5.3):
1. 优雅降级——工件缺失/过期不抛异常,回退 trailing/ma5 并标 source
2. 原子写(tmp+rename)
3. PIT 存档(outputs/history/)
4. 读取延迟低(本地 parquet 直读+进程内日缓存)
5. 工件三戳 model_version/trained_through/generated_at

horizon 语义(§7.11-4): v1 只支持 horizon=1;>1 显式 NotImplementedError。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from VolumePrediction.common import load_config, REPO, OUT, DATA_ROOT, get_logger
from VolumePrediction.outputs.recorder import Registry

log = get_logger("service")

_LATEST = "volume_forecast_latest.parquet"

# ADV 陈旧阈值(交易日;2026-08-26 AVB 退市实证)。一两天的空档是 grouped
# 日频常见的零星缺行,>3 个交易日不印价就不再是"数据噪声"而是停牌/退市/
# 断源 —— 那时候的 trailing 均值只是死前旧 bar 的回声,不能当 ADV 用。
STALE_TD = 3


def _as_list(symbols) -> list[str]:
    if isinstance(symbols, str):
        return [symbols]
    return list(symbols)


class VolumeService:
    """四策略统一入口。artifacts_dir/raw_dir 可覆盖(测试/沙盒)。"""

    def __init__(self,
                 artifacts_dir: Optional[Path] = None,
                 raw_dir: Optional[Path] = None,
                 config: Optional[dict] = None):
        self.cfg = config or load_config()
        self.art = Path(artifacts_dir) if artifacts_dir else OUT
        self.raw = Path(raw_dir) if raw_dir else (DATA_ROOT / "raw")
        self.registry = Registry(self.art)
        self._day_cache: dict[str, pd.DataFrame] = {}

        self.forecast = _Forecast(self)
        self.econ = _Econ(self)
        self.execute = _Execute(self)
        self.tca = _Tca(self)
        self.signals = _Signals(self)
        self.adv = _Adv(self)
        self.ops = _Ops(self)

    # ── 原始数据直读(零重依赖的独立小读者;与 polygon_loader 缓存格式对齐) ──
    def _raw_dates(self) -> list[str]:
        if not self.raw.exists():
            return []
        return sorted(p.stem.replace("grouped_", "")
                      for p in self.raw.glob("grouped_*.parquet"))

    def _load_day(self, date: str) -> pd.DataFrame:
        if date in self._day_cache:
            return self._day_cache[date]
        p = self.raw / f"grouped_{date}.parquet"
        if not p.exists():
            df = pd.DataFrame()
        else:
            df = pd.read_parquet(p)
            # 改名统一处理(2026-08-15): 改名日前的行 old→current,历史在
            # 现名下连续(BK→BNY 型;日期窗口化,回收名如今日 FB 不受影响)
            from ticker_aliases import normalize_day_frame
            df = normalize_day_frame(df, date)
            if not df.empty and "dollar_volume" not in df.columns \
                    and {"v", "vw"}.issubset(df.columns):
                df["dollar_volume"] = df["v"].astype(float) * df["vw"].astype(float)
        if len(self._day_cache) > 400:            # 简单上限,防内存膨胀
            self._day_cache.clear()
        self._day_cache[date] = df
        return df

    def _last_raw_date(self, date: Optional[str] = None) -> Optional[str]:
        ds = self._raw_dates()
        if not ds:
            return None
        if date is None:
            return ds[-1]
        ds = [d for d in ds if d <= date]
        return ds[-1] if ds else None

    def _ticker_frame(self, ticker: str, end_date: Optional[str],
                      lookback: int) -> pd.DataFrame:
        """单票近 lookback 个原始日的 [date, shares, close, V, v]。
        键用 canonical(归一数据语义): _load_day 已把历史行归一到现名下,
        改名前的 end_date 也必须查现名 —— resolve(市场名语义)在这里会给
        旧名 → 查空(2026-08-15 复审修正);回收名(FB 型)canonical 原样。"""
        from ticker_aliases import canonical
        ticker = canonical(ticker, end_date)
        ds = self._raw_dates()
        if end_date:
            ds = [d for d in ds if d <= end_date]
        rows = []
        for d in ds[-lookback:]:
            day = self._load_day(d)
            if day.empty:
                continue
            r = day[day["ticker"] == ticker]
            if r.empty:
                continue
            r0 = r.iloc[0]
            V = float(r0["dollar_volume"])
            rows.append({"date": d, "shares": float(r0["v"]),
                         "close": float(r0["c"]), "V": V,
                         "v": math.log(V) if V > 0 else float("nan")})
        return pd.DataFrame(rows)

    def _latest_artifact(self) -> Optional[pd.DataFrame]:
        p = self.art / _LATEST
        if not p.exists():
            return None
        try:
            return pd.read_parquet(p)
        except Exception:  # noqa: BLE001 — 破损工件按缺失处理(降级)
            return None

    def _history_file(self, date: str) -> Optional[pd.DataFrame]:
        p = self.art / "history" / f"volume_forecast_{date}.parquet"
        if p.exists():
            try:
                return pd.read_parquet(p)
            except Exception:  # noqa: BLE001
                return None
        return None


# ═══════════════════════════════════ forecast ═══════════════════════════════
class _Forecast:
    def __init__(self, s: VolumeService):
        self.s = s

    def get(self, symbols, date: Optional[str] = None, horizon: int = 1,
            model: str = "production") -> pd.DataFrame:
        """预测表[symbol, pred_v, pred_V, pred_eta, ci_low, ci_high,
        model_version, trained_through, source]。缺工件/缺票 → 逐票 ma5 回退。"""
        if horizon != 1:
            raise NotImplementedError(
                "v1 服务只支持 horizon=1(§7.11-4);多步预测属 P2 可选")
        from ticker_aliases import resolve
        # 旧名查询解析到现名(工件行一律现名);输出 symbol 保留调用方原名,
        # 免得消费端(按自己的 key 找行)拿不回结果
        syms = _as_list(symbols)
        _q = {s: resolve(s, date) for s in syms}
        art = (self.s._history_file(date) if date else None)
        if art is None:
            art = self.s._latest_artifact()
            if art is not None and date:
                # 明确日期但只有 latest → 仅当 latest 恰为该日才用(防前视)
                if str(art["date"].iloc[0])[:10] != date:
                    art = None
        rows = []
        std = self.s.registry.resid_std()
        z90 = 1.645
        got = set()
        if art is not None and not art.empty:
            _rev = {}                            # 现名 → 调用方原名(改名解析)
            for s_, c_ in _q.items():
                _rev.setdefault(c_, s_)
            sub = art[art["ticker"].isin(_rev.keys())]
            for _, r in sub.iterrows():
                orig = _rev[r["ticker"]]
                row = {
                    "symbol": orig, "pred_v": float(r["pred_v"]),
                    "pred_V": float(r["pred_V"]), "pred_eta": float(r["pred_eta"]),
                    "ci_low": float(r["pred_v"]) - z90 * std,
                    "ci_high": float(r["pred_v"]) + z90 * std,
                    "model_version": r.get("model_version", "?"),
                    "trained_through": r.get("trained_through"),
                    "source": "model"}
                if orig != r["ticker"]:
                    row["resolved_ticker"] = r["ticker"]   # 改名解析如实标注
                rows.append(row)
                got.add(orig)
        for sym in syms:
            if sym in got:
                continue
            fb = self._ma5_fallback(sym, date)
            if fb is not None:
                rows.append(fb)
        return pd.DataFrame(rows)

    def _ma5_fallback(self, symbol: str, date: Optional[str]) -> Optional[dict]:
        tf = self.s._ticker_frame(symbol, date, lookback=8)
        v = tf["v"].dropna() if not tf.empty else pd.Series(dtype=float)
        if len(v) < 2:
            return {"symbol": symbol, "pred_v": float("nan"), "pred_V": float("nan"),
                    "pred_eta": float("nan"), "ci_low": float("nan"),
                    "ci_high": float("nan"), "model_version": "none",
                    "trained_through": None, "source": "unavailable"}
        ma5 = float(v.iloc[-5:].mean())
        std = self.s.registry.resid_std()
        return {"symbol": symbol, "pred_v": ma5, "pred_V": float(math.exp(ma5)),
                "pred_eta": 0.0, "ci_low": ma5 - 1.645 * std,
                "ci_high": ma5 + 1.645 * std, "model_version": "baselines.ma5",
                "trained_through": tf["date"].iloc[-1], "source": "fallback_ma5"}

    def baselines(self, symbols, date: Optional[str] = None) -> pd.DataFrame:
        """论文四基准(G2): ma5/lag1/ma22/ma252 的 v 预测。"""
        out = []
        for sym in _as_list(symbols):
            tf = self.s._ticker_frame(sym, date, lookback=260)
            v = tf["v"].dropna() if not tf.empty else pd.Series(dtype=float)
            row = {"symbol": sym}
            row["lag1"] = float(v.iloc[-1]) if len(v) >= 1 else float("nan")
            for name, w in (("ma5", 5), ("ma22", 22), ("ma252", 252)):
                row[name] = float(v.iloc[-w:].mean()) if len(v) >= min(w, 2) else float("nan")
            out.append(row)
        return pd.DataFrame(out)

    def distribution(self, symbols, date: Optional[str] = None,
                     quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95)
                     ) -> pd.DataFrame:
        """v1 语义: 以 pred_v 为中心、registry 残差 std 的高斯近似分位数
        (v 与 V 双坐标)。P2 换经验残差分布。"""
        from statistics import NormalDist
        nd = NormalDist()
        base = self.get(symbols, date=date)
        std = self.s.registry.resid_std()
        rows = []
        for _, r in base.iterrows():
            row = {"symbol": r["symbol"], "source": r["source"]}
            for q in quantiles:
                vq = r["pred_v"] + nd.inv_cdf(q) * std
                row[f"v_q{int(q*100):02d}"] = vq
                row[f"V_q{int(q*100):02d}"] = math.exp(vq) if math.isfinite(vq) else float("nan")
            rows.append(row)
        return pd.DataFrame(rows)

    def history(self, start: str, end: str,
                model: str = "production") -> pd.DataFrame:
        """PIT 预测存档区间读取(策略回测接入,防前视)。"""
        hdir = self.s.art / "history"
        frames = []
        if hdir.exists():
            for p in sorted(hdir.glob("volume_forecast_*.parquet")):
                d = p.stem.replace("volume_forecast_", "")
                if start <= d <= end:
                    try:
                        frames.append(pd.read_parquet(p))
                    except Exception:  # noqa: BLE001
                        continue
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def feature_snapshot(self, symbol: str, date: str):
        """面板特征向量(审计/调试);面板工件缺失 → None+日志。"""
        p = self.s.art / "panel" / "panel_latest.parquet"
        if not p.exists():
            log.info("feature_snapshot: panel artifact missing")
            return None
        try:
            df = pd.read_parquet(
                p, filters=[("ticker", "=", symbol), ("date", "=", pd.Timestamp(date))])
            return df.iloc[0] if len(df) else None
        except Exception as e:  # noqa: BLE001
            log.info(f"feature_snapshot degraded: {e}")
            return None

    def list_models(self) -> pd.DataFrame:
        data = self.s.registry.load()
        rows = [{"version": k, **{kk: vv for kk, vv in v.items() if kk != "meta"}}
                for k, v in data.get("models", {}).items()]
        df = pd.DataFrame(rows)
        df.attrs["production"] = data.get("production")
        return df

    def model_info(self, version: str) -> Optional[dict]:
        return self.s.registry.model_info(version)


# ═════════════════════════════════════ econ ═════════════════════════════════
class _Econ:
    def __init__(self, s: VolumeService):
        self.s = s

    def _V_hat(self, symbol: str, date: Optional[str]) -> tuple[float, str]:
        f = self.s.forecast.get(symbol, date=date)
        if f.empty or not math.isfinite(float(f.iloc[0]["pred_V"])):
            return float("nan"), "unavailable"
        return float(f.iloc[0]["pred_V"]), str(f.iloc[0]["source"])

    def price_impact(self, symbol: str, dollar_amount: float,
                     date: Optional[str] = None) -> dict:
        from VolumePrediction.econ.policy import IMPACT_COEF, impact_cost_dollars
        V, src = self._V_hat(symbol, date)
        if not math.isfinite(V) or V <= 0:
            return {"symbol": symbol, "impact_frac": None, "cost_dollars": None,
                    "V_hat": None, "source": "unavailable"}
        part = dollar_amount / V
        return {"symbol": symbol, "participation": part,
                "impact_frac": IMPACT_COEF * part,
                "cost_dollars": impact_cost_dollars(dollar_amount, V),
                "V_hat": V, "source": src}

    def cost_curve(self, symbol: str, date: Optional[str] = None,
                   amounts: Optional[Sequence[float]] = None) -> pd.DataFrame:
        from VolumePrediction.econ.policy import impact_cost_dollars, IMPACT_COEF
        V, src = self._V_hat(symbol, date)
        amounts = list(amounts or [1e4, 1e5, 1e6, 1e7])
        rows = []
        for a in amounts:
            if not math.isfinite(V) or V <= 0:
                rows.append({"amount": a, "cost_main": None, "cost_sqrt": None,
                             "participation": None, "source": "unavailable"})
                continue
            part = a / V
            rows.append({
                "amount": a, "participation": part,
                "cost_main": impact_cost_dollars(a, V),               # 0.1·a²/V
                "cost_sqrt": IMPACT_COEF * math.sqrt(part) * a,       # 0.1·√part·a(注16)
                "source": src})
        return pd.DataFrame(rows)

    def optimal_trade_rate(self, symbol: str, x0: float, x_star: float,
                           mu: Optional[float] = None,
                           aum: Optional[float] = None,
                           date: Optional[str] = None,
                           objective: Optional[str] = None,
                           strategy: Optional[str] = None,
                           trade_type: Optional[str] = None) -> dict:
        from VolumePrediction.econ.policy import s_opt_constrained
        from VolumePrediction.econ import objective as obj
        V, src = self._V_hat(symbol, date)
        gap = abs(float(x_star) - float(x0))
        prof = None
        mu_src = "explicit"
        cap = 0.20
        if mu is None:
            prof = obj.resolve(strategy=strategy, trade_type=trade_type,
                               objective=objective or ("ssrs_rebalance" if not strategy
                                                       else None),
                               cfg=self.s.cfg) if (objective or strategy) else None
            if prof is not None:
                mu, mu_src = obj.resolve_mu(prof, aum=aum,
                                            artifacts_dir=self.s.art, cfg=self.s.cfg)
                cap = prof.adv_cap
            else:
                from VolumePrediction.econ.policy import mu_from_aum
                econ = self.s.cfg["econ"]
                mu = (mu_from_aum(aum, econ["aum_scenarios"], econ["mu_grid"])
                      if aum else 1.0e-6)
                mu_src = "paper_prior_aum_map" if aum else "paper_prior"
        if not math.isfinite(V) or V <= 0 or gap <= 0:
            return {"symbol": symbol, "z": 0.0, "capped": False, "mu": mu,
                    "mu_source": mu_src, "source": "unavailable" if not gap else src}
        from VolumePrediction.econ.policy import lambda_params
        lam_form = (prof.lambda_form if prof
                    else self.s.cfg.get("econ", {}).get("lambda_form", "0.2/V"))
        cr = s_opt_constrained(math.log(V), mu, gap, V, cap, form=lam_form)
        lp = lambda_params(lam_form)
        return {"symbol": symbol, "z": cr.z, "z_unconstrained": cr.z_unconstrained,
                "capped": cr.capped, "cap": cr.cap, "rationing_reason": cr.reason,
                "mu": mu, "mu_source": mu_src, "V_hat": V, "gap_dollars": gap,
                "trade_dollars_today": cr.z * gap, "source": src,
                "lambda_form": lam_form,
                "lambda_C": lp["C"], "lambda_gamma": lp["gamma"],
                "lambda_source": lp["calibration_source"]}

    def economic_loss(self, v_real: float, z_taken: float, mu: float,
                      form: Optional[str] = None) -> float:
        from VolumePrediction.econ.policy import losscon
        if form is None:      # 与 optimal_trade_rate 同源: 默认吃 config 的 λ 形式
            form = self.s.cfg.get("econ", {}).get("lambda_form", "0.2/V")
        return losscon(v_real, z_taken, mu, form)

    def calibrate_mu(self, strategy: str,
                     signal_history_df: Optional[pd.DataFrame] = None,
                     alpha_decay_curve: Optional[pd.Series] = None) -> dict:
        from VolumePrediction.econ import calibration as cal
        from VolumePrediction.econ import objective as obj
        st = strategy.lower()
        if st in ("mrpt", "mtfs", "pairs"):
            res = cal.calibrate_mu_pairs(signal_history_df, strategy=st)
            key = "pairs_decay"
        else:
            res = cal.calibrate_mu_momentum(alpha_decay_curve, strategy=st)
            key = f"{st}_mom_decay"
        self._write_mu(key, res)
        return {**res, "mu_key": key}

    def calibrate_lambda_from_fills(self, strategy: str = "all",
                                    fills_df: Optional[pd.DataFrame] = None) -> dict:
        from VolumePrediction.econ import calibration as cal
        if fills_df is None:
            fills_df = self.s.tca._load_fills(strategy)
        res = cal.calibrate_lambda_from_fills(fills_df, strategy=strategy)
        self._write_mu(f"lambda_{strategy}", res)
        return res

    def _write_mu(self, key: str, res: dict) -> None:
        p = self.s.art / "registry" / "mu_calibration.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                data = {}
        data[key] = res
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        os.replace(tmp, p)


# ═══════════════════════════════════ execute ════════════════════════════════
class _Execute:
    def __init__(self, s: VolumeService):
        self.s = s

    def _profile_mu(self, strategy, trade_type, objective, aum):
        from VolumePrediction.econ import objective as obj
        prof = obj.resolve(strategy=strategy, trade_type=trade_type,
                           objective=objective, cfg=self.s.cfg)
        mu, mu_src = obj.resolve_mu(prof, aum=aum, artifacts_dir=self.s.art,
                                    cfg=self.s.cfg)
        return prof, mu, mu_src

    def _adv_forecast_series(self, ticker: str, date: Optional[str],
                             days: int) -> tuple[pd.Series, float, str]:
        """(未来 days 个工作日的预测股量序列(v1: 平推), 现价, source)。"""
        info = self.s.adv.info(ticker, date=date)
        shares = info.get("adv_shares")
        price = info.get("last_price")
        start = pd.Timestamp(date) if date else pd.Timestamp.now().normalize()
        idx = pd.bdate_range(start + pd.Timedelta(days=1), periods=days)
        if shares is None or price is None:
            return pd.Series(dtype=float), float("nan"), "unavailable"
        return (pd.Series(float(shares), index=idx), float(price),
                info.get("source", "?"))

    def advice(self, ticker: str, target_shares: float,
               date: Optional[str] = None,
               strategy: Optional[str] = None, trade_type: Optional[str] = None,
               objective: Optional[str] = None, aum: Optional[float] = None) -> dict:
        prof, mu, mu_src = self._profile_mu(strategy, trade_type, objective, aum)
        r = self.s.econ.optimal_trade_rate(
            ticker,
            x0=0.0, x_star=abs(target_shares) * (self._last_price(ticker, date) or 0),
            mu=mu, date=date)
        shares_today = (r.get("z", 0.0) or 0.0) * abs(target_shares)
        return {**r, "profile": prof.name, "mu_source": mu_src,
                "target_shares": target_shares, "shares_today": shares_today,
                "urgent": prof.is_urgent}

    def schedule(self, ticker: str, target_shares: float,
                 start_date: Optional[str] = None, max_days: int = 10,
                 strategy: Optional[str] = None, trade_type: Optional[str] = None,
                 objective: Optional[str] = None,
                 aum: Optional[float] = None) -> pd.DataFrame:
        from VolumePrediction.execution.scheduler import schedule_single
        prof, mu, mu_src = self._profile_mu(strategy, trade_type, objective, aum)
        adv, price, src = self._adv_forecast_series(ticker, start_date, max_days)
        if adv.empty:
            df = pd.DataFrame()
            df.attrs["error"] = "no volume data"
            df.attrs["source"] = src
            return df
        df = schedule_single(ticker, abs(target_shares), price, adv, prof, mu,
                             max_days=max_days)
        df.attrs["profile"] = prof.name
        df.attrs["mu"] = mu
        df.attrs["mu_source"] = mu_src
        df.attrs["forecast_source"] = src
        return df

    def _last_price(self, ticker: str, date: Optional[str]) -> Optional[float]:
        tf = self.s._ticker_frame(ticker, date, lookback=3)
        return float(tf["close"].iloc[-1]) if len(tf) else None

    def participation_cap(self, ticker: str, date: Optional[str] = None,
                          cap: float = 0.20) -> dict:
        info = self.s.adv.info(ticker, date=date)
        shares = info.get("adv_shares")
        return {"ticker": ticker, "cap": cap,
                "max_shares_per_day": cap * shares if shares else None,
                "source": info.get("source")}

    def days_to_liquidate(self, ticker: str, shares: float,
                          date: Optional[str] = None, cap: float = 0.20) -> dict:
        info = self.s.adv.info(ticker, date=date)
        adv_sh = info.get("adv_shares")
        if not adv_sh:
            # source 透传(stale/unavailable 要能分辨:前者是票死了/停了,
            # 后者是这票本来就没数据 —— 排障时是两码事)
            return {"ticker": ticker, "days": None,
                    "source": info.get("source") or "unavailable"}
        return {"ticker": ticker,
                "days": abs(float(shares)) / (cap * adv_sh),
                "adv_shares": adv_sh, "cap": cap, "source": info.get("source")}


# ═════════════════════════════════════ tca ══════════════════════════════════
class _Tca:
    def __init__(self, s: VolumeService):
        self.s = s

    def _fills_path(self, strategy: str) -> Path:
        return self.s.art / "fills" / f"fills_{strategy}.jsonl"

    def record_fill(self, strategy: str, ticker: str, date: str,
                    shares: float, price: float, side: Optional[str] = None,
                    **extra) -> dict:
        rec = {"ts": pd.Timestamp.now().isoformat(timespec="seconds"),
               "strategy": strategy, "ticker": ticker, "date": date,
               "shares": float(shares), "price": float(price),
               "side": side or ("BUY" if shares > 0 else "SELL"), **extra}
        p = self._fills_path(strategy)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:                      # append-only
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def _load_fills(self, strategy: str) -> pd.DataFrame:
        p = self._fills_path(strategy)
        if not p.exists():
            return pd.DataFrame()
        rows = []
        for line in p.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return pd.DataFrame(rows)

    def realized_vs_predicted(self, strategy: str, window: int = 30) -> dict:
        fills = self._load_fills(strategy)
        if fills.empty:
            return {"strategy": strategy, "n": 0, "rows": pd.DataFrame(),
                    "mape_V": None, "note": "no fills recorded"}
        fills = fills.tail(500)
        rows = []
        for _, f in fills.iterrows():
            d = str(f["date"])[:10]
            hist = self.s._history_file(d)
            pred_V = None
            if hist is not None and not hist.empty:
                m = hist[hist["ticker"] == f["ticker"]]
                if len(m):
                    pred_V = float(m.iloc[0]["pred_V"])
            day = self.s._load_day(d)
            real_V = None
            if not day.empty:
                m = day[day["ticker"] == f["ticker"]]
                if len(m):
                    real_V = float(m.iloc[0]["dollar_volume"])
            ape = (abs(pred_V - real_V) / real_V
                   if pred_V and real_V and real_V > 0 else None)
            rows.append({"date": d, "ticker": f["ticker"], "pred_V": pred_V,
                         "real_V": real_V, "ape": ape})
        df = pd.DataFrame(rows)
        apes = df["ape"].dropna()
        return {"strategy": strategy, "n": len(df),
                "n_matched": int(apes.count()),
                "mape_V": float(apes.mean()) if len(apes) else None, "rows": df}

    def implementation_report(self, strategy: str, period: int = 30) -> dict:
        """式13/15 语义的事后汇总(v1: 成交额/估计冲击/换手代理)。"""
        from VolumePrediction.econ.policy import impact_cost_dollars
        fills = self._load_fills(strategy)
        if fills.empty:
            return {"strategy": strategy, "n": 0, "note": "no fills recorded"}
        fills["dollars"] = fills["shares"].abs() * fills["price"]
        est_costs = []
        for _, f in fills.iterrows():
            day = self.s._load_day(str(f["date"])[:10])
            V = None
            if not day.empty:
                m = day[day["ticker"] == f["ticker"]]
                if len(m):
                    V = float(m.iloc[0]["dollar_volume"])
            est_costs.append(impact_cost_dollars(float(f["dollars"]), V) if V else None)
        fills["est_impact_cost"] = est_costs
        return {"strategy": strategy, "n": int(len(fills)),
                "gross_traded_dollars": float(fills["dollars"].sum()),
                "est_impact_cost_dollars": float(pd.Series(est_costs).dropna().sum()),
                "date_range": [str(fills['date'].min()), str(fills['date'].max())],
                "by_ticker": fills.groupby("ticker")["dollars"].sum().to_dict()}


# ═══════════════════════════════════ signals ════════════════════════════════
class _Signals:
    def __init__(self, s: VolumeService):
        self.s = s

    def _eta_z_and_ret(self, ticker: str, date: Optional[str]
                       ) -> tuple[Optional[float], Optional[float]]:
        tf = self.s._ticker_frame(ticker, date, lookback=90)
        if len(tf) < 30:
            return None, None
        v = tf["v"]
        eta = v - v.rolling(5).mean()
        z = float((eta.iloc[-1]) / (eta.rolling(60, min_periods=20).std().iloc[-1]))
        ret5 = float(tf["close"].iloc[-1] / tf["close"].iloc[-6] - 1.0) \
            if len(tf) >= 6 else None
        return z, ret5

    def capitulation(self, symbols, date: Optional[str] = None) -> pd.DataFrame:
        """投降量(§5.2): η 的 60d 时序 z>2.5 且 5 日收益>−1%(价企稳)。"""
        rows = []
        for sym in _as_list(symbols):
            z, r5 = self._eta_z_and_ret(sym, date)
            flag = (z is not None and r5 is not None and z > 2.5 and r5 > -0.01)
            rows.append({"symbol": sym, "eta_z": z, "ret_5d": r5,
                         "capitulation": bool(flag),
                         "source": "raw" if z is not None else "unavailable"})
        return pd.DataFrame(rows)

    def abnormal_volume(self, symbols, date: Optional[str] = None) -> pd.DataFrame:
        rows = []
        for sym in _as_list(symbols):
            z, _ = self._eta_z_and_ret(sym, date)
            rows.append({"symbol": sym, "eta_z": z,
                         "abnormal": bool(z is not None and abs(z) > 2.0),
                         "source": "raw" if z is not None else "unavailable"})
        return pd.DataFrame(rows)

    def market_liquidity_outlook(self, date: Optional[str] = None,
                                 horizon: int = 5) -> dict:
        ds = self.s._raw_dates()
        if date:
            ds = [d for d in ds if d <= date]
        totals = []
        for d in ds[-10:]:
            day = self.s._load_day(d)
            if not day.empty:
                totals.append({"date": d,
                               "market_dollar_volume": float(day["dollar_volume"].sum())})
        tdf = pd.DataFrame(totals)
        events = self.upcoming_calendar(date=date, horizon=horizon)
        trend = None
        if len(tdf) >= 6:
            trend = float(tdf["market_dollar_volume"].iloc[-1]
                          / tdf["market_dollar_volume"].iloc[-6:-1].mean() - 1.0)
        return {"recent": tdf, "trend_vs_ma5": trend, "upcoming_events": events,
                "source": "raw" if len(tdf) else "unavailable"}

    def upcoming_calendar(self, date: Optional[str] = None,
                          horizon: int = 5) -> pd.DataFrame:
        """未来 N 个工作日的日历旗标(G3 calendar 组同规则)+ 财报票数(自有缓存)。"""
        start = (pd.Timestamp(date) if date else pd.Timestamp.now().normalize())
        days = pd.bdate_range(start + pd.Timedelta(days=1), periods=horizon)
        early = set()
        try:
            import pandas_market_calendars as mcal          # 轻延迟 import
            cal = mcal.get_calendar("NYSE")
            sched = cal.schedule(start_date=days[0].strftime("%Y-%m-%d"),
                                 end_date=days[-1].strftime("%Y-%m-%d"))
            if "market_close" in sched:
                closes = pd.to_datetime(sched["market_close"]).dt.tz_convert(
                    "America/New_York")
                early = {d.strftime("%Y-%m-%d")
                         for d, c in zip(sched.index, closes) if c.hour < 16}
        except Exception:  # noqa: BLE001 — 无日历库时 early_close 缺省 False
            pass
        earn_by_day: dict[str, int] = {}
        ec = DATA_ROOT / "earnings" / "earnings_cache_vp.json"
        if ec.exists():
            try:
                data = json.loads(ec.read_text())
                for _, quarters in (data.get("symbols") or {}).items():
                    for q in quarters:
                        d = q.get("earnings_date")
                        if d:
                            earn_by_day[d] = earn_by_day.get(d, 0) + 1
            except Exception:  # noqa: BLE001
                pass
        rows = []
        for d in days:
            third_friday = (d.weekday() == 4 and 15 <= d.day <= 21)
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "early_close": d.strftime("%Y-%m-%d") in early,
                "triple_witching": third_friday and d.month in (3, 6, 9, 12),
                "double_witching": third_friday and d.month not in (3, 6, 9, 12),
                "russell_rebalance": (d.month == 6 and d.weekday() == 4
                                      and 22 <= d.day <= 28),   # 6 月第 4 个周五(G3)
                "n_earnings": earn_by_day.get(d.strftime("%Y-%m-%d"), 0),
            })
        return pd.DataFrame(rows)


# ═════════════════════════════════════ adv ══════════════════════════════════
class _Adv:
    def __init__(self, s: VolumeService):
        self.s = s

    def info(self, ticker: str, window: int = 20, date: Optional[str] = None,
             blend: float = 0.5) -> dict:
        """trailing/前瞻混合 ADV 的完整信息(get_adv_forecast 的 dict 版)。"""
        tf = self.s._ticker_frame(ticker, date, lookback=window + 3)

        # ── 陈旧守卫(2026-08-26, AVB 并购退市实证)────────────────────────
        # _ticker_frame 取的是"最近 lookback 个交易日里**该票出现过**的行",
        # 不校验这些行离参照日多远。退市/长停牌票的窗口会一直命中死前的旧
        # bar,于是 trailing 交出一个外观完全健康的正数 ADV、last_price 永远
        # 冻结在最后一个交易日 —— 而下游三个消费点只过滤 None/nan/非正数
        # (RiskManager._vp_adv_forecast / days_to_liquidate / participation_cap),
        # 正数一律放行。AVB 退市 8 天仍报 997,536 股就是这么来的。
        #
        # 参照日必须取 **_last_raw_date(date)** —— 即"市场确实开过盘、raw 也
        # 确实落盘了的最后一天",而不是 date 或今天:周末/节假日/当日 raw 未
        # 到都会让全市场集体误判为陈旧。
        ref = self.s._last_raw_date(date)
        last_bar = str(tf["date"].iloc[-1])[:10] if len(tf) else None
        stale_td = (int(np.busday_count(last_bar, ref))
                    if (last_bar and ref and last_bar < ref) else 0)
        if stale_td > STALE_TD:
            # 公司行为登记处若已确证原因就一并标注(纯本地查表,零网络)。
            # 注意守卫本身**不依赖**登记处: 陈旧是本地可测的事实,停牌和尚未
            # 查证的退市都必须拦住,不能等到登记处有记录才生效。
            from ticker_aliases import delisting_of
            ca = delisting_of(ticker, ref)
            return {"ticker": ticker, "adv_shares": None, "source": "stale",
                    "last_bar": last_bar, "stale_td": stale_td,
                    "last_price": float(tf["close"].iloc[-1]),
                    "corporate_action": "delisted" if ca else None,
                    "delisted": (ca or {}).get("delisted")}

        trailing = float(tf["shares"].iloc[-window:].mean()) if len(tf) else None
        last_price = float(tf["close"].iloc[-1]) if len(tf) else None
        fsh, fsrc = None, None
        f = self.s.forecast.get(ticker, date=date)
        if len(f) and f.iloc[0]["source"] == "model" and last_price:
            fsh = float(f.iloc[0]["pred_V"]) / last_price
            fsrc = "model"
        if trailing is None and fsh is None:
            return {"ticker": ticker, "adv_shares": None, "source": "unavailable"}
        if fsh is None:
            return {"ticker": ticker, "adv_shares": trailing,
                    "trailing_shares": trailing, "forecast_shares": None,
                    "last_price": last_price, "source": "fallback_trailing"}
        mixed = blend * fsh + (1 - blend) * (trailing if trailing is not None else fsh)
        return {"ticker": ticker, "adv_shares": mixed, "trailing_shares": trailing,
                "forecast_shares": fsh, "last_price": last_price,
                "blend": blend, "source": "blend"}

    def get_adv_forecast(self, ticker: str, window: int = 20,
                         date: Optional[str] = None, blend: float = 0.5) -> float:
        """RiskManager.adv 同签名语义的前瞻版(§5.2)。→ float(股数)。"""
        info = self.info(ticker, window=window, date=date, blend=blend)
        v = info.get("adv_shares")
        return float(v) if v is not None else float("nan")

    def batch(self, tickers: Iterable[str], window: int = 20,
              date: Optional[str] = None, blend: float = 0.5) -> dict:
        return {t: self.get_adv_forecast(t, window, date, blend)
                for t in _as_list(tickers)}


# ═════════════════════════════════════ ops ══════════════════════════════════
class _Ops:
    def __init__(self, s: VolumeService):
        self.s = s

    def health(self) -> dict:
        art = self.s._latest_artifact()
        raw_last = self.s._last_raw_date()
        fresh = None
        cov = 0
        mv = None
        if art is not None and not art.empty:
            fresh = str(art["date"].iloc[0])[:10]
            cov = int(art["ticker"].nunique())
            mv = str(art["model_version"].iloc[0])
        stale = None
        if fresh:
            stale = int(np.busday_count(fresh,
                                        pd.Timestamp.now().strftime("%Y-%m-%d")))
        # 学习模型 drift(2026-08-01 ②晋升配套): 距冻结日的交易日数。
        # 通用serve路径的冻结统计漂移 O(1/n)/日 + fund末值随 filings 陈旧
        # → >25td 建议 refreeze(prod_model.freeze / refreeze.py)。
        #
        # 2026-08-26 修(C): 口径改为**按工件里实际服务的层**逐层算,不再读
        # registry.production 的 meta。原因: production 槽位(lgbm)与真正
        # 服务的层可以分家 —— blend.rnn_full_coverage 让 RNN 覆盖了 100% 的行,
        # 而 drift 却在数 lgbm 的冻结日。今天两者恰好都是 7/31,所以是"碰巧
        # 正确"的潜伏 bug。工件每行自带 trained_through,那才是事实源。
        prod_ver = None
        try:
            prod_ver = self.s.registry.load().get("production")
        except Exception:  # noqa: BLE001
            pass
        layers: dict = {}
        if art is not None and not art.empty and "trained_through" in art.columns:
            _today = pd.Timestamp.now().strftime("%Y-%m-%d")
            for _mv, _g in art.groupby(art["model_version"].astype(str)):
                _tt = str(_g["trained_through"].iloc[0])[:10]
                layers[_mv] = {
                    "rows": int(len(_g)), "trained_through": _tt,
                    "drift_tradedays": (None if _mv == "baselines.ma5"
                                        else int(np.busday_count(_tt, _today)))}
        _learned = {k: v for k, v in layers.items() if k != "baselines.ma5"}
        drift_td = max((v["drift_tradedays"] for v in _learned.values()
                        if v["drift_tradedays"] is not None), default=None)
        out = {"fresh_through": fresh, "raw_through": raw_last,
               "model_version": mv or self.s.registry.production(),
               "coverage_n": cov, "staleness_days": stale,
               "stale": bool(stale is None
                             or stale > int(self.s.cfg["service"]["staleness_max_days"]))}
        if layers:
            out["serving_layers"] = layers
        # 静止检测(2026-08-26 B'): 比 MAPE 阈值早得多的确定性信号
        static = self._static_check()
        if static:
            out["layer_identical_frac"] = {k: v["identical_frac"]
                                           for k, v in static.items()}
            _frozen = sorted(k for k, v in static.items()
                             if v["identical_frac"] > 0.99)
            out["forecast_static"] = bool(_frozen)
            if _frozen:
                out["forecast_static_layers"] = _frozen
        # 地板连败(2026-08-26 B 配套): AB 表里 RNN 在持仓票上连续输给朴素 ma5 的
        # 天数。事故期这个数悄悄走到 13/17 天 —— 静止检测抓"冻死",这条抓"还在动
        # 但已经没价值",两种失效模式不重叠。
        _fs = self._floor_streak()
        if _fs is not None:
            out["rnn_floor_loss_streak"] = _fs
            out["rnn_below_floor"] = bool(_fs >= 3)
        if _learned:
            out["production"] = prod_ver
            out["model_drift_tradedays"] = drift_td
            out["refreeze_due"] = bool(drift_td is not None and drift_td > 25)
        return out

    def _floor_streak(self) -> Optional[int]:
        """AB 追踪表末尾连续 rnn_beats_ma5_held == False 的行数(只读,失败返 None)。"""
        p = self.s.art / "shadow_rnn" / "rnn_ab_tracking.csv"
        if not p.exists():
            return None
        try:
            col = pd.read_csv(p, usecols=["rnn_beats_ma5_held"])["rnn_beats_ma5_held"]
        except Exception:  # noqa: BLE001 — 列不存在(旧表)/表破损都按"无信号"
            return None
        n = 0
        for v in col.iloc[::-1]:
            if pd.isna(v):
                break                      # 未评的行(旧口径)不算连败,也不跨过
            if bool(v):
                break
            n += 1
        return n

    def _static_check(self, min_n: int = 50) -> dict:
        """相邻两日预测工件逐位对拍 —— 某层几乎全票预测值不变 = 该层已冻死。

        为什么需要这条(2026-08-26 事故): RNN 层因 serve 漏写通用分支,自 8/14 起
        连续 9 个交易日 3,869 票 pred_V **逐位相同**,而唯二的探测器都瞎了 ——
        A/B 因候选臂与生产臂同源 rnn_wins 恒 False,refreeze_due 是纯日历计数。
        靠 MAPE 阈值要 6+ 天才反应,靠"输出没变"当天就能抓到,且几乎无假阳:
        ma5 层窗口天天前移,学习层特征天天更新,正常情况下不可能 >99% 逐位相同。

        覆盖两处工件:
          发布层 history/volume_forecast_* —— 当天真正服务下游的那份;
          休眠层 history/counterfactual_noblend_* —— RNN 吃满学习层后 lgbm 不再
            出行(8/18 起),发布工件里查不到它,可它仍是 set_blend(False) 的回退
            目标。回退到一个没人体检过的层 = 第二次事故。反事实存档正好每天存
            的就是被覆盖前的 lgbm 行,拿来当休眠层的体检样本零额外算力。
        """
        base = self.s.art / "history"
        res: dict = {}
        hist = sorted(base.glob("volume_forecast_*.parquet"))
        if len(hist) >= 2:
            res.update(self._pairwise_static(hist[-1], hist[-2], min_n))
        cf = sorted(base.glob("counterfactual_noblend_*.parquet"))
        if len(cf) >= 2:
            for _k, _v in self._pairwise_static(cf[-1], cf[-2], min_n).items():
                res[f"dormant:{_k}"] = _v
        return res

    @staticmethod
    def _pairwise_static(cur_p, prv_p, min_n: int = 50) -> dict:
        """两份工件按**层内**逐位对拍。

        2026-08-26 修: 初版把 prv 取成**整份工件**按 ticker 索引(所有层混在
        一起),于是"今天在 A 层的票 vs 昨天它在 B 层的值"—— 跨层比必然不同,
        把 identical_frac 稀释**下去**,漏报而不是误报。实测代价: 8/17 那天
        RNN 层真值 1.0(已冻死),混层算法给 0.975 < 0.99 阈值 → 不告警,
        要拖到 8/19 全层换完才炸,白丢两天。层路由每天都有票进出,这不是
        边缘情况。正解 = 两边都按 model_version 分组,只比同层同票。
        """
        try:
            cols = ["ticker", "pred_V", "model_version"]
            cur = pd.read_parquet(cur_p, columns=cols)
            prv = pd.read_parquet(prv_p, columns=cols)
        except Exception:  # noqa: BLE001 — 体检项绝不阻断调用方
            return {}
        pl = {str(mv): g.set_index("ticker")["pred_V"]
              for mv, g in prv.groupby(prv["model_version"].astype(str))}
        res: dict = {}
        for _mv, _g in cur.groupby(cur["model_version"].astype(str)):
            prev_s = pl.get(str(_mv))
            if prev_s is None:
                continue                      # 该层昨天不存在(首日上线),无从比
            s = _g.set_index("ticker")["pred_V"]
            s, prev_s = s[~s.index.duplicated()], prev_s[~prev_s.index.duplicated()]
            common = s.index.intersection(prev_s.index)
            if len(common) < min_n:
                continue
            same = float(np.isclose(s.loc[common].to_numpy(float),
                                    prev_s.loc[common].to_numpy(float),
                                    rtol=1e-9, atol=0.0).mean())
            res[str(_mv)] = {"n": int(len(common)),
                             "identical_frac": round(same, 6),
                             "vs": prv_p.stem.split("_")[-1]}
        return res

    def refresh(self, date: Optional[str] = None, fetch: bool = True) -> dict:
        """日更: (可选)拉最新原始日 → 以 production 模型生成预测工件。

        初始 production=baselines.ma5(§7.6): 用最近 6 个原始日 pivot 计算
        全市场 ma5 预测;学习模型晋升后由 retrain 管道产出的工件替换本路径
        (registry.production 驱动,接线属主协调管道)。
        """
        if fetch:
            from VolumePrediction.data import polygon_loader as pl   # 延迟(requests)
            today = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
            days = pl.trading_days("2026-01-01", today)
            target = date or days[-1]
            pl.ensure_day(target)
            self.s._day_cache.pop(target, None)
        ds = self.s._raw_dates()
        if date:
            ds = [d for d in ds if d <= date]
        if len(ds) < 2:
            return {"status": "error", "reason": "insufficient raw days"}
        use = ds[-6:]
        frames = []
        for d in use:
            day = self.s._load_day(d)
            if not day.empty:
                frames.append(day[["ticker", "dollar_volume"]].assign(date=d))
        longdf = pd.concat(frames)
        longdf = longdf[longdf["dollar_volume"] > 0]
        piv = longdf.pivot_table(index="ticker", columns="date",
                                 values="dollar_volume", aggfunc="first")
        v = np.log(piv)
        ma5 = v.iloc[:, -5:].mean(axis=1)
        last_v = v.iloc[:, -1]
        eta_now = (last_v - ma5).dropna()
        asof = use[-1]
        out = pd.DataFrame({
            "date": asof, "ticker": ma5.index, "pred_v": ma5.values,
            "pred_V": np.exp(ma5.values), "pred_eta": 0.0,
            "model_version": "baselines.ma5", "trained_through": asof,
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        }).dropna(subset=["pred_v"]).reset_index(drop=True)
        # 原子写 latest + PIT history
        for p in (self.s.art / _LATEST,
                  self.s.art / "history" / f"volume_forecast_{asof}.parquet"):
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            out.to_parquet(tmp, index=False)
            os.replace(tmp, p)
        # ── 学习模型分发(2026-08-01 ②晋升接线): registry.production 驱动。
        # production 为学习版本时,用其工件覆盖所覆盖票的行;其余票保持 ma5
        # (行级 model_version 标注,§5.4 混合服务)。任何失败 → 大声回退全 ma5,
        # 绝不静默(§5.3-①)。
        _prod_ver = None
        try:
            _prod_ver = self.s.registry.load().get("production")
        except Exception:  # noqa: BLE001
            pass
        _n_lgbm = 0
        if _prod_ver and _prod_ver != "baselines.ma5":
            try:
                from VolumePrediction import prod_model as _pm
                from VolumePrediction.data import polygon_loader as _pl2
                _fut = _pl2.trading_days(
                    asof, str((pd.Timestamp(asof) + pd.Timedelta(days=10)).date()))
                _target = next(d for d in _fut if d > asof)
                _art = self.s.art / "registry" / "artifacts" / _prod_ver
                # 工件 kind 决定服务模块(2026-08-03 E4): learned.rnn 走窄集序列
                # 路径(有状态 seq_tail 滚动),其余走 lgbm 路径。kind 读不到 →
                # 按 lgbm(既有行为不变)。
                _kind = ""
                try:
                    with open(_art / "meta.json") as _mf:
                        _kind = json.load(_mf).get("kind", "")
                except Exception:  # noqa: BLE001
                    pass
                if _kind == "learned.rnn":
                    from VolumePrediction import prod_model_rnn as _pmr
                    _lg = _pmr.serve(_art, _target, update_state=True).set_index("ticker")
                else:
                    _lg = _pm.serve(_art, _target).set_index("ticker")
                _mask = out["ticker"].isin(_lg.index)
                for _c in ("pred_v", "pred_V", "pred_eta",
                           "model_version", "trained_through"):
                    out.loc[_mask, _c] = out.loc[_mask, "ticker"].map(_lg[_c])
                _n_lgbm = int(_mask.sum())
                log.info(f"refresh: production={_prod_ver} 覆盖 {_n_lgbm}/{len(out)} 票"
                         f"(target={_target}),其余 ma5")
                # 覆盖后重写工件(原子写同上)
                for p in (self.s.art / _LATEST,
                          self.s.art / "history" / f"volume_forecast_{asof}.parquet"):
                    tmp = p.with_suffix(".tmp")
                    out.to_parquet(tmp, index=False)
                    os.replace(tmp, p)
            except Exception as _le:  # noqa: BLE001
                log.error(f"refresh: 学习模型服务失败 → 回退全 ma5(需排查!): {_le}")

        # ── blend3 分层服务(E10 实施-1,2026-08-15 代码就位;默认关)────────────
        # 启用 = registry.json 的 blend.enabled(8/17 拍板后由 ops.promote_blend
        # 置 true)。三层: RNN 层(blend_routing.rnn_layer,与影子同一事实源的
        # 路由门)→ lgbm 守其余覆盖票 → ma5 兜底。
        # seq_tail 纪律: 启用后**本路径是唯一滚动者**(update_state=True),
        # shadow_rnn 检测到 enabled 自动转只读 —— 双滚会把序列状态推快一天。
        # 任何失败 → 大声保留两层工件(lgbm+ma5),绝不静默。
        _blend_cfg = {}
        try:
            _blend_cfg = self.s.registry.load().get("blend") or {}
        except Exception:  # noqa: BLE001
            pass
        _n_rnn = 0
        if _blend_cfg.get("enabled"):
            try:
                from VolumePrediction import prod_model_rnn as _pmr2
                from VolumePrediction.blend_routing import (recent_measured_adv,
                                                            rnn_layer)
                from VolumePrediction.shadow_rnn import _held_tickers
                _rart = (self.s.art / "registry" / "artifacts"
                         / _blend_cfg["rnn_version"])
                # serve(T) 预测的是 **T 日**(锚 ma5v_next = T 之前 5 个交易日
                # 的均值,同 features/pipeline.py 的 ma5_v=shift(1).rolling(5))。
                # 所以 asof 收盘后要的目标是 next_trading_day(asof),与本函数
                # 上方 lgbm 路径的 _target 完全同款。
                # 2026-08-26 修: 原先传 asof,预测的是**今天**(已发生),被当作
                # 次日预测消费 → 整层晚一天。冻结路径下 target_date 不影响特征,
                # 所以这个错位一直隐形;P0 修好 serve 后它会立刻变成真实错一天。
                # "会撞断档守卫"的原注释不成立: 目标日连续递增,守卫自然满足。
                # 逐日补滚: seq_tail 落后时(如某日 RNN serve 失败后停滚)把
                # (seq_tail, target] 的交易日按序滚齐,否则次日永久断档。
                # 同日重跑: seq_tail 已到 target,serve 会拒绝 → 缓存,缓存丢了
                # 退影子当日工件 —— 绝不让重跑把当日工件降级回两层。
                _cache = _rart / f"blend_serve_{asof}.parquet"
                with open(_rart / "meta.json") as _mf2:
                    _seq_d = json.load(_mf2).get("seq_tail_date") or ""
                _rtgt = _pmr2.next_trading_day(asof)
                if _seq_d >= _rtgt:
                    if _cache.exists():
                        _rn = pd.read_parquet(_cache).set_index("ticker")
                        log.info(f"refresh: blend3 同日重跑 — 复用缓存 {_cache.name}")
                    else:
                        _shadow_p = (self.s.art / "shadow_rnn"
                                     / f"rnn_pred_{asof}.parquet")
                        _rn = pd.read_parquet(_shadow_p).set_index("ticker")
                        log.info(f"refresh: blend3 缓存缺失 — 退影子当日工件"
                                 f" {_shadow_p.name}")
                else:
                    _rn = None
                    from VolumePrediction.data import polygon_loader as _pl3
                    _tspan = _pl3.trading_days(
                        str((pd.Timestamp(_rtgt) - pd.Timedelta(days=60)).date()),
                        _rtgt)
                    for _d in [d for d in _tspan if _seq_d < d <= _rtgt]:
                        _rn = _pmr2.serve(_rart, _d,
                                          update_state=True).set_index("ticker")
                    if _rn is None:
                        raise RuntimeError(
                            f"blend3: seq_tail={_seq_d} 与 target={_rtgt} 间无交易日")
                    _ctmp = _cache.with_suffix(".tmp")
                    _rn.reset_index().to_parquet(_ctmp, index=False)
                    os.replace(_ctmp, _cache)
                    for _old in sorted(_rart.glob("blend_serve_*.parquet"))[:-5]:
                        _old.unlink()                  # 只留近 5 日缓存
                    # 同步写影子目录: AB 追踪/shadow_blend 逐位对拍继续吃同一份
                    # 预测(影子已转只读不再自产;evaluate 只认 rnn_pred_*)
                    _sp = self.s.art / "shadow_rnn" / f"rnn_pred_{asof}.parquet"
                    _sp.parent.mkdir(parents=True, exist_ok=True)
                    _stmp = _sp.with_suffix(".tmp")
                    _rn.reset_index().to_parquet(_stmp, index=False)
                    os.replace(_stmp, _sp)
                _pv = out.set_index("ticker")["pred_V"]
                _cov = _rn.index.intersection(_pv.index)
                _layer, _diag = rnn_layer(_cov, _pv.loc[_cov],
                                          _held_tickers(asof),
                                          recent_measured_adv(asof),
                                          full_coverage=bool(
                                              _blend_cfg.get("rnn_full_coverage")))
                # ── 反事实存档(2026-08-26 修 B): 覆盖前把 RNN 将要接管的那批票
                # 的 **两层(lgbm+ma5)** 预测原样存下来。没有它,AB 表的对照臂
                # 就是被 RNN 自己覆盖过的生产工件 —— 持仓票恰好全在 RNN 层里,
                # 于是 rnn_* 与 prod_* 逐位相同、rnn_wins 结构性恒 False,
                # RNN 连输 13/17 天(8/07 起)一声没吭。零额外算力: out 此刻
                # 正好就是两层结果。
                try:
                    _cfp = (self.s.art / "history"
                            / f"counterfactual_noblend_{asof}.parquet")
                    _cfp.parent.mkdir(parents=True, exist_ok=True)
                    _cftmp = _cfp.with_suffix(".tmp")
                    (out[out["ticker"].isin(_cov)]
                     .to_parquet(_cftmp, index=False))
                    os.replace(_cftmp, _cfp)
                    for _oldcf in sorted((self.s.art / "history").glob(
                            "counterfactual_noblend_*.parquet"))[:-90]:
                        _oldcf.unlink()               # 只留近 90 日
                except Exception as _cfe:  # noqa: BLE001 — 存档失败不阻断服务
                    log.error(f"refresh: 反事实存档失败(AB 对照臂会退化): {_cfe}")
                _m2 = out["ticker"].isin(_layer)
                for _c in ("pred_v", "pred_V", "pred_eta",
                           "model_version", "trained_through"):
                    if _c in _rn.columns:
                        out.loc[_m2, _c] = out.loc[_m2, "ticker"].map(_rn[_c])
                _n_rnn = int(_m2.sum())
                _n_lgbm_left = int((out["model_version"]
                                    .astype(str) == str(_prod_ver)).sum())
                log.info(f"refresh: blend3 RNN 层 {_n_rnn}/{len(out)} 票 "
                         f"(gate={_diag['gate_source']}, thr={_diag['thr']:,.0f},"
                         f" 实测抬升 {_diag['n_measured_lifted']}),"
                         f"lgbm 守 {_n_lgbm_left} 票,其余 ma5")
                for p in (self.s.art / _LATEST,
                          self.s.art / "history"
                          / f"volume_forecast_{asof}.parquet"):
                    tmp = p.with_suffix(".tmp")
                    out.to_parquet(tmp, index=False)
                    os.replace(tmp, p)
            except Exception as _be:  # noqa: BLE001
                log.error(f"refresh: blend3 分层失败 → 保留 lgbm+ma5 两层"
                          f"(需排查!): {_be}")

        resid_std = float(eta_now.std())
        self.s.registry.record_model(
            "baselines.ma5", kind="baseline", trained_through=asof,
            metrics={"n": int(len(out)), "n_learned_rows": _n_lgbm,
                     "n_rnn_rows": _n_rnn},
            resid_std=resid_std,
            status="production" if not _n_lgbm else "fallback_component")
        data = self.s.registry.load()
        if not data.get("production"):
            self.s.registry.promote("baselines.ma5", by="bootstrap")
        return {"status": "ok", "asof": asof, "n": int(len(out)),
                "resid_std": resid_std}

    def set_blend(self, enabled: bool, rnn_version: Optional[str] = None,
                  by: str = "user",
                  full_coverage: Optional[bool] = None) -> dict:
        """blend3 分层服务总开关(E10 实施-1;晋升纪律与 promote 同:人工显式)。

        enabled=True 前提: rnn_version 工件存在;写 registry.blend 并留痕。
        回退 = set_blend(False) 单行,下一次 refresh 即回两层(lgbm+ma5)。

        full_coverage: RNN 是否吃满整个学习层(None=不动,沿用现值)。
        None 而不是 False 是**故意的** —— 覆盖度是独立的晋升决策(生产现值由
        用户 2026-08-17 终判置 true),不该被"开/关 blend"这个动作顺手改掉。
        """
        data = self.s.registry.load()
        cur = dict(data.get("blend") or {})
        ver = rnn_version or cur.get("rnn_version")
        if enabled:
            if not ver:
                raise ValueError("set_blend(True) 需要 rnn_version")
            art = self.s.art / "registry" / "artifacts" / ver
            if not art.exists():
                raise FileNotFoundError(f"RNN 工件不存在: {art}")
        # 2026-08-26 修: 原先是整字典覆写 `data["blend"] = {...}`,而字面量里
        # 没有 rnn_full_coverage/full_coverage_by/full_coverage_at —— 这三个键
        # 是 8/17 晋升时手写进 registry 的,谁调一次 set_blend() 就被静默抹掉。
        # 后果: refresh 只读 `_blend_cfg.get("rnn_full_coverage")`,缺键 = False,
        # 于是一次 set_blend(False) 应急回退 + 事后 set_blend(True) 复原,RNN
        # 路由就从"吃满学习层"悄悄掉回 50/50 分层,日志一个字都不说。
        # 改成**合并**语义: 只覆写本次动作显式负责的字段,其余原样保留。
        _at = pd.Timestamp.now().isoformat(timespec="seconds")
        cur.update({"enabled": bool(enabled), "variant": "blend3",
                    "rnn_version": ver, "by": by, "at": _at})
        if full_coverage is not None:
            cur["rnn_full_coverage"] = bool(full_coverage)
            cur["full_coverage_by"] = by
            cur["full_coverage_at"] = _at
        data["blend"] = cur
        self.s.registry.save(data)
        # 生效路由必须每次都打出来 —— 这个 bug 之所以能潜伏,正是因为改了路由
        # 却无声无息。full_coverage=False 时 RNN 只吃 blend_routing 的门控子集。
        log.info(f"[OPS] blend3 {'ENABLED' if enabled else 'disabled'} "
                 f"(rnn={ver}, full_coverage="
                 f"{bool(cur.get('rnn_full_coverage'))}, by={by})")
        return data["blend"]

    def retrain(self, config: Optional[dict] = None) -> dict:
        """登记再训练请求(设计即如此,非缺口——审计留档 2026-08-09)。

        训练**有意不在服务进程内执行**: 重训吃数小时算力,放进只读低延迟的服务
        API 会阻塞四策略消费端、破坏 §5.3 服务保证。本方法只登记带时间戳的
        request(留痕可审计);实际执行走外部脚本——月度统计重冻结 refreeze.py
        (conductor/vp_refreeze_monthly.sh)、深模型重训 prod_model.py /
        rnn_export.py;晋升永远人工 ops.promote(version, by='user')。
        """
        ver = f"retrain_request_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
        return self.s.registry.record_model(ver, kind="request",
                                            meta=config or {}, status="requested")

    def promote(self, version: str, by: str = "user") -> dict:
        return self.s.registry.promote(version, by=by)

    def backfill(self, start: str, end: str) -> dict:
        from VolumePrediction.data import polygon_loader as pl       # 延迟
        return pl.backfill(start, end)

    def coverage(self, start: Optional[str] = None,
                 end: Optional[str] = None) -> dict:
        ds = self.s._raw_dates()
        if start:
            ds = [d for d in ds if d >= start]
        if end:
            ds = [d for d in ds if d <= end]
        return {"raw_days": len(ds),
                "first": ds[0] if ds else None, "last": ds[-1] if ds else None}
