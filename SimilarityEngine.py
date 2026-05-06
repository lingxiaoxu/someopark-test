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


def _smart_normalize_column(
    col: np.ndarray,
    today_val: float,
    feature_name: str,
) -> tuple:
    """
    Smart normalization for a single feature column + today's value.

    Rules:
      - Feature ends with '_z': already z-scored by MacroStateStore → skip
      - Feature in _LOG_TRANSFORM_FEATURES: log-transform first, then z-score
      - Otherwise: plain z-score normalization

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

    # Z-score normalize
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

    def __init__(self, normalize: bool = True):
        self.normalize = normalize

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

        sigma = max(float(np.median(dists)), 1e-3)
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
    ):
        self.latent_dim = latent_dim
        self.epochs = epochs
        self.lr = lr
        self.seed = seed
        self._encoder = None
        self._scaler_mean = None
        self._scaler_std = None
        self._trained = False

    def _build_and_train(self, macro_matrix: np.ndarray, feature_names: List[str]) -> None:
        """Build and train the autoencoder on the provided macro data."""
        # Smart normalization: log-transform large-range features, skip _z features
        self._feature_names = feature_names
        mat = macro_matrix.copy()
        for i, f in enumerate(feature_names):
            if f in _LOG_TRANSFORM_FEATURES:
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
        if not self._trained:
            self._build_and_train(macro_matrix, feature_names)

        # Apply same log-transform as training
        mat = macro_matrix.copy()
        today = today_vector.copy()
        for i, f in enumerate(feature_names):
            if f in _LOG_TRANSFORM_FEATURES:
                floor = max(float(mat[:, i].min()), 0.01)
                mat[:, i] = np.log(np.maximum(mat[:, i], floor))
                today[i] = np.log(max(today[i], floor))

        # Normalize using training stats
        mat_norm = (mat - self._scaler_mean) / self._scaler_std
        today_norm = (today - self._scaler_mean) / self._scaler_std

        # Encode to latent space
        latent_mat = self._encode(mat_norm)
        latent_today = self._encode(today_norm.reshape(1, -1)).flatten()

        # Gaussian kernel in latent space
        diffs = latent_mat - latent_today
        dists = np.sqrt((diffs ** 2).sum(axis=1))
        sigma = max(float(np.median(dists)), 1e-3)
        weights = np.exp(-(dists ** 2) / (2.0 * sigma ** 2))
        return weights


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

        _euc_keys = {'normalize'}
        _ae_keys = {'latent_dim', 'epochs', 'lr', 'seed'}

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
            # Partial missing → fill with column median (graceful degradation)
            for i, (f, v) in enumerate(zip(avail, today_v)):
                if v is None or (isinstance(v, float) and v != v):
                    today_v[i] = float(sub[f].median())

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
