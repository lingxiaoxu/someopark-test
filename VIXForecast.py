"""
VIXForecast.py — 每日 VIX 预测信号模块

用 Chronos-2 (fine-tuned) 预测未来 10 个交易日 VIX 走向，
输出归一化分数供 RegimeDetector._score_volatility() 使用。

设计原则（零数据泄露）：
  - context：截至今天的 CTX_DAYS 天历史（不含未来）
  - past_covariates (VIX9D/VIX3M)：与 context 等长，OOS 期未知 → fit() 用最后值填充
  - future_covariates (FOMC 特征)：完全前向已知（公告日程）→ context + OOS 均可使用
  - 训练样本 up_to_idx = len(vix_df)：全部历史，预测窗口是"明天起"，无泄露

双模型策略：
  finetune-full : VIX + VIX9D/VIX3M past_covariates（无 FOMC）
  finetune-fomc : VIX + VIX9D/VIX3M past_covariates + FOMC future_covariates

权重（来自 WalkForward OOS Dir Acc）：
  W_FULL = 0.542  (65.0% Dir Acc)
  W_FOMC = 0.458  (55.0% Dir Acc)

用法（独立运行）：
    set -a && source .env && set +a
    conda run -n someopark_run --no-capture-output python VIXForecast.py
    conda run -n someopark_run --no-capture-output python VIXForecast.py --finetune
    conda run -n someopark_run --no-capture-output python VIXForecast.py --finetune --no-cache
"""

import os
import glob
import json
import logging
import warnings
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path

warnings.filterwarnings('ignore')
log = logging.getLogger('VIXForecast')

# ── 默认参数 ─────────────────────────────────────────────────────────────────
MODEL_ID         = 'amazon/chronos-2'
CTX_DAYS         = 504      # context 长度（2年历史）
PRED_DAYS        = 10       # 预测未来天数
FT_STEPS         = 300      # fine-tuning 步数
FT_LR            = 5e-6     # fine-tuning 学习率
FT_BATCH         = 16
FT_CKPT_DIR_FULL = 'historical_runs/vix_chronos2/ft_ckpt_full'
FT_CKPT_DIR_FOMC = 'historical_runs/vix_chronos2/ft_ckpt_fomc'
CACHE_FILE       = 'historical_runs/vix_chronos2/vix_forecast_cache.json'
FOMC_DIR         = 'price_data/macro/fomc'

# 集成权重（来自 walk-forward OOS Dir Acc）
W_FULL = 0.542
W_FOMC = 0.458

# FOMC rule-override 阈值（交易日）
FOMC_WINDOW_DAYS = 10

# score 映射：预测变化率 → score
SCORE_SCALE = 3.0
SCORE_LO    = 0.15
SCORE_HI    = 0.85


# ── 数据加载 ──────────────────────────────────────────────────────────────────
def _load_vix_data() -> pd.DataFrame:
    """
    返回 DataFrame，列：vix, vix9d, vix3m
    vix9d / vix3m 若不存在则填 NaN（协变量在 build_input 中会被跳过）
    """
    files = sorted(glob.glob('price_data/macro/vix/vix_*.parquet'))
    if not files:
        raise FileNotFoundError('找不到 price_data/macro/vix/vix_*.parquet')
    df = pd.concat([pd.read_parquet(f) for f in files]).sort_index()
    df.index = pd.to_datetime(df.index)
    result = df[['close']].rename(columns={'close': 'vix'})

    files9d = sorted(glob.glob('price_data/macro/vix/vix9d_*.parquet'))
    if files9d:
        df9 = pd.concat([pd.read_parquet(f) for f in files9d]).sort_index()
        df9.index = pd.to_datetime(df9.index)
        col = 'close' if 'close' in df9.columns else df9.columns[0]
        result['vix9d'] = df9[col]
    else:
        result['vix9d'] = np.nan

    files3m = sorted(glob.glob('price_data/macro/vix/vix3m_*.parquet'))
    if files3m:
        df3 = pd.concat([pd.read_parquet(f) for f in files3m]).sort_index()
        df3.index = pd.to_datetime(df3.index)
        col = 'close' if 'close' in df3.columns else df3.columns[0]
        result['vix3m'] = df3[col]
    else:
        result['vix3m'] = np.nan

    result = result.dropna(subset=['vix'])
    return result


# ── FOMC 特征 ─────────────────────────────────────────────────────────────────
def _load_fomc_dates() -> list:
    files = sorted(glob.glob(f'{FOMC_DIR}/fomc_*.json'))
    if not files:
        return []
    dates = []
    for f in files:
        dates.extend(json.loads(Path(f).read_text()))
    return sorted(set(pd.to_datetime(d).date() for d in dates))


def _build_fomc_features(trading_dates: pd.DatetimeIndex, fomc_dates: list) -> pd.DataFrame:
    """
    三列：days_to_next_fomc, days_since_last_fomc, is_fomc_week
    全部均为已知的日历信息，无泄露，可用作 future_covariates。
    """
    import bisect
    import datetime as _dt

    fomc_set  = set(fomc_dates)
    fomc_list = sorted(fomc_dates)
    n = len(trading_dates)
    days_to_next = np.zeros(n, dtype=np.float32)
    days_since   = np.zeros(n, dtype=np.float32)
    is_fomc_wk   = np.zeros(n, dtype=np.float32)

    for i, ts in enumerate(trading_dates):
        d = ts.date()
        idx_next = bisect.bisect_left(fomc_list, d)
        days_to_next[i] = min((fomc_list[idx_next] - d).days, 50) if idx_next < len(fomc_list) else 50.0
        idx_prev = idx_next - 1
        days_since[i] = min((d - fomc_list[idx_prev]).days, 50) if idx_prev >= 0 else 50.0
        near = any(d + _dt.timedelta(days=o) in fomc_set for o in [-2, -1, 0, 1, 2])
        is_fomc_wk[i] = 1.0 if near else 0.0

    return pd.DataFrame({
        'days_to_next_fomc':    days_to_next,
        'days_since_last_fomc': days_since,
        'is_fomc_week':         is_fomc_wk,
    }, index=trading_dates)


# ── 构建推理输入 ──────────────────────────────────────────────────────────────
def _build_predict_input(ctx_vix: np.ndarray,
                         ctx_vix9d: np.ndarray | None,
                         ctx_vix3m: np.ndarray | None,
                         fomc_ctx: np.ndarray | None = None,
                         fomc_oos: np.ndarray | None = None,
                         fomc_ctx_since: np.ndarray | None = None,
                         fomc_oos_since: np.ndarray | None = None,
                         fomc_ctx_wk: np.ndarray | None = None,
                         fomc_oos_wk: np.ndarray | None = None,
                         ) -> dict | np.ndarray:
    """
    构建 predict_quantiles 单条推理输入。
    Chronos-2 约束：
      past_covariates  长度 = history_length (CTX_DAYS)
      future_covariates长度 = prediction_length (PRED_DAYS)
      future_cov keys 必须是 past_cov keys 的子集
    """
    has_fomc = (fomc_ctx is not None and fomc_oos is not None)
    past_cov: dict = {}

    if ctx_vix9d is not None and not np.all(np.isnan(ctx_vix9d)):
        s = pd.Series(ctx_vix9d).ffill().bfill()
        past_cov['vix9d'] = s.values.astype(np.float32)
    if ctx_vix3m is not None and not np.all(np.isnan(ctx_vix3m)):
        s = pd.Series(ctx_vix3m).ffill().bfill()
        past_cov['vix3m'] = s.values.astype(np.float32)

    if has_fomc:
        past_cov['days_to_next_fomc']    = fomc_ctx.astype(np.float32)
        past_cov['days_since_last_fomc'] = fomc_ctx_since.astype(np.float32)
        past_cov['is_fomc_week']         = fomc_ctx_wk.astype(np.float32)

    if not past_cov:
        return ctx_vix

    result: dict = {'target': ctx_vix, 'past_covariates': past_cov}

    if has_fomc:
        result['future_covariates'] = {
            'days_to_next_fomc':    fomc_oos.astype(np.float32),
            'days_since_last_fomc': fomc_oos_since.astype(np.float32),
            'is_fomc_week':         fomc_oos_wk.astype(np.float32),
        }

    return result


# ── Fine-tuning 数据准备 ──────────────────────────────────────────────────────
def _build_finetune_inputs(vix_df: pd.DataFrame,
                           fomc_df: pd.DataFrame | None,
                           use_fomc: bool) -> list:
    """
    从 vix_df 全量历史滚动切出训练样本，步长=PRED_DAYS（不重叠）。
    预测窗口是今天之后，训练数据全在今天及之前，无泄露。

    fit() 格式要求：
      target          : ctx + oos 拼接（ctx_days + pred_days）
      past_covariates : 等长（VIX9D/VIX3M OOS 段用最后值填充，因为当时未知）
      future_covariates: OOS 段长度（FOMC 真实值，日程已知）
    """
    train_inputs = []
    vix_vals   = vix_df['vix'].values.astype(np.float32)
    vix9d_vals = vix_df['vix9d'].values if 'vix9d' in vix_df.columns else None
    vix3m_vals = vix_df['vix3m'].values if 'vix3m' in vix_df.columns else None

    fomc_vals       = fomc_df['days_to_next_fomc'].values    if (use_fomc and fomc_df is not None) else None
    fomc_since_vals = fomc_df['days_since_last_fomc'].values if (use_fomc and fomc_df is not None) else None
    fomc_wk_vals    = fomc_df['is_fomc_week'].values         if (use_fomc and fomc_df is not None) else None

    min_len = CTX_DAYS + PRED_DAYS
    if len(vix_vals) < min_len:
        return train_inputs

    start = 0
    while start + min_len <= len(vix_vals):
        end_ctx = start + CTX_DAYS
        end_oos = end_ctx + PRED_DAYS

        ctx_vix = vix_vals[start:end_ctx]
        ctx_9d  = vix9d_vals[start:end_ctx] if vix9d_vals is not None else None
        ctx_3m  = vix3m_vals[start:end_ctx] if vix3m_vals is not None else None
        tgt_vix = vix_vals[end_ctx:end_oos]

        f_ctx       = fomc_vals[start:end_ctx]         if fomc_vals is not None else None
        f_oos       = fomc_vals[end_ctx:end_oos]       if fomc_vals is not None else None
        f_ctx_since = fomc_since_vals[start:end_ctx]   if fomc_since_vals is not None else None
        f_oos_since = fomc_since_vals[end_ctx:end_oos] if fomc_since_vals is not None else None
        f_ctx_wk    = fomc_wk_vals[start:end_ctx]      if fomc_wk_vals is not None else None
        f_oos_wk    = fomc_wk_vals[end_ctx:end_oos]    if fomc_wk_vals is not None else None

        inp = _build_predict_input(ctx_vix, ctx_9d, ctx_3m,
                                   fomc_ctx=f_ctx, fomc_oos=f_oos,
                                   fomc_ctx_since=f_ctx_since, fomc_oos_since=f_oos_since,
                                   fomc_ctx_wk=f_ctx_wk, fomc_oos_wk=f_oos_wk)

        if isinstance(inp, dict):
            # target 延长到 ctx + oos 全长
            inp['target'] = np.concatenate([inp['target'], tgt_vix.astype(np.float32)])
            # past_cov 延长到全长
            past_cov   = inp.get('past_covariates', {})
            future_cov = inp.get('future_covariates', {})
            for k in list(past_cov.keys()):
                if k in future_cov:
                    # FOMC：OOS 段已知，直接拼真实值
                    past_cov[k] = np.concatenate([past_cov[k], future_cov[k]])
                else:
                    # VIX9D/VIX3M：OOS 未知，用最后值填充（无泄露）
                    past_cov[k] = np.concatenate([past_cov[k],
                                                   np.full(PRED_DAYS, past_cov[k][-1],
                                                           dtype=np.float32)])
            inp['past_covariates'] = past_cov
        else:
            # 统一包成 dict，确保 inputs 全为 dict 类型（Chronos2Dataset 要求一致）
            inp = {'target': np.concatenate([inp, tgt_vix]).astype(np.float32)}

        train_inputs.append(inp)
        start += PRED_DAYS

    return train_inputs


# ── Checkpoint 管理 ───────────────────────────────────────────────────────────
def _ckpt_is_fresh(ckpt_dir: str) -> bool:
    meta = Path(ckpt_dir) / 'ft_meta.json'
    if not meta.exists():
        return False
    try:
        info = json.loads(meta.read_text())
        return info.get('date') == datetime.today().strftime('%Y-%m-%d')
    except Exception:
        return False


def _save_ckpt_meta(ckpt_dir: str, n_samples: int, model_tag: str):
    meta = Path(ckpt_dir) / 'ft_meta.json'
    meta.write_text(json.dumps({
        'date':      datetime.today().strftime('%Y-%m-%d'),
        'model':     MODEL_ID,
        'model_tag': model_tag,
        'n_samples': n_samples,
        'steps':     FT_STEPS,
    }))


# ── score 计算 ────────────────────────────────────────────────────────────────
def _compute_score(median: float, current_vix: float) -> tuple[float, float, str]:
    change_pct = (median - current_vix) / current_vix
    score = float(np.clip(0.50 + change_pct * SCORE_SCALE, SCORE_LO, SCORE_HI))
    if change_pct > 0.03:
        direction = 'up'
    elif change_pct < -0.03:
        direction = 'down'
    else:
        direction = 'flat'
    return round(score, 4), round(change_pct, 4), direction


# ── 单模型推理 ────────────────────────────────────────────────────────────────
def _run_single_model(base_pipeline,
                      ckpt_dir: str,
                      ft_inputs: list | None,
                      predict_input,
                      current_vix: float,
                      model_tag: str,
                      device: str) -> dict:
    """
    Fine-tune（若需要）并推理，返回 sub-result dict。
    finetune=True  → 使用 ckpt_dir，今日已有则复用
    finetune=False → ft_inputs=None，直接用 base_pipeline 推理
    """
    import torch
    from chronos import BaseChronosPipeline

    if ft_inputs is not None:
        os.makedirs(ckpt_dir, exist_ok=True)
        if _ckpt_is_fresh(ckpt_dir):
            log.info(f'[{model_tag}] 加载今日 checkpoint: {ckpt_dir}')
            pipeline = BaseChronosPipeline.from_pretrained(
                ckpt_dir, device_map=device, dtype=torch.float32)
        else:
            log.info(f'[{model_tag}] 开始 fine-tuning ({len(ft_inputs)} 样本, {FT_STEPS} 步)...')
            pipeline = base_pipeline.fit(
                inputs            = ft_inputs,
                prediction_length = PRED_DAYS,
                num_steps         = FT_STEPS,
                learning_rate     = FT_LR,
                batch_size        = FT_BATCH,
                finetune_mode     = 'full',
                output_dir        = ckpt_dir,
                finetuned_ckpt_name = 'finetuned-ckpt',
            )
            _save_ckpt_meta(ckpt_dir, len(ft_inputs), model_tag)
            log.info(f'[{model_tag}] fine-tuning 完成')
    else:
        pipeline = base_pipeline

    quantile_levels = [0.1, 0.5, 0.9]
    q_list, _ = pipeline.predict_quantiles(
        [predict_input],
        prediction_length = PRED_DAYS,
        quantile_levels   = quantile_levels,
    )
    q = q_list[0]   # shape: (1, PRED_DAYS, 3)
    q10    = float(q[0, :, 0].mean())
    median = float(q[0, :, 1].mean())
    q90    = float(q[0, :, 2].mean())

    score, change_pct, direction = _compute_score(median, current_vix)
    return {
        'score':       score,
        'pred_median': round(median, 3),
        'pred_q10':    round(q10, 3),
        'pred_q90':    round(q90, 3),
        'change_pct':  change_pct,
        'direction':   direction,
    }


# ── 集成选择 ──────────────────────────────────────────────────────────────────
def _select_ensemble(res_full: dict, res_fomc: dict,
                     fomc_df: pd.DataFrame | None,
                     use_fomc_rule: bool,
                     fomc_window_days: int) -> dict:
    """
    默认：加权平均（权重来自 WalkForward OOS Dir Acc）。
    use_fomc_rule=True 且 FOMC 在 fomc_window_days 交易日内时，改用 fomc 模型。
    """
    if use_fomc_rule and fomc_df is not None:
        days_to_fomc_cal = float(fomc_df['days_to_next_fomc'].iloc[-1])
        days_to_fomc_td  = int(days_to_fomc_cal * 0.714)
        if days_to_fomc_td <= fomc_window_days:
            log.info(f'[ensemble] FOMC rule 触发: 距下次FOMC约 {days_to_fomc_td} 交易日 → 使用 fomc 模型')
            return {**res_fomc,
                    'ensemble_method': f'fomc-rule({days_to_fomc_td}td)',
                    'models': {'full': res_full, 'fomc': res_fomc}}

    score    = round(W_FULL * res_full['score']       + W_FOMC * res_fomc['score'],       4)
    median   = round(W_FULL * res_full['pred_median'] + W_FOMC * res_fomc['pred_median'], 3)
    q10      = round(W_FULL * res_full['pred_q10']    + W_FOMC * res_fomc['pred_q10'],    3)
    q90      = round(W_FULL * res_full['pred_q90']    + W_FOMC * res_fomc['pred_q90'],    3)
    change   = round(W_FULL * res_full['change_pct']  + W_FOMC * res_fomc['change_pct'],  4)
    direction = 'up' if change > 0.03 else ('down' if change < -0.03 else 'flat')

    return {
        'score':           score,
        'pred_median':     median,
        'pred_q10':        q10,
        'pred_q90':        q90,
        'change_pct':      change,
        'direction':       direction,
        'ensemble_method': 'weighted-dirAcc',
        'models':          {'full': res_full, 'fomc': res_fomc},
    }


# ── 主预测函数 ────────────────────────────────────────────────────────────────
def run_vix_forecast(finetune: bool = False,
                     use_cache: bool = True,
                     use_fomc_rule: bool = False,
                     fomc_window_days: int = FOMC_WINDOW_DAYS) -> dict:
    """
    运行 VIX 预测，返回（顶层键与旧版完全兼容）：
      score, pred_median, pred_q10, pred_q90, current_vix,
      change_pct, direction, mode, as_of
    新增（可选）：
      ensemble_method, models (sub-dicts for full/fomc)
    """
    from chronos import BaseChronosPipeline
    import torch

    today = datetime.today().strftime('%Y-%m-%d')
    cache_mode = 'finetune-dual' if finetune else 'zero-shot-cov'

    # ── 读缓存 ────────────────────────────────────────────────────────────────
    if use_cache and os.path.exists(CACHE_FILE):
        try:
            cached = json.loads(Path(CACHE_FILE).read_text())
            if cached.get('as_of') == today and cached.get('mode') == cache_mode:
                log.info(f"VIXForecast: 使用今日缓存  score={cached['score']:.3f}")
                return cached
        except Exception:
            pass

    # ── 加载数据 ──────────────────────────────────────────────────────────────
    vix_df = _load_vix_data()
    if len(vix_df) < CTX_DAYS + PRED_DAYS:
        raise ValueError(f'VIX 数据不足：{len(vix_df)} 天 < {CTX_DAYS + PRED_DAYS}')

    ctx_vix  = vix_df['vix'].values[-CTX_DAYS:].astype(np.float32)
    ctx_vix9d = vix_df['vix9d'].values[-CTX_DAYS:] if 'vix9d' in vix_df.columns else None
    ctx_vix3m = vix_df['vix3m'].values[-CTX_DAYS:] if 'vix3m' in vix_df.columns else None
    current_vix = float(ctx_vix[-1])

    # FOMC 特征（context 段 + 未来 PRED_DAYS 段）
    fomc_df    = None
    f_ctx      = f_oos = f_ctx_since = f_oos_since = f_ctx_wk = f_oos_wk = None
    fomc_dates = _load_fomc_dates()
    if fomc_dates:
        # 为未来 PRED_DAYS 天生成虚拟日期（从今天起 N 个工作日）
        last_date  = vix_df.index[-1]
        future_idx = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=PRED_DAYS)
        full_idx   = pd.DatetimeIndex(list(vix_df.index) + list(future_idx))
        fomc_full  = _build_fomc_features(full_idx, fomc_dates)
        fomc_df    = fomc_full.iloc[:len(vix_df)]  # 历史段（对齐 vix_df）

        n_hist = len(vix_df)
        # context 段：历史最后 CTX_DAYS 天（不含未来）
        f_ctx       = fomc_full['days_to_next_fomc'].values[n_hist - CTX_DAYS : n_hist]
        f_ctx_since = fomc_full['days_since_last_fomc'].values[n_hist - CTX_DAYS : n_hist]
        f_ctx_wk    = fomc_full['is_fomc_week'].values[n_hist - CTX_DAYS : n_hist]
        # OOS 段：未来 PRED_DAYS 天（日历已知，无泄露）
        f_oos       = fomc_full['days_to_next_fomc'].values[n_hist : n_hist + PRED_DAYS]
        f_oos_since = fomc_full['days_since_last_fomc'].values[n_hist : n_hist + PRED_DAYS]
        f_oos_wk    = fomc_full['is_fomc_week'].values[n_hist : n_hist + PRED_DAYS]

    # ── 构建推理输入 ──────────────────────────────────────────────────────────
    # finetune-full: past_cov 只含 VIX9D/VIX3M，无 future_cov
    predict_input_full = _build_predict_input(ctx_vix, ctx_vix9d, ctx_vix3m)
    # finetune-fomc: past_cov 含 VIX9D/VIX3M + FOMC ctx，future_cov 含 FOMC oos
    predict_input_fomc = _build_predict_input(
        ctx_vix, ctx_vix9d, ctx_vix3m,
        fomc_ctx=f_ctx, fomc_oos=f_oos,
        fomc_ctx_since=f_ctx_since, fomc_oos_since=f_oos_since,
        fomc_ctx_wk=f_ctx_wk, fomc_oos_wk=f_oos_wk,
    ) if f_ctx is not None else predict_input_full

    # ── 设备 ─────────────────────────────────────────────────────────────────
    device = 'mps' if torch.backends.mps.is_available() else \
             ('cuda' if torch.cuda.is_available() else 'cpu')

    # ── 推理（zero-shot 或 finetune 双模型）───────────────────────────────────
    if finetune:
        base_pipeline = BaseChronosPipeline.from_pretrained(
            MODEL_ID, device_map=device, dtype=torch.float32)

        # finetune-full 训练输入
        ft_inputs_full = _build_finetune_inputs(vix_df, None, use_fomc=False)
        log.info(f'finetune-full 训练样本数: {len(ft_inputs_full)}')

        # finetune-fomc 训练输入
        ft_inputs_fomc = _build_finetune_inputs(
            vix_df, fomc_df if fomc_df is not None else None,
            use_fomc=(fomc_df is not None))
        log.info(f'finetune-fomc 训练样本数: {len(ft_inputs_fomc)}')

        res_full = _run_single_model(
            base_pipeline, FT_CKPT_DIR_FULL, ft_inputs_full,
            predict_input_full, current_vix, 'full', device)

        res_fomc = _run_single_model(
            base_pipeline, FT_CKPT_DIR_FOMC, ft_inputs_fomc,
            predict_input_fomc, current_vix, 'fomc', device)

        result = _select_ensemble(res_full, res_fomc, fomc_df, use_fomc_rule, fomc_window_days)
        result['mode'] = 'finetune-dual'

    else:
        # Zero-shot：使用 zero-shot-cov 模式（VIX9D/VIX3M past_cov，无 finetune）
        pipeline = BaseChronosPipeline.from_pretrained(
            MODEL_ID, device_map=device, dtype=torch.float32)
        res_zs = _run_single_model(
            pipeline, '', None, predict_input_full, current_vix, 'zero-shot', device)
        result = res_zs
        result['mode'] = 'zero-shot-cov'

    result['current_vix'] = round(current_vix, 3)
    result['as_of']       = today

    # ── 写缓存 ────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    Path(CACHE_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    log.info(f"VIXForecast: VIX当前={current_vix:.1f} → 预测均值={result['pred_median']:.1f} "
             f"变化={result['change_pct']:+.1%}  score={result['score']:.3f}  方向={result['direction']}")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='VIX Chronos-2 今日预测（双模型集成）')
    parser.add_argument('--finetune',      action='store_true', help='使用 fine-tuned 双模型')
    parser.add_argument('--fomc-rule',     action='store_true', help='FOMC 接近时切换为 fomc 模型')
    parser.add_argument('--fomc-window',   type=int, default=FOMC_WINDOW_DAYS,
                        help=f'FOMC rule 阈值（交易日，默认 {FOMC_WINDOW_DAYS}）')
    parser.add_argument('--no-cache',      action='store_true', help='忽略今日缓存，强制重新推理')
    args = parser.parse_args()

    result = run_vix_forecast(
        finetune=args.finetune,
        use_cache=not args.no_cache,
        use_fomc_rule=args.fomc_rule,
        fomc_window_days=args.fomc_window,
    )

    print(f"\n{'='*60}")
    print(f"VIX 预测信号  ({result['as_of']})  [{result['mode']}]")
    print(f"{'='*60}")
    print(f"  当前 VIX  : {result['current_vix']:.2f}")
    print(f"  预测均值  : {result['pred_median']:.2f}  "
          f"[P10={result['pred_q10']:.2f}, P90={result['pred_q90']:.2f}]")
    print(f"  预期变化  : {result['change_pct']:+.1%}  → {result['direction'].upper()}")
    print(f"  Regime分数: {result['score']:.4f}  "
          f"({'偏MTFS↑' if result['score'] > 0.55 else '偏MRPT↓' if result['score'] < 0.45 else '中性'})")
    ens = result.get('ensemble_method', '')
    if ens:
        print(f"  集成方式  : {ens}")
    if 'models' in result:
        m = result['models']
        print(f"  [full] 预测={m['full']['pred_median']:.2f}  "
              f"Δ={m['full']['change_pct']:+.1%}  score={m['full']['score']:.4f}")
        print(f"  [fomc] 预测={m['fomc']['pred_median']:.2f}  "
              f"Δ={m['fomc']['change_pct']:+.1%}  score={m['fomc']['score']:.4f}")
    print(f"{'='*60}")
