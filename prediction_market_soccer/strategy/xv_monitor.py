"""Model-vs-market divergence monitor — club edition (plan §2.2 xv_monitor row).

Two products, mirroring the WC module:
  * ``compare_matches``  → xv_matches.json — per-match 3-way divergence (model vs
    each venue's de-vig), DERIVED from the freshly-built upcoming.json rows so
    the Divergence view and the match cards can never disagree (single fetch).
  * ``compare_champion`` → xv_champion.json — per-competition title divergence:
    model p_champion (soccer_model.json) vs Kalshi champion ¢ de-vigged N-way
    (Shin — longshot-aware, kept from the WC "48-way exclusive" discipline).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active


def _load_output(name: str) -> dict | None:
    p = CONFIG.paths.output / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# A quoted market only counts as a reference price if it is actually TRADEABLE.
# An untouched club market sits at bid 3¢ / ask 81¢ on all three sides; de-vigging
# that returns 33/33/33, which then reads as a huge "divergence" from any real
# model and topped the board (Lens v Lorient at 0.33). Anything wider than this
# on any side is an empty book, not a price.
_MAX_SPREAD = 0.20


def _liquid_devig(venue: dict | None) -> dict | None:
    """The venue's de-vig, or None when its book is too wide to be a real price."""
    if not venue or not venue.get("devig"):
        return None
    for side in ("home", "draw", "away"):
        q = venue.get(side) or {}
        ask, bid = q.get("ask"), q.get("bid")
        if ask is None or bid is None or (ask - bid) > _MAX_SPREAD:
            return None
    return venue["devig"]


def compare_matches(limit: int = 12, conn=None) -> dict:
    """Divergence rows derived from upcoming.json (model vs venue de-vig)."""
    up = _load_output("upcoming.json") or {}
    rows = []
    for m in (up.get("matches") or []):
        model = m.get("model")
        if not model or m.get("tentative"):
            continue
        kd = _liquid_devig(m.get("kalshi"))
        pdv = _liquid_devig(m.get("poly_us"))
        bd = m.get("book_devig")
        ref = kd or pdv or bd
        if not ref:
            continue
        div = {s: round(model[s] - ref[s], 4) for s in ("home", "draw", "away")}
        worst = max(div, key=lambda s: abs(div[s]))
        rows.append({
            "fixture_id": m["fixture_id"], "league": m.get("league"),
            "kickoff": m.get("kickoff"), "et": m.get("et"), "round": m.get("round"),
            "home": m["home"], "away": m["away"],
            "model": {s: model[s] for s in ("home", "draw", "away")},
            "kalshi_devig": kd, "poly_devig": pdv, "book_devig": bd,
            "divergence": div, "max_side": worst, "max_abs": round(abs(div[worst]), 4),
            "ref_source": "kalshi" if kd else ("poly_us" if pdv else "book"),
        })
    rows.sort(key=lambda r: -r["max_abs"])
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows[:limit]),
            "note_key": "notes.xv",
            "note": "model − de-vigged market per side; derived from upcoming.json "
                    "(single quote fetch, views can never disagree).",
            "matches": rows[:limit]}


def _shin_devig_cents(cents: dict[str, float]) -> dict[str, float]:
    from prediction_market_soccer.strategy.devig import devig
    keys = [k for k, v in cents.items() if v is not None and v > 0]
    if len(keys) < 3:
        return {}
    asks = [cents[k] / 100.0 for k in keys]
    try:
        p = devig(asks, method="shin")
    except Exception:
        p = devig(asks, method="multiplicative")
    return {k: float(x) for k, x in zip(keys, p)}


def compare_champion(n_sims: int | None = None) -> dict:
    """Per-comp champion divergence; writes xv_champion.json itself (WC behavior)."""
    model_doc = _load_output("soccer_model.json") or {}
    by_league = {lg["league"]: lg for lg in model_doc.get("leagues", [])}
    try:
        from prediction_market_soccer.venues.champion_prices import season_cents
    except Exception:
        season_cents = None

    leagues = []
    for comp in active():
        lg = by_league.get(comp.key)
        if not lg:
            continue
        cents = {}
        if season_cents is not None and comp.kalshi.get("champion"):
            try:
                cents = season_cents(comp.key, "champion")
            except Exception as e:  # noqa: BLE001
                print(f"[xv_champion:{comp.key}] cents skipped ({e})")
        devigged = _shin_devig_cents(cents)
        rows = []
        for so in lg.get("season_odds", []):
            pm = so.get("p_champion")
            if pm is None:
                continue
            mk = devigged.get(so["club_id"])
            rows.append({
                "club_id": so["club_id"], "name": so["name"], "zh": so.get("zh", ""),
                "p_model": round(pm, 5),
                "kalshi_c": cents.get(so["club_id"]),
                "p_kalshi_devig": round(mk, 5) if mk is not None else None,
                "divergence": round(pm - mk, 5) if mk is not None else None,
            })
        rows.sort(key=lambda r: -(r["p_model"] or 0))
        if rows:
            leagues.append({"league": comp.key, "name": comp.name, "zh": comp.zh,
                            "series": comp.kalshi.get("champion"), "rows": rows})

    doc = {"as_of": datetime.now(timezone.utc).isoformat(),
           "note_key": "notes.xv",
           "note": "model p_champion vs Kalshi champion book (Shin de-vig, longshot-aware).",
           "leagues": leagues}
    CONFIG.paths.ensure()
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / "xv_champion.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    return doc


def write_report(*args, **kwargs) -> None:
    """WC back-compat no-op (hourly_job legacy caller)."""


if __name__ == "__main__":
    doc = compare_matches()
    print(f"xv_matches: {doc['n']} rows; top divergence:",
          [(m['home']['name'], m['max_side'], m['max_abs']) for m in doc['matches'][:3]])
    ch = compare_champion()
    print(f"xv_champion: {len(ch['leagues'])} leagues")
