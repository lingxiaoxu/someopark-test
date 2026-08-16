"""
SimilarityEngine — Pluggable Macro Similarity Framework
========================================================
Computes similarity between macro states using multiple methods.
Used by MCPS.macro_cond_sharpe() to weight historical returns
by how similar each day's macro environment is to today's.

Methods:
  - euclidean: Current production method. Raw Euclidean distance with
               optional z-normalization for non-z-scored features.
  - autoencoder: Train a small autoencoder on 20+ macro indicators,
                 compute distance in latent space (8-16 dims).
  - (future) text_embedding: Encode macro state as text → sentence-transformer
                             → cosine similarity in 768-dim space.

Usage:
    from SimilarityEngine import SimilarityEngine

    engine = SimilarityEngine(method="euclidean")  # or "autoencoder"
    weights = engine.compute_weights(macro_matrix, today_vector)
    # weights: (T,) array, higher = more similar to today

Architecture:
    SimilarityEngine (facade)
    ├── EuclideanMethod     — current production logic
    ├── AutoencoderMethod   — learned latent space
    └── (future methods)

All methods return the same interface:
    compute_weights(macro_matrix, today_vector) → np.ndarray of shape (T,)
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Abstract base
# ═══════════════════════════════════════════════════════════════════════════

class SimilarityMethod(ABC):
    """Base class for similarity computation methods."""

    @abstractmethod
    def compute_weights(
        self,
        macro_matrix: np.ndarray,
        today_vector: np.ndarray,
        feature_names: List[str],
    ) -> np.ndarray:
        """
        Compute similarity weights for each historical day.

        Parameters
        ----------
        macro_matrix : (T, n_features) array of historical macro states
        today_vector : (n_features,) array of today's macro state
        feature_names : list of feature names (for normalization decisions)

        Returns
        -------
        weights : (T,) array, higher = more similar to today
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
#  Method 1: Euclidean (current production)
# ═══════════════════════════════════════════════════════════════════════════

# Features with large numeric range that benefit from log-transform before z-scoring.
# Identified by range > 50 in raw values (e.g. vix 9-83, move 37-183, consumer_sent 50-101).
# Log-transform compresses the scale and makes distribution more symmetric.
_LOG_TRANSFORM_FEATURES = {
    'vix', 'vix3m', 'vix9d', 'move', 'consumer_sent',
    'arkk_20d', 'nvda_20d', 'soxx_20d', 'uso_20d',
}

# ── 非平稳"水平量"特征 → rolling z-score(2026-07-31 修复) ──────────────────
# 这7个是宏观水平序列(利率/失业率/通胀预期/信心指数等),存在多年期 regime 漂移:
# 静态全样本 z-score 无法去除趋势——如 consumer_sent 低迷数月后,近期所有日子
# 共享同一个极端 z 值,把"相似度"退化成"近因"。改用 252 日 rolling z(与
# MacroStateStore._rolling_z 同窗口口径),恢复平稳性。
# 要求: 输入列必须按时间升序(本仓库所有调用方 store.load/MacroSimilarity 均满足)。
_ROLLING_Z_FEATURES = {
    'effr', 'unrate', 'tnx', 'yield_curve', 'nfci',
    'consumer_sent', 'breakeven_10y',
}
ROLLING_Z_WINDOW = 252
_ROLLING_Z_MIN_PERIODS = 60   # 首年内用扩张窗(min 20)防 NaN,首 20 行置 0
# z 截断: 阶梯型序列(effr/unrate 等月更或平台期)在平台后首次跳变时滚动 sd≈0.03,
# z 会爆到 ±15(2024-09 首降息 -15.8σ / COVID unrate +15σ),单轴平方距离 225
# 淹没其余 22 维。截 ±4 保留"极端"排序信息但不让单轴独裁(2026-07-31 数据体检)。
_ROLLING_Z_CLIP = 4.0


def _smart_normalize_column(
    col: np.ndarray,
    today_val: float,
    feature_name: str,
) -> tuple:
    """
    Smart normalization for a single feature column + today's value.

    Rules:
      - Feature ends with '_z': already z-scored by MacroStateStore → skip
      - Feature in _LOG_TRANSFORM_FEATURES: log-transform first
      - Feature in _ROLLING_Z_FEATURES: 252d rolling z-score(去 regime 漂移,
        要求列按时间升序;today 用尾窗统计归一)
      - Otherwise: static full-sample z-score

    Returns (normalized_col, normalized_today)
    """
    if feature_name.endswith('_z'):
        # Already z-scored — do NOT double-normalize
        return col, today_val

    if feature_name in _LOG_TRANSFORM_FEATURES:
        # Log-transform: shift to positive range, then log
        floor = max(float(col.min()), 0.01)  # avoid log(0)
        col = np.log(np.maximum(col, floor))
        today_val = np.log(max(today_val, floor))

    if feature_name in _ROLLING_Z_FEATURES:
        # Rolling z-score(非平稳水平量): 每行用其前 252 日窗口统计
        import pandas as _pd
        s = _pd.Series(col)
        mu_r = s.rolling(ROLLING_Z_WINDOW, min_periods=_ROLLING_Z_MIN_PERIODS).mean()
        sd_r = s.rolling(ROLLING_Z_WINDOW, min_periods=_ROLLING_Z_MIN_PERIODS).std()
        # 首年 warmup: 扩张窗(min 20)填充,再早置 0(2017 年头几周,权重噪声可忽略)
        mu_e = s.expanding(min_periods=20).mean()
        sd_e = s.expanding(min_periods=20).std()
        mu_r = mu_r.fillna(mu_e)
        sd_r = sd_r.fillna(sd_e)
        z = ((s - mu_r) / sd_r.replace(0.0, np.nan)).clip(-_ROLLING_Z_CLIP, _ROLLING_Z_CLIP)
        col = z.fillna(0.0).values
        # today 用序列尾部的滚动统计(col 已含 signal-date 行时即最后一行的窗口)
        mu_t = float(mu_r.iloc[-1]) if np.isfinite(mu_r.iloc[-1]) else 0.0
        sd_t = float(sd_r.iloc[-1]) if np.isfinite(sd_r.iloc[-1]) and sd_r.iloc[-1] > 0 else 1.0
        today_val = float(np.clip((today_val - mu_t) / sd_t, -_ROLLING_Z_CLIP, _ROLLING_Z_CLIP))
        return col, today_val

    # Z-score normalize (static, 平稳特征)
    mu = float(col.mean())
    sd = float(col.std())
    if sd > 0:
        col = (col - mu) / sd
        today_val = (today_val - mu) / sd

    return col, today_val


class EuclideanMethod(SimilarityMethod):
    """
    Gaussian kernel on Euclidean distance.

    Smart normalization (normalize=True):
      - _z features: untouched (already standardized by MacroStateStore)
      - Large-range features (vix, move, etc.): log-transform → z-score
      - Other raw features: plain z-score
    """

    def __init__(self, normalize: bool = True, sigma_scale: float = 1.0):
        self.normalize = normalize
        # 核带宽系数: σ = sigma_scale × median(dists)。1.0=旧行为;
        # <1 收紧核让"相似"真正挑人(2026-07-31 ESS 修复,MCPS 路径用)
        self.sigma_scale = sigma_scale

    def compute_weights(
        self,
        macro_matrix: np.ndarray,
        today_vector: np.ndarray,
        feature_names: List[str],
    ) -> np.ndarray:
        mat = macro_matrix.copy()
        today = today_vector.copy()

        if self.normalize:
            for i, f in enumerate(feature_names):
                mat[:, i], today[i] = _smart_normalize_column(
                    mat[:, i], today[i], f
                )

        diffs = mat - today
        dists = np.sqrt((diffs ** 2).sum(axis=1))

        sigma = max(float(np.median(dists)) * self.sigma_scale, 1e-3)
        weights = np.exp(-(dists ** 2) / (2.0 * sigma ** 2))
        return weights


# ═══════════════════════════════════════════════════════════════════════════
#  Method 2: Autoencoder latent space
# ═══════════════════════════════════════════════════════════════════════════

class AutoencoderMethod(SimilarityMethod):
    """
    Train a small autoencoder on ALL available macro indicators (20-30 dims),
    compress to latent space (8-16 dims), compute Gaussian kernel distance
    in latent space.

    Architecture:
        Input (n_features) → 32 → latent_dim → 32 → Output (n_features)

    Training is done lazily on first call with the IS macro data.
    The autoencoder learns which macro dimensions co-move and compresses
    them, automatically discovering the most informative representation.

    Advantages over raw Euclidean:
      - Handles correlated features (VIX ↔ fin_stress ↔ baa_spread)
      - Learns non-linear relationships
      - Scale-invariant (built-in normalization)
    """

    def __init__(
        self,
        latent_dim: int = 12,
        epochs: int = 100,
        lr: float = 0.003,
        seed: int = 42,
        sigma_scale: float = 1.0,
    ):
        self.latent_dim = latent_dim
        self.epochs = epochs
        self.lr = lr
        self.seed = seed
        self.sigma_scale = sigma_scale   # 核带宽系数(同 EuclideanMethod)
        self._encoder = None
        self._scaler_mean = None
        self._scaler_std = None
        self._trained = False
        self._is_pca = False             # 2026-08-16: 未训练时缺属性曾致
        self._feature_names = None       # AttributeError(AISS weekly 降级)

    @staticmethod
    def regime_prepass(mat: np.ndarray, today: 'np.ndarray | None',
                       feature_names: List[str]):
        """非平稳水平量的 rolling-z 预变换(2026-07-31,norm_version=2)。
        训练/嵌入/today 三处必须用同一变换 — MacroSimilarity 与本类共用此入口。
        today=None 时只变换矩阵(全历史嵌入场景)。原地修改并返回 (mat, today)。"""
        for i, f in enumerate(feature_names):
            if f in _ROLLING_Z_FEATURES:
                tv = float(today[i]) if today is not None else float(mat[-1, i])
                mat[:, i], tv_n = _smart_normalize_column(mat[:, i], tv, f)
                if today is not None:
                    today[i] = tv_n
        return mat, today

    def _build_and_train(self, macro_matrix: np.ndarray, feature_names: List[str]) -> None:
        """Build and train the autoencoder on the provided macro data.

        期望输入: 已经过 regime_prepass 的矩阵(7个非平稳特征已是 rolling-z 空间,
        本函数的 log 循环会跳过它们防止对 z 值取对数)。"""
        # Smart normalization: log-transform large-range features, skip _z features
        self._feature_names = feature_names
        mat = macro_matrix.copy()
        for i, f in enumerate(feature_names):
            if f in _LOG_TRANSFORM_FEATURES and f not in _ROLLING_Z_FEATURES:
                floor = max(float(mat[:, i].min()), 0.01)
                mat[:, i] = np.log(np.maximum(mat[:, i], floor))

        try:
            import torch
            import torch.nn as nn
        except ImportError:
            logger.warning("AutoencoderMethod: torch not available, falling back to PCA")
            self._use_pca_fallback(mat, feature_names)
            return

        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

        n_samples, n_features = mat.shape

        # Standardize (only non-_z features need it, but autoencoder handles all uniformly
        # since log-transform already compressed large ranges)
        self._scaler_mean = mat.mean(axis=0)
        self._scaler_std = mat.std(axis=0)
        self._scaler_std[self._scaler_std == 0] = 1.0
        X = (mat - self._scaler_mean) / self._scaler_std

        # Build autoencoder
        hidden = min(32, n_features * 2)
        encoder = nn.Sequential(
            nn.Linear(n_features, hidden),
            nn.ReLU(),
            nn.Linear(hidden, self.latent_dim),
        )
        decoder = nn.Sequential(
            nn.Linear(self.latent_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, n_features),
        )
        autoencoder = nn.Sequential(encoder, decoder)

        optimizer = torch.optim.Adam(autoencoder.parameters(), lr=self.lr)
        loss_fn = nn.MSELoss()

        X_tensor = torch.FloatTensor(X)

        # Train
        autoencoder.train()
        for epoch in range(self.epochs):
            optimizer.zero_grad()
            recon = autoencoder(X_tensor)
            loss = loss_fn(recon, X_tensor)
            loss.backward()
            optimizer.step()

        autoencoder.eval()
        self._encoder = encoder
        self._autoencoder = autoencoder
        self._trained = True
        self._is_pca = False

        final_loss = float(loss.item())
        logger.info(f"AutoencoderMethod: trained on {n_samples} days × {n_features} features "
                    f"→ latent_dim={self.latent_dim}, final_loss={final_loss:.6f}")

    def _use_pca_fallback(self, macro_matrix: np.ndarray, feature_names: List[str]) -> None:
        """PCA fallback when torch is not available."""
        from sklearn.decomposition import PCA

        self._feature_names = feature_names
        self._scaler_mean = macro_matrix.mean(axis=0)
        self._scaler_std = macro_matrix.std(axis=0)
        self._scaler_std[self._scaler_std == 0] = 1.0

        X = (macro_matrix - self._scaler_mean) / self._scaler_std
        n_components = min(self.latent_dim, X.shape[1], X.shape[0])

        self._pca = PCA(n_components=n_components)
        self._pca.fit(X)
        self._trained = True
        self._is_pca = True

        explained = self._pca.explained_variance_ratio_.sum() * 100
        logger.info(f"AutoencoderMethod (PCA fallback): {n_components} components, "
                    f"{explained:.1f}% variance explained")

    def _encode(self, X_normalized: np.ndarray) -> np.ndarray:
        """Encode data to latent space."""
        if self._is_pca:
            return self._pca.transform(X_normalized)
        else:
            import torch
            with torch.no_grad():
                return self._encoder(torch.FloatTensor(X_normalized)).numpy()

    def compute_weights(
        self,
        macro_matrix: np.ndarray,
        today_vector: np.ndarray,
        feature_names: List[str],
    ) -> np.ndarray:
        # ── 守卫(2026-07-31): 输入维度 ≤ latent_dim 时 AE 零压缩,是昂贵的
        # 伪恒等映射 → 明确降级为 Euclidean 并大声告警。正常路径(23维)不触发;
        # 数据质量应由调用方保证(喂满特征),此守卫只是最后防线。
        if macro_matrix.shape[1] <= self.latent_dim:
            logger.warning(
                f"AutoencoderMethod: n_features={macro_matrix.shape[1]} <= "
                f"latent_dim={self.latent_dim} — no compression possible; "
                f"delegating to EuclideanMethod. 调用方应喂满 AUTOENCODER_FEATURES!"
            )
            return EuclideanMethod(
                normalize=True, sigma_scale=self.sigma_scale
            ).compute_weights(macro_matrix, today_vector, feature_names)

        mat_norm, today_norm = self._prep_transform(macro_matrix, today_vector,
                                                    feature_names)

        # Encode to latent space
        latent_mat = self._encode(mat_norm)
        latent_today = self._encode(today_norm.reshape(1, -1)).flatten()

        # Gaussian kernel in latent space
        diffs = latent_mat - latent_today
        dists = np.sqrt((diffs ** 2).sum(axis=1))
        sigma = max(float(np.median(dists)) * self.sigma_scale, 1e-3)
        weights = np.exp(-(dists ** 2) / (2.0 * sigma ** 2))
        return weights

    def _prep_transform(self, macro_matrix: np.ndarray, today_vector: np.ndarray,
                        feature_names: List[str]):
        """完整预处理链(compute_weights 原逐行抽取,行为等价):
        prepass(rolling-z)→ (惰性训练) → log 变换 → 训练期 scaler 标准化。
        已训练(含 load 恢复)时特征序必须与训练时一致 —— 不一致直接 raise,
        绝不静默错位编码。"""
        mat = macro_matrix.copy()
        today = today_vector.copy()
        mat, today = self.regime_prepass(mat, today, feature_names)

        if not self._trained:
            self._build_and_train(mat, feature_names)
        elif (self._feature_names is not None
              and list(feature_names) != list(self._feature_names)):
            raise ValueError(
                f"AutoencoderMethod: feature mismatch — trained on "
                f"{self._feature_names}, got {list(feature_names)}")

        # Apply same log-transform as training(rolling-z 特征已在 z 空间,跳过)
        for i, f in enumerate(feature_names):
            if f in _LOG_TRANSFORM_FEATURES and f not in _ROLLING_Z_FEATURES:
                floor = max(float(mat[:, i].min()), 0.01)
                mat[:, i] = np.log(np.maximum(mat[:, i], floor))
                today[i] = np.log(max(today[i], floor))

        # Normalize using training stats
        mat_norm = (mat - self._scaler_mean) / self._scaler_std
        today_norm = (today - self._scaler_mean) / self._scaler_std
        return mat_norm, today_norm

    def latent_of(self, macro_matrix: np.ndarray, today_vector: np.ndarray,
                  feature_names: List[str]):
        """→ (latent_mat, latent_today) —— 与 compute_weights **完全同一条**
        预处理链后编码(2026-08-16: smart_select/AISSBatchRun 曾把生向量直塞
        _encode,跳过 prepass/log/标准化,latent 是尺度噪音;公共 API 堵死)。
        维度 ≤ latent_dim(无压缩可言)→ (None, None),调用方按 unavailable 处理。"""
        if macro_matrix.shape[1] <= self.latent_dim:
            logger.warning(
                f"AutoencoderMethod.latent_of: n_features={macro_matrix.shape[1]}"
                f" <= latent_dim={self.latent_dim} — 不出数")
            return None, None
        mat_norm, today_norm = self._prep_transform(macro_matrix, today_vector,
                                                    feature_names)
        return (self._encode(mat_norm),
                self._encode(today_norm.reshape(1, -1)).flatten())

    def save(self, path) -> None:
        """训练后的编码器持久化 —— 离线重建(centroids)与每日 serving 共用
        **同一 latent 基底**;每日惰性重训会随数据增长漂移基底,与固定
        centroids 比距离没有意义(2026-08-16 AISS Layer-1 真修)。"""
        import torch
        if not self._trained or self._is_pca or self._encoder is None:
            raise RuntimeError("save: encoder 未训练或处于 PCA 回退态,拒绝落盘")
        torch.save({"state_dict": self._encoder.state_dict(),
                    "meta": {"feature_names": list(self._feature_names),
                             "scaler_mean": np.asarray(self._scaler_mean).tolist(),
                             "scaler_std": np.asarray(self._scaler_std).tolist(),
                             "latent_dim": self.latent_dim,
                             "seed": self.seed}}, str(path))

    @classmethod
    def load(cls, path) -> "AutoencoderMethod":
        import torch
        import torch.nn as nn
        blob = torch.load(str(path), map_location="cpu", weights_only=False)
        meta = blob["meta"]
        m = cls(latent_dim=int(meta["latent_dim"]), seed=int(meta.get("seed", 42)))
        n_features = len(meta["feature_names"])
        hidden = min(32, n_features * 2)
        enc = nn.Sequential(nn.Linear(n_features, hidden), nn.ReLU(),
                            nn.Linear(hidden, m.latent_dim))
        enc.load_state_dict(blob["state_dict"])
        enc.eval()
        m._encoder = enc
        m._feature_names = list(meta["feature_names"])
        m._scaler_mean = np.asarray(meta["scaler_mean"], dtype=float)
        m._scaler_std = np.asarray(meta["scaler_std"], dtype=float)
        m._trained, m._is_pca = True, False
        return m


# ═══════════════════════════════════════════════════════════════════════════
#  Facade: SimilarityEngine
# ═══════════════════════════════════════════════════════════════════════════

# All available macro features for the autoencoder (broader than SIMILARITY_FEATURES)
AUTOENCODER_FEATURES: List[str] = [
    # Z-scored indicators (already standardized)
    'fin_stress_z', 'baa_spread_z', 'xlk_spy_z', 'vix_z',
    'move_z', 'yield_curve_z', 'iwm_spy_z', 'qqq_spy_z',
    # Raw indicators (autoencoder handles scaling internally)
    'breakeven_10y', 'consumer_sent', 'effr', 'effr_yoy',
    'unrate', 'vix', 'yield_curve', 'nfci',
    'gld_spy_corr20', 'spy_20d', 'tnx',
    # Momentum/volatility
    'nvda_20d', 'soxx_20d', 'uso_20d', 'uup_20d',
]


class SimilarityEngine:
    """
    Pluggable macro similarity engine.

    Usage:
        engine = SimilarityEngine(method="euclidean")
        weights = engine.compute_weights(macro_df, today_vec, feature_names)

        engine = SimilarityEngine(method="autoencoder")
        weights = engine.compute_weights(macro_df, today_vec)
        # autoencoder uses AUTOENCODER_FEATURES (23 dims) by default

    Parameters
    ----------
    method : str
        "euclidean" — current production (6 SIMILARITY_FEATURES)
        "autoencoder" — learned latent space (23 AUTOENCODER_FEATURES)
        "ensemble" — average weights from both methods
    kwargs : passed to the underlying method constructor
    """

    _METHODS = {
        "euclidean": EuclideanMethod,
        "autoencoder": AutoencoderMethod,
    }

    def __init__(self, method: str = "euclidean", **kwargs):
        self.method_name = method

        _euc_keys = {'normalize', 'sigma_scale'}
        _ae_keys = {'latent_dim', 'epochs', 'lr', 'seed', 'sigma_scale'}

        if method == "ensemble":
            self._methods = [
                EuclideanMethod(**{k: v for k, v in kwargs.items() if k in _euc_keys}),
                AutoencoderMethod(**{k: v for k, v in kwargs.items() if k in _ae_keys}),
            ]
            self._is_ensemble = True
        elif method == "euclidean":
            self._methods = [EuclideanMethod(**{k: v for k, v in kwargs.items() if k in _euc_keys})]
            self._is_ensemble = False
        elif method == "autoencoder":
            self._methods = [AutoencoderMethod(**{k: v for k, v in kwargs.items() if k in _ae_keys})]
            self._is_ensemble = False
        else:
            raise ValueError(f"Unknown method '{method}'. Available: "
                             f"{list(self._METHODS.keys()) + ['ensemble']}")

    def compute_weights(
        self,
        macro_df: pd.DataFrame,
        today_vec: dict,
        feature_names: Optional[List[str]] = None,
    ) -> Tuple[np.ndarray, pd.Index]:
        """
        Compute similarity weights for each historical day.

        Parameters
        ----------
        macro_df : pd.DataFrame — daily macro state data
        today_vec : dict — today's macro state {feature: float}
        feature_names : list[str] — features to use (default depends on method)

        Returns
        -------
        weights : (T,) np.ndarray — similarity weights per day
        index : pd.Index — aligned DatetimeIndex
        """
        if self._is_ensemble:
            return self._compute_ensemble(macro_df, today_vec, feature_names)

        method = self._methods[0]

        if feature_names is None:
            if isinstance(method, AutoencoderMethod):
                feature_names = AUTOENCODER_FEATURES
            else:
                from MacroStateStore import SIMILARITY_FEATURES
                feature_names = SIMILARITY_FEATURES

        avail = [f for f in feature_names if f in macro_df.columns]
        if not avail:
            return np.array([]), pd.DatetimeIndex([])

        sub = macro_df[avail].dropna(how="any")
        if sub.empty:
            return np.array([]), pd.DatetimeIndex([])

        today_v = [today_vec.get(f) for f in avail]
        n_missing = sum(1 for v in today_v
                        if v is None or (isinstance(v, float) and v != v))
        if n_missing == len(today_v):
            # ALL features missing → cannot compute similarity
            return np.array([]), pd.DatetimeIndex([])
        if n_missing > 0:
            # Partial missing → 子空间降级(2026-07-31,用户指定行为): 剔除缺失
            # 特征列,用可用维度继续计算(如 23 缺 3 → 20 维),并大声报警。
            # 不用中位数填充——填充=虚构今日值,子空间=诚实地只比可比的维度。
            _dropped = [f for f, v in zip(avail, today_v)
                        if v is None or (isinstance(v, float) and v != v)]
            logger.warning(
                f"SimilarityEngine: today_vec 缺 {n_missing}/{len(avail)} 特征 "
                f"{_dropped} — 降维至 {len(avail)-n_missing} 维可用子空间继续"
                f"(应修复上游数据!)"
            )
            keep = [i for i, v in enumerate(today_v)
                    if not (v is None or (isinstance(v, float) and v != v))]
            avail = [avail[i] for i in keep]
            today_v = [today_v[i] for i in keep]
            sub = sub[avail]

        today_arr = np.array([float(v) for v in today_v])
        weights = method.compute_weights(sub.values, today_arr, avail)
        return weights, sub.index

    def _compute_ensemble(
        self,
        macro_df: pd.DataFrame,
        today_vec: dict,
        feature_names: Optional[List[str]],
    ) -> Tuple[np.ndarray, pd.Index]:
        """Average weights from euclidean and autoencoder methods."""
        from MacroStateStore import SIMILARITY_FEATURES

        # Euclidean uses SIMILARITY_FEATURES
        euc_method = self._methods[0]
        euc_avail = [f for f in SIMILARITY_FEATURES if f in macro_df.columns]
        euc_sub = macro_df[euc_avail].dropna(how="any")

        # Autoencoder uses AUTOENCODER_FEATURES
        ae_method = self._methods[1]
        ae_avail = [f for f in AUTOENCODER_FEATURES if f in macro_df.columns]
        ae_sub = macro_df[ae_avail].dropna(how="any")

        # Common index
        common_idx = euc_sub.index.intersection(ae_sub.index)
        if common_idx.empty:
            return np.array([]), pd.DatetimeIndex([])

        # Euclidean weights
        euc_today = np.array([float(today_vec.get(f, euc_sub[f].median())) for f in euc_avail])
        euc_w = euc_method.compute_weights(
            euc_sub.reindex(common_idx).values, euc_today, euc_avail)

        # Autoencoder weights
        ae_today = np.array([float(today_vec.get(f, ae_sub[f].median())) for f in ae_avail])
        ae_w = ae_method.compute_weights(
            ae_sub.reindex(common_idx).values, ae_today, ae_avail)

        # Normalize each to [0,1] range then average
        euc_n = euc_w / max(euc_w.max(), 1e-10)
        ae_n = ae_w / max(ae_w.max(), 1e-10)
        combined = (euc_n + ae_n) / 2.0

        return combined, common_idx
