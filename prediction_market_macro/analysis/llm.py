"""analysis/llm.py — nemotron on box A, degradable (PLAN §10).

`Nemo.ask_json(system, user, schema, timeout)`: OpenAI-compatible chat endpoint at
settings.ollama_base, **global threading.Lock serial** (the 120B model must never see
parallel requests — project discipline), one retry, then None. A None NEVER blocks a
decision — every caller treats LLM output as optional annotation.

Use cases wired here: fomc_statement_diff (hawk/dove score), news_risk_tags (layoffs /
energy-shock / fed_speak tagging over the news table), weekly_narrative (weekly report
prose). Everything lands in llm_annotations (auditable, can be switched off).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

_LOCK = threading.Lock()          # module-global: one in-flight nemo call, ever
_THINK = re.compile(r"<think>.*?</think>", re.S)


class Nemo:
    def __init__(self, settings):
        self.base = settings.ollama_base.rstrip("/")
        self.model = settings.ollama_model

    def _chat(self, system: str, user: str, timeout: int) -> str | None:
        r = requests.post(
            f"{self.base}/chat/completions",
            json={"model": self.model, "stream": False,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": 0.2, "max_tokens": 1200},
            timeout=timeout)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
        return _THINK.sub("", txt).strip()

    def ask_json(self, system: str, user: str, schema_hint: str,
                 timeout: int = 180) -> dict | None:
        """schema_hint: prose description of the exact JSON keys wanted. One retry,
        then None (degradable)."""
        sys_full = (f"{system}\nRespond with ONLY a JSON object, no prose, keys: "
                    f"{schema_hint}")
        with _LOCK:
            for _ in range(2):
                try:
                    txt = self._chat(sys_full, user, timeout)
                    m = re.search(r"\{.*\}", txt, re.S)
                    if m:
                        return json.loads(m.group(0))
                except Exception:                                  # noqa: BLE001
                    continue
        return None

    def ask_text(self, system: str, user: str, timeout: int = 240) -> str | None:
        with _LOCK:
            for _ in range(2):
                try:
                    return self._chat(system, user, timeout)
                except Exception:                                  # noqa: BLE001
                    continue
        return None


def _annotate(conn, kind: str, model: str, payload_in: str, out: dict | str | None) -> None:
    conn.execute(
        "INSERT INTO llm_annotations(ts, kind, model, input_hash, output_json)"
        " VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), kind, model,
         hashlib.sha256(payload_in.encode()).hexdigest()[:16],
         json.dumps(out, ensure_ascii=False) if out is not None else None))
    conn.commit()


def _cached(conn, kind: str, payload_in: str) -> dict | None:
    h = hashlib.sha256(payload_in.encode()).hexdigest()[:16]
    r = conn.execute(
        "SELECT output_json FROM llm_annotations WHERE kind=? AND input_hash=?"
        " AND output_json IS NOT NULL ORDER BY id DESC LIMIT 1", (kind, h)).fetchone()
    return json.loads(r["output_json"]) if r else None


def fomc_statement_diff(conn, settings, old_text: str, new_text: str) -> dict | None:
    """Hawk/dove read of a statement change. Returns {hawk_score, key_changes, rationale}
    (hawk_score in [-1, 1]: -1 max dovish, +1 max hawkish). Cached by input hash."""
    payload = f"OLD:\n{old_text}\n\nNEW:\n{new_text}"
    hit = _cached(conn, "fomc_diff", payload)
    if hit is not None:
        return hit
    out = Nemo(settings).ask_json(
        "You are a Fed statement analyst. Compare the two FOMC statements.",
        payload,
        '"hawk_score" (float -1..1), "key_changes" (list of strings), "rationale" (string)')
    _annotate(conn, "fomc_diff", settings.ollama_model, payload, out)
    return out


def news_risk_tags(conn, settings, lookback_hours: int = 36,
                   max_items: int = 40) -> dict | None:
    """Tag recent headlines with macro-shock categories that feed claims/energy shock
    terms: mass_layoffs, energy_shock, fed_speak, inflation_surprise, none. Cached per
    headline-set hash. Returns {tags: [{title, tag, direction}], summary}."""
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    rows = conn.execute(
        "SELECT title, publisher FROM news WHERE published_utc>=? ORDER BY published_utc"
        " DESC LIMIT ?", (since, max_items)).fetchall()
    if not rows:
        return None
    payload = "\n".join(f"- {r['title']} ({r['publisher']})" for r in rows)
    hit = _cached(conn, "news_tags", payload)
    if hit is not None:
        return hit
    out = Nemo(settings).ask_json(
        "You tag macro-market-relevant news. Categories: mass_layoffs, energy_shock,"
        " fed_speak, inflation_surprise, none. direction: up_risk|down_risk|neutral"
        " for the affected macro print (claims up = up_risk etc.).",
        payload,
        '"tags" (list of {"title","tag","direction"}), "summary" (string, <=60 words)')
    _annotate(conn, "news_tags", settings.ollama_model, payload, out)
    return out


def weekly_narrative(conn, settings) -> str | None:
    """Weekly report prose from the week's decisions, marks and alerts. Plain text."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    dec = conn.execute(
        "SELECT series, period, kind, fair, ask, net_edge, size_usd FROM decisions"
        " WHERE ts_utc>=? ORDER BY ts_utc", (since,)).fetchall()
    alerts = conn.execute(
        "SELECT level, source, message FROM alerts WHERE ts>=? AND level='error'"
        " ORDER BY ts DESC LIMIT 20", (since,)).fetchall()
    payload = json.dumps({
        "decisions": [dict(r) for r in dec][:80],
        "error_alerts": [dict(r) for r in alerts]}, ensure_ascii=False, default=str)
    txt = Nemo(settings).ask_text(
        "You write the weekly narrative for a macro prediction-market paper-trading desk."
        " 3 short paragraphs: what we traded and why, how the models behaved, what to"
        " watch next week. Factual, no hype, cite series tickers.",
        payload)
    _annotate(conn, "weekly_narrative", settings.ollama_model, payload,
              {"text": txt} if txt else None)
    return txt


# ── wiring: the two previously-dead use cases land here (PLAN_EXTENSION #18/#23) ──

_TAG_FAMILY = {"mass_layoffs": ("labor",), "energy_shock": ("energy", "inflation"),
               "fed_speak": ("fed",), "inflation_surprise": ("inflation",)}


def statement_risk_pass(conn, settings) -> dict | None:
    """Diff the two latest stored FOMC statements (ingest/fed_text) → hawk/dove score
    annotation + an alert when the language shift is large. Cached by text hash, so
    this is a no-op except in the days after a new statement lands."""
    from prediction_market_macro.ingest.fed_text import latest_two
    pair = latest_two(conn)
    if pair is None:
        return None
    new, old = pair
    out = fomc_statement_diff(conn, settings, old["text"][:6000], new["text"][:6000])
    if out and abs(float(out.get("hawk_score") or 0.0)) >= 0.5:
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), "warn", "statement_risk",
             f"FOMC {new['period']} language shift hawk_score="
             f"{out['hawk_score']:+.2f}: {'; '.join(out.get('key_changes', [])[:3])}"))
        conn.commit()
    return out


def apply_news_flags(conn, settings, ttl_days: float = 5.0) -> int:
    """news_risk_tags → event_flags rows (§19-8). Severity = tag count in the window
    (capped 5). decide_all reads active flags and tightens gates for the family.
    Degradable: no nemo ⇒ no flags ⇒ unchanged behavior."""
    out = news_risk_tags(conn, settings)
    if not out or not out.get("tags"):
        return 0
    now = datetime.now(timezone.utc)
    counts: dict[tuple[str, str, str], int] = {}
    for t in out["tags"]:
        tag = t.get("tag")
        fams = _TAG_FAMILY.get(tag)
        if not fams:
            continue
        for fam in fams:
            k = (tag, t.get("direction") or "neutral", fam)
            counts[k] = counts.get(k, 0) + 1
    n = 0
    for (tag, direction, fam), c in counts.items():
        dup = conn.execute(
            "SELECT 1 FROM event_flags WHERE tag=? AND family=? AND expires_ts>?",
            (tag, fam, now.isoformat())).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO event_flags(ts, tag, direction, family, severity, expires_ts)"
            " VALUES(?,?,?,?,?,?)",
            (now.isoformat(), tag, direction, fam, min(c, 5),
             (now + timedelta(days=ttl_days)).isoformat()))
        n += 1
    conn.commit()
    return n


def active_flags(conn, family: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT tag, direction, severity FROM event_flags WHERE family=?"
        " AND expires_ts>?", (family, now)).fetchall()
    return [dict(r) for r in rows]
