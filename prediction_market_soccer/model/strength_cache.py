"""On-disk strength-model cache (club edition performance layer).

The WC module refit its 48-team model on every export call — cheap for one
tournament, but the soccer live loop is a FRESH PROCESS every 60s across 12
competitions, and a full per-comp reverse-fit + blends costs seconds each.
So the fitted ratings are persisted per comp (data/output/ratings_<comp>.json)
and live paths LOAD them instead of refitting:

  * ``run_model``/daily refresh always fit fresh and save;
  * exports call ``cached_strength(conn, comp)`` — load when fresh-enough
    (default 2h AND same settled-fixture count), else fit+save;
  * the settled-count key means a newly-settled match invalidates the cache
    immediately (the trigger pipeline refits within 15 min anyway).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import get
from prediction_market_soccer.model.strength import StrengthModel

_MAX_AGE_S = 2 * 3600


def _path(comp_key: str) -> Path:
    return CONFIG.paths.output / f"ratings_{comp_key}.json"


def _settled_count(conn, comp) -> int:
    r = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE league_id=? AND season=? "
        "AND status_short IN ('FT','AET','PEN')",
        (comp.api_football_id, comp.season)).fetchone()
    return int(r["n"])


def save_model(sm: StrengthModel, comp_key: str, conn) -> None:
    comp = get(comp_key)
    doc = {
        "comp": comp_key,
        "saved_at": time.time(),
        "settled": _settled_count(conn, comp),
        "base_mu": sm.base_mu, "home_adv": sm.home_adv,
        "ratings": {k: round(v, 6) for k, v in sm.ratings.items()},
        "sigma": {k: round(v, 6) for k, v in sm.sigma.items()},
    }
    CONFIG.paths.ensure()
    _path(comp_key).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def load_model(conn, comp_key: str, *, max_age_s: float = _MAX_AGE_S) -> StrengthModel | None:
    p = _path(comp_key)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    comp = get(comp_key)
    if time.time() - float(doc.get("saved_at") or 0) > max_age_s:
        return None
    if int(doc.get("settled") or -1) != _settled_count(conn, comp):
        return None   # a match settled since the fit — refit
    return StrengthModel(ratings=dict(doc["ratings"]), sigma=dict(doc.get("sigma") or {}),
                         cfg=CONFIG.model, comp=comp_key,
                         base_mu=doc.get("base_mu"), home_adv=doc.get("home_adv"))


def cached_strength(conn, comp_key: str, *, xg_form: bool = True,
                    max_age_s: float = _MAX_AGE_S) -> StrengthModel:
    """Load-or-fit one competition's live strength model (the export entry point)."""
    sm = load_model(conn, comp_key, max_age_s=max_age_s)
    if sm is not None:
        return sm
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.squad_strength import build_strength_live
    sm = build_strength_live(conn, load_prior(comp_key), league=comp_key, xg_form=xg_form)
    save_model(sm, comp_key, conn)
    return sm


class CompositeStrength:
    """StrengthModel-shaped facade over PER-COMPETITION models for the in-play stack.

    The copied in-play consumers (inplay_arb & friends, 500+ lines each) call
    ``sm.pair_lambdas(i, j)`` / ``sm.ratings`` on ONE model; club ratings are
    per-league (not cross-league comparable), so this facade resolves the pair's
    competition from a fixture-derived pair→comp cache and delegates to that
    comp's cached model — zero changes inside the consumers.
    """

    def __init__(self, conn, comp_keys: list[str], pair_comp: dict[frozenset, str]):
        self._models: dict[str, StrengthModel] = {}
        for k in comp_keys:
            try:
                self._models[k] = cached_strength(conn, k)
            except Exception as e:  # noqa: BLE001
                print(f"[composite_strength] {k}: {e}")
        self._pair_comp = pair_comp
        self.ratings: dict[str, float] = {}
        self.sigma: dict[str, float] = {}
        for m in self._models.values():
            for cid, r in m.ratings.items():
                self.ratings.setdefault(cid, r)
                self.sigma.setdefault(cid, m.sigma.get(cid, 0.04))
        from prediction_market_soccer.config import CONFIG as _C
        self.cfg = _C.model
        self.host_ids = frozenset()
        self.adj = None

    def _model_for(self, i: str, j: str) -> StrengthModel | None:
        k = self._pair_comp.get(frozenset((i, j)))
        if k and k in self._models:
            return self._models[k]
        for m in self._models.values():   # fallback: any model rating both clubs
            if i in m.ratings and j in m.ratings:
                return m
        return None

    def pair_lambdas(self, i: str, j: str, **kw):
        m = self._model_for(i, j)
        if m is None:
            raise KeyError(f"no competition model rates both {i} and {j}")
        return m.pair_lambdas(i, j, **kw)


def composite_live_strength(conn) -> CompositeStrength:
    """Composite over the comps that currently have live/imminent fixtures, with a
    pair→comp cache built from today's fixture rows."""
    from prediction_market_soccer.config.leagues import active, by_api_id
    lids = {c.api_football_id: c.key for c in active()}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    pair_comp: dict[frozenset, str] = {}
    comps: set[str] = set()
    for r in conn.execute(
        "SELECT league_id, home_api_id, away_api_id FROM fixture "
        "WHERE kickoff_ts >= datetime('now','-6 hours') AND kickoff_ts <= datetime('now','+2 days')"):
        k = lids.get(r["league_id"])
        if not k:
            continue
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hi and ai:
            pair_comp[frozenset((hi, ai))] = k
            comps.add(k)
    if not comps:
        comps = set(lids.values())
    return CompositeStrength(conn, sorted(comps), pair_comp)
