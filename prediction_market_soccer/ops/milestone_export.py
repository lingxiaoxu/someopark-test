"""Recorded milestone prices alongside the published canonical strategy ledger.

Prices remain per-contract cents. Strategy direction, position size, exits and
position P&L are projections of performance_report.strategy_ledger only. This
reader never freezes a decision or reconstructs a historical strategy.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import json
from prediction_market_soccer.util.research_inputs import epoch
import sqlite3
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util.pricing import to_cents

_ORDER = {"PRE": 0, "T15": 1, "T30": 2, "HT": 3, "T60": 4, "T75": 5, "FT": 6}
_FINISHED = ("FT", "AET", "PEN")


def _mark(row) -> dict:
    """One milestone row → {milestone, minute, score, poly{¢}, devig{prob}}."""
    def c(side):  # poly per-contract ¢ for a side (ask==bid==price for history)
        return to_cents(row[f"poly_{side}_ask"])
    poly = {s: c(s) for s in ("home", "draw", "away")}
    devig = None
    if row["devig_home"] is not None:
        devig = {s: row[f"devig_{s}"] for s in ("home", "draw", "away")}
    model = None
    if row["p_model_home"] is not None:
        model = {s: row[f"p_model_{s}"] for s in ("home", "draw", "away")}
    return {
        "milestone": row["milestone"],
        "minute": row["elapsed"],
        "score": f'{row["home_goals"] if row["home_goals"] is not None else "?"}-'
                 f'{row["away_goals"] if row["away_goals"] is not None else "?"}',
        "poly_c": poly,
        "kalshi_c": {s: to_cents(row[f"kalshi_{s}_ask"]) for s in ("home", "draw", "away")},
        "devig": devig,
        "model": model,
    }


def _projection(record: dict) -> dict:
    """Compatibility fields, copied exclusively from one canonical record.

    All P&L compatibility fields are position cents, never a new price movement
    calculation. The unchanged smart_exit/exit blocks retain their ledger units.
    """
    bet = bool(record.get("bet"))
    argmax = None
    if record.get("model_pick") is not None:
        argmax = {"side": record.get("model_pick"),
                  "pick_team": record.get("model_pick_team"),
                  "entry_cents": record.get("argmax_entry_cents"),
                  "won": record.get("model_won"),
                  "advance": record.get("advance") is not None,
                  "pnl_cents": record.get("argmax_pnl_cents"),
                  "cum_pnl_cents": record.get("argmax_cum_pnl_cents"),
                  "reference_only": True, "mtm": None}
    our_bet = {"side": record.get("pick"), "entry_prob": record.get("model_prob"),
               "entry_cents": record.get("entry_cents"),
               "entry_source": record.get("entry_source"),
               "pick_team": record.get("pick_team"), "bet": bet,
               "stake_usd": record.get("stake_usd"),
               "model_pick": record.get("model_pick"), "argmax": argmax,
               "realized_pnl_cents": record.get("realized_pnl_cents"),
               "cum_pnl_cents": record.get("pre_cum_pnl_cents")}
    mtm = None
    if bet and record.get("pnl_cents") is not None:
        pnl = record["pnl_cents"]
        mtm = {"entry_c": record.get("entry_cents"), "ft_c": record.get("settle_cents"),
               "pnl_c": pnl, "won": record.get("won"),
               "path_direction": "converging" if pnl > 0 else "diverging" if pnl < 0 else "flat",
               "pnl_unit": "position_cents", "reference_only": True}
    inplay = None
    if record.get("inplay_side") is not None:
        inplay = {"side": record.get("inplay_side"),
                  "pick_team": record.get("inplay_side_team"),
                  "milestone": record.get("inplay_milestone"),
                  "entry_cents": record.get("inplay_entry_cents"),
                  "stake_usd": record.get("inplay_stake_usd"),
                  "won": record.get("inplay_won"), "edge": record.get("inplay_edge"),
                  "exit": record.get("inplay_exit"),
                  "hold_pnl_cents": record.get("inplay_hold_cents"),
                  "realized_pnl_cents": record.get("inplay_pnl_cents"),
                  "cum_pnl_cents": record.get("inplay_cum_pnl_cents"),
                  "pnl_unit": "position_cents"}
    return {"our_bet": our_bet, "mtm": mtm, "smart_exit": record.get("smart_exit"),
            "inplay": inplay, "pnl_unit": "position_cents",
            **{key: record.get(key) for key in (
                "realized_pnl_cents", "pre_cum_pnl_cents", "inplay_pnl_cents",
                "inplay_cum_pnl_cents", "combined_pnl_cents", "combined_cum_pnl_cents")}}


def _fixture_context(fx, cmap) -> dict:
    """Price-only match labels and result, with no model/strategy fallback."""
    from prediction_market_soccer.config.leagues import by_api_id
    from prediction_market_soccer.util.pricing import reg_score
    comp = by_api_id(fx["league_id"])
    try:
        teams = json.loads(fx["raw_json"] or "{}").get("teams") or {}
    except (ValueError, TypeError):
        teams = {}
    def team(side):
        api_id = fx[f"{side}_api_id"]
        identity = cmap.get(api_id)
        raw = teams.get(side) or {}
        return {"id": identity, "api_id": api_id,
                "name": raw.get("name") or identity or f"Team {api_id}", "zh": ""}
    settled = fx["status_short"] in _FINISHED and fx["home_goals"] is not None and fx["away_goals"] is not None
    result = score = None
    if settled:
        gh, ga = reg_score(fx["raw_json"], fx["home_goals"], fx["away_goals"])
        result = "home" if gh > ga else "draw" if gh == ga else "away"
        score = f"{gh}-{ga}"
        if (gh, ga) != (fx["home_goals"], fx["away_goals"]):
            score += f' (AET {fx["home_goals"]}-{fx["away_goals"]})'
    return {"fixture_id": fx["api_id"], "league": comp.key if comp else None,
            "home": team("home"), "away": team("away"), "kickoff": fx["kickoff_ts"],
            "round": fx["round"] or "", "settled": settled, "score": score,
            "result": result, "source_as_of": fx["updated_at"]}


def _build(conn, ledger) -> dict:
    from prediction_market_soccer.util.strategy_ledger import (
        StrategyLedgerUnavailable, read_strategy_ledger, validate_strategy_ledger)

    issues = []
    if ledger is None:
        try:
            ledger = read_strategy_ledger(conn)
        except StrategyLedgerUnavailable:
            issues.append({"code": "strategy_ledger_unavailable"})
    if ledger is not None:
        validate_strategy_ledger(ledger)
    version = None
    if ledger is not None and ledger.get('book_version',{}).get('version_id'):
        from prediction_market_soccer.util.frozen_strategy_store import active_version
        version = active_version(conn)
        if version['version_id'] != ledger['book_version']['version_id']:
            raise ValueError('Price export ledger differs from the active frozen book')
        if version.get('origin') == 'explicit_backtest':
            return _explicit_book_marks(conn,ledger,version)
    records = ledger["records"] if ledger is not None else []
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta")}
    fixtures = {r["api_id"]: r for r in conn.execute("SELECT * FROM fixture")}
    marks_by_id = {}
    source_stamps = []
    for row in conn.execute("SELECT * FROM milestone_snapshot"):
        marks_by_id.setdefault(row["fixture_api_id"], []).append(row)
        if row["ts"]:
            source_stamps.append(row["ts"])
    marks_by_id = {fid: [_mark(row) for row in sorted(rows, key=lambda r: _ORDER.get(r["milestone"], 9))]
                   for fid, rows in marks_by_id.items()}

    matches = []
    for ordinal, record in enumerate(records):
        fid = record["fixture_id"]
        fx = fixtures.get(fid)
        # The published row remains authoritative even if the fixture or price path
        # was removed. Never drop a recorded strategy position from this view.
        is_forward = (version is not None and ordinal >= version['baseline_count']
                      and (record.get('pre_decision_id') or record.get('inplay_decision_id')))
        marks = _forward_price_marks(conn, fid) if is_forward else marks_by_id.get(fid, [])
        row = {"fixture_id": fid, "league": record.get("league"),
               "home": {"id": record.get("home_id"), "name": record.get("home"), "zh": record.get("home_zh", "")},
               "away": {"id": record.get("away_id"), "name": record.get("away"), "zh": record.get("away_zh", "")},
               "kickoff": fx["kickoff_ts"] if fx else record.get("date"),
               "round": (fx["round"] or "") if fx else "", "settled": True,
               "score": record.get("score"), "result": record.get("result"),
               "marks": marks, "marks_status": "ok" if marks else "unavailable",
               "strategy_status": "recorded", "strategy_record": record,
               **_projection(record)}
        matches.append(row)
        if not marks:
            issues.append({"code": "milestone_prices_unavailable", "fixture_id": fid})

    canonical_ids = {record["fixture_id"] for record in records}
    price_only = []
    for fid, marks in marks_by_id.items():
        if fid in canonical_ids:
            continue
        fx = fixtures.get(fid)
        context = _fixture_context(fx, cmap) if fx else {
            "fixture_id": fid, "league": None, "home": {"name": None}, "away": {"name": None},
            "kickoff": None, "round": "", "settled": False, "score": None, "result": None}
        price_only.append({**context, "marks": marks, "marks_status": "ok",
                           "strategy_status": "not_recorded" if ledger is not None else "unavailable",
                           "strategy_record": None, "our_bet": None, "mtm": None,
                           "smart_exit": None, "inplay": None})
    price_only.sort(key=lambda row: row.get("kickoff") or "", reverse=True)
    n_by_league = {}
    for row in matches:
        if row["league"]:
            n_by_league[row["league"]] = n_by_league.get(row["league"], 0) + 1
    return {"source_as_of": max(source_stamps, default=None),
            "data_status": {"state": "unavailable" if ledger is None else "degraded" if issues else "ok", "issues": issues},
            "as_of": datetime.now(timezone.utc).isoformat(), "n": len(matches),
            "n_by_league": n_by_league, "milestones": list(_ORDER),
            "note_key": "notes.milestones", "note": "Recorded per-contract milestone prices with the same published strategy ledger as performance; strategy P&L is position-sized, before fees.",
            "strategy_ledger": ledger, "records": records, "matches": matches,
            "price_only_matches": price_only, "n_price_only": len(price_only)}


def from_candidate(ledger, candidate, *, forward_marks=None):
    """Candidate price paths only; never borrow milestone prices from the live DB."""
    from copy import deepcopy
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    from prediction_market_soccer.util.research_inputs import CandidateMarketData
    from prediction_market_soccer.util.forward_replay_certification import ForwardReplayPriceData
    if isinstance(candidate,ForwardReplayPriceData):
        return from_forward_replay(ledger,candidate,forward_marks=forward_marks)
    if isinstance(candidate, RegisteredRedecisionPriceData):
        return from_redecision(ledger, candidate, forward_marks=forward_marks)
    if not isinstance(candidate, CandidateMarketData):
        raise ValueError('A verified completed CandidateMarketData is required')
    ledger = deepcopy(validate_strategy_ledger(ledger))
    records = ledger['records']
    scope = set(candidate.manifest['fixture_ids'])
    forward_marks = forward_marks or {}
    if not {row['fixture_id'] for row in records} <= scope | set(forward_marks):
        raise ValueError('Candidate price scope does not cover this ledger')
    issues, matches = [], []
    for record in records:
        fid = record['fixture_id']
        targets = sorted({row['target_at'] for row in candidate.items if row['kind']=='quote' and row['fixture_id']==fid and row['market_kind']=='match'})
        marks = deepcopy(forward_marks.get(fid, []))
        for target in targets:
            state = candidate.state_at(fid,target)
            values = state.get('data') or {}
            minute = values.get('elapsed') if state['status']=='ok' else None
            mark = {'milestone': 'PRE' if minute == 0 else 'HT' if values.get('period')=='HT' else ('T'+str(minute) if minute is not None else None),
                    'target_at':datetime.fromtimestamp(target,timezone.utc).isoformat(),'minute':minute,
                    'score':f"{values.get('home_goals','?')}-{values.get('away_goals','?')}",
                    'poly_c':{},'kalshi_c':{},'candidate_c':{},'devig':None,'model':None,
                    'state_status':state['status'],'quote_status':{},'provenance':state['provenance']}
            for side in ('home','draw','away'):
                selected = candidate.quotes_at(fid,target,required_sides=[side])
                mark['quote_status'][side] = {'status':selected['status'],'reason':selected.get('reason')}
                if selected['status'] != 'ok':
                    continue
                quote = selected['data'][side]
                price = quote.get('ask') if candidate.manifest.get('require_observed_quotes') else quote.get('price',quote.get('reference_price'))
                venue = quote.get('venue') or (quote.get('receipt') or {}).get('provider')
                key = 'kalshi_c' if venue=='kalshi' else 'poly_c' if str(venue).startswith('poly') else 'candidate_c'
                mark[key][side] = to_cents(price)
                mark.setdefault('quote_provenance',{})[side] = selected['provenance']
            if state['status'] != 'ok' or any(q['status']!='ok' for q in mark['quote_status'].values()):
                issues.append({'code':'candidate_point_unavailable','fixture_id':fid,'target_at':mark['target_at']})
            marks.append(mark)
        if not marks:
            issues.append({'code':'candidate_prices_unavailable','fixture_id':fid})
        matches.append({'fixture_id':fid,'league':record.get('league'),
            'home':{'id':record.get('home_id'),'name':record.get('home'),'zh':record.get('home_zh','')},
            'away':{'id':record.get('away_id'),'name':record.get('away'),'zh':record.get('away_zh','')},
            'kickoff':record.get('date'),'round':record.get('stage',''),'settled':True,
            'score':record.get('score'),'result':record.get('result'),'marks':marks,
            'marks_status':'ok' if marks else 'unavailable','strategy_status':'recorded',
            'strategy_record':record,**_projection(record)})
    return {'strategy_ledger':ledger,'records':records,'matches':matches,'n':len(matches),
        'price_only_matches':[],'n_price_only':0,'as_of':ledger['as_of'],'source_as_of':None,
        'candidate_source':{'run_id':candidate.run_id,'scope_id':candidate.scope_id,
                            'input_manifest_hash':candidate.completion['input_manifest_hash'],'items_hash':candidate.completion['items_hash']},
        'data_status':{'state':'degraded' if issues else 'ok','issues':issues}}


def from_forward_replay(ledger, source, *, forward_marks=None):
    """Use the certified source's original receipts, never legacy price tables."""
    from copy import deepcopy
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    ledger=deepcopy(validate_strategy_ledger(ledger));forward_marks=forward_marks or {}
    records=ledger['records'];baseline=source.records
    if records[:len(baseline)]!=baseline:
        raise ValueError('Replay marks and registered financial baseline disagree')
    matches=[];issues=[]
    for index,record in enumerate(records):
        fid=record['fixture_id']
        if index<len(baseline):
            from prediction_market_soccer.util import paper_store
            from prediction_market_soccer.util.forward_replay_certification import _quote_objects
            for observation in paper_store.observations(source.source.conn,fid):
                _quote_objects(source.source,source.reference['root'],observation,'9999-12-31T23:59:59+00:00',{})
            marks=_forward_price_marks(source.source.conn,fid)
        elif fid in forward_marks:
            marks=deepcopy(forward_marks[fid])
        else:
            raise ValueError('Forward extension has no explicit observed price source')
        if not marks:issues.append({'code':'observed_prices_unavailable','fixture_id':fid})
        matches.append({'fixture_id':fid,'league':record.get('league'),
            'home':{'id':record.get('home_id'),'name':record.get('home'),'zh':record.get('home_zh','')},
            'away':{'id':record.get('away_id'),'name':record.get('away'),'zh':record.get('away_zh','')},
            'kickoff':record.get('date'),'round':record.get('stage',''),'settled':True,
            'score':record.get('score'),'result':record.get('result'),'marks':marks,
            'marks_status':'ok' if marks else 'unavailable','strategy_status':'recorded','strategy_record':record,**_projection(record)})
    source.source.assert_unchanged()
    return {'strategy_ledger':ledger,'records':records,'matches':matches,'n':len(matches),'price_only_matches':[],
        'n_price_only':0,'as_of':ledger['as_of'],'source_as_of':None,'candidate_source':source.reference,
        'data_status':{'state':'degraded' if issues else 'ok','issues':issues}}


class RegisteredRedecisionPriceData:
    """Read the certified and registered rendering bytes without rerunning a model."""
    def __init__(self, reference):
        from copy import deepcopy
        import hashlib
        from pathlib import Path
        from prediction_market_soccer.util.research_inputs import SourceSnapshot, _path, digest
        from prediction_market_soccer.util.redecision_certification import KIND, NAMES
        from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
        self.reference = deepcopy(reference)
        if reference.get('kind') != KIND:
            raise ValueError('Registered observed-input price source required')
        root = Path(reference['root']).resolve(strict=True)
        files = reference.get('validated_render_files') or {}
        required = set(NAMES) | {'certification.json'}
        if set(files) != required:
            raise ValueError('Registered rendering files are incomplete')
        docs = {}
        for name, item in files.items():
            path = _path(Path(item['path']), root, exists=True)
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != item['sha256']:
                raise ValueError('Registered redecision rendering file changed')
            docs[name] = json.loads(body)
        cert = docs['certification.json']
        original = {k:v for k,v in reference.items() if k != 'validated_render_files'}
        if (cert.get('kind') != KIND or cert.get('source') != original
                or cert.get('certification_id') != digest({k:v for k,v in cert.items() if k != 'certification_id'})
                or docs['input_manifest.json']['candidate_input'] != original
                or any(cert['documents'].get(name) != files[name]['sha256'] for name in NAMES)):
            raise ValueError('Registered source and certified rendering identity disagree')
        report = docs['candidate_report.json']
        ledger = validate_strategy_ledger(report['strategy_ledger'])
        if report['bet_log'] != ledger['records']:
            raise ValueError('Registered rendering records disagree with the ledger')
        for raw_path, expected in cert['raw_hashes'].items():
            path = _path(Path(raw_path), root, exists=True)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Registered original raw evidence changed')
        self.source = SourceSnapshot(Path(reference['path']), root=root, raw_archive=reference.get('raw_archive'))
        try:
            if self.source.manifest['sha256'] != reference['sha256']:
                raise ValueError('Registered redecision source snapshot changed')
            self.records = deepcopy(ledger['records'])
            self.marks = deepcopy(report['redecision_marks'])
        except BaseException:
            self.source.close()
            raise

    def close(self):
        self.source.close()


def from_redecision(ledger, source, *, forward_marks=None):
    from copy import deepcopy
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    ledger = deepcopy(validate_strategy_ledger(ledger))
    records, baseline = ledger['records'], source.records
    if records[:len(baseline)] != baseline:
        raise ValueError('Registered redecision price baseline differs from financial records')
    forward_marks = forward_marks or {}
    matches, issues = [], []
    for index, record in enumerate(records):
        fid = record['fixture_id']
        if index < len(baseline):
            marks = [deepcopy(item['rendered']) for item in source.marks.get(str(fid), [])]
        elif fid in forward_marks:
            marks = deepcopy(forward_marks[fid])
        else:
            raise ValueError('Forward extension has no explicit observed price source')
        if not marks:
            issues.append({'code':'observed_prices_unavailable', 'fixture_id':fid})
        matches.append({'fixture_id':fid, 'league':record.get('league'),
            'home':{'id':record.get('home_id'),'name':record.get('home'),'zh':record.get('home_zh','')},
            'away':{'id':record.get('away_id'),'name':record.get('away'),'zh':record.get('away_zh','')},
            'kickoff':record.get('date'),'round':record.get('stage',''),'settled':True,
            'score':record.get('score'),'result':record.get('result'),'marks':marks,
            'marks_status':'ok' if marks else 'unavailable','strategy_status':'recorded',
            'strategy_record':record, **_projection(record)})
    source.source.assert_unchanged()
    return {'strategy_ledger':ledger,'records':records,'matches':matches,'n':len(matches),'price_only_matches':[],
        'n_price_only':0,'as_of':ledger['as_of'],'source_as_of':None,'candidate_source':source.reference,
        'data_status':{'state':'degraded' if issues else 'ok','issues':issues}}


_WALL_TO_LABEL = {-5: "PRE", 15: "T15", 30: "T30", 47: "HT", 75: "T60", 90: "T75"}


def _approximate_labels(conn, doc, ledger):
    """Give corrected-candidate marks their milestone label, match minute and score.

    from_candidate leaves them blank because the candidate's match STATE is 'unavailable'
    (the +15-minute half-time is an approximation, so the repair refuses to call it a
    verified state). The label and the approximate minute follow from the sample's wall
    offset, and the score from fixture_event at that match minute — the same approximation
    the correction itself used. state_status says 'approximate_clock' so nobody reads it
    as an observed phase boundary."""
    kicks = {r["api_id"]: r["kickoff_ts"] for r in conn.execute("SELECT api_id, kickoff_ts FROM fixture")}
    homes = {r["api_id"]: r["home_api_id"] for r in conn.execute("SELECT api_id, home_api_id FROM fixture")}

    def score_at(fid, minute):
        gh = ga = 0
        for e in conn.execute("SELECT minute, team_api_id, detail FROM fixture_event WHERE fixture_api_id=? "
                              "AND type='Goal' ORDER BY minute, seq", (fid,)):
            if (e["detail"] or "") == "Missed Penalty" or (e["minute"] or 0) > minute:
                continue
            if (e["team_api_id"] == homes.get(fid)) ^ ((e["detail"] or "") == "Own Goal"):
                gh += 1
            else:
                ga += 1
        return gh, ga
    minutes = {"PRE": 0, "T15": 15, "T30": 30, "HT": 45, "T60": 60, "T75": 75}
    corrected = {r["fixture_id"] for r in ledger["records"]
                 if (r.get("leak_correction") or {}).get("milestone_source") == "candlestick"}
    for match in doc.get("matches", []):
        fid = match["fixture_id"]
        if fid not in corrected or not kicks.get(fid):
            continue
        ko = epoch(kicks[fid])
        for mark in match.get("marks", []):
            if mark.get("milestone") is not None or not mark.get("target_at"):
                continue
            wall = round((epoch(mark["target_at"]) - ko) / 60)
            label = _WALL_TO_LABEL.get(wall)
            if label is None:
                continue
            minute = minutes[label]
            gh, ga = score_at(fid, minute)
            score = f"{gh}-{ga}"
            mark.update(milestone=label, minute=minute, score=score, state_status="approximate_clock")
        match["marks"].sort(key=lambda m: _ORDER.get(m.get("milestone"), 9))
    # A point whose only defect was the unverified phase clock is now labelled; the issue
    # list keeps every point that still lacks a quote, so 'degraded' stays honest.
    labelled = {(m["fixture_id"], k["target_at"]) for m in doc.get("matches", []) if m["fixture_id"] in corrected
                for k in m.get("marks", []) if k.get("state_status") == "approximate_clock"
                and all(q.get("status") == "ok" for q in (k.get("quote_status") or {}).values())}
    issues = [i for i in doc.get("data_status", {}).get("issues", [])
              if not (i.get("code") == "candidate_point_unavailable" and (i.get("fixture_id"), i.get("target_at")) in labelled)]
    doc["data_status"] = {"state": "degraded" if issues else "ok", "issues": issues,
                          "note": "corrected-candidate points carry an approximate (+15 min half-time) clock"}
    return doc


def marks_for_book(conn, ledger, version):
    """The milestone_marks document for a SPECIFIC book version, active or not.

    _build() insists the ledger belongs to the active version because a live export must
    never publish a different book than the one the report shows. A version switch renders
    the TARGET book before it is active, and a rollback renders the previous one, so both
    need this explicit form. Explicit-backtest versions take their prices from the
    registered candidate; baseline versions from their own live rows, forward rows from
    paper observations."""
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    ledger = validate_strategy_ledger(ledger)
    if ledger.get('book_version', {}).get('version_id') != version['version_id']:
        raise ValueError('Ledger does not belong to the requested book version')
    if version.get('origin') == 'explicit_backtest':
        return _explicit_book_marks(conn, ledger, version)
    matches, issues = [], []
    for record in ledger['records']:
        fid = record['fixture_id']
        fwd = record.get('evidence_level') == 'forward_observed_paper' or record.get('pre_decision_id') or record.get('inplay_decision_id')
        marks = _forward_price_marks(conn, fid) if fwd else _live_row_marks(conn, fid)
        if not marks:
            issues.append({'code': 'milestone_prices_unavailable', 'fixture_id': fid})
        matches.append({'fixture_id': fid, 'league': record.get('league'),
            'home': {'id': record.get('home_id'), 'name': record.get('home'), 'zh': record.get('home_zh', '')},
            'away': {'id': record.get('away_id'), 'name': record.get('away'), 'zh': record.get('away_zh', '')},
            'kickoff': record.get('date'), 'round': record.get('stage', ''), 'settled': True,
            'score': record.get('score'), 'result': record.get('result'), 'marks': marks,
            'marks_status': 'ok' if marks else 'unavailable', 'strategy_status': 'recorded',
            'strategy_record': record, **_projection(record)})
    return {'strategy_ledger': ledger, 'records': ledger['records'], 'matches': matches, 'n': len(matches),
            'price_only_matches': [], 'n_price_only': 0, 'as_of': ledger['as_of'], 'source_as_of': None,
            'data_status': {'state': 'degraded' if issues else 'ok', 'issues': issues}}


def _live_row_marks(conn, fixture_id):
    """Marks straight from this fixture's own live-captured milestone rows — what the
    legacy view always showed for a live-marked fixture. Used for legacy rows whose prices
    a corrected-price candidate does not (and must not) describe."""
    rows = conn.execute("SELECT * FROM milestone_snapshot WHERE fixture_api_id=?", (fixture_id,)).fetchall()
    return [_mark(row) for row in sorted(rows, key=lambda r: _ORDER.get(r["milestone"], 9))]


def _forward_price_marks(conn, fixture_id):
    """Future book extensions use their observed paper receipts, never old snapshots."""
    from prediction_market_soccer.util import paper_store
    from prediction_market_soccer.util.source_history import resolve_receipt
    out = []
    for observation in paper_store.observations(conn,fixture_id):
        times = [observation['observed_at']]
        for quotes in (observation.get('quotes') or {}).values():
            if not isinstance(quotes,dict):
                continue
            for quote in quotes.values():
                receipt = quote.get('receipt') if isinstance(quote,dict) else None
                durable = resolve_receipt(conn,receipt.get('receipt_id')) if receipt else None
                if durable:
                    times.append(durable['available_at'])
        at = max(times,key=paper_store.dt)
        choices = paper_store.available_quotes(conn,observation,at)
        state = observation['state']
        out.append({'milestone':observation.get('milestone') or 'T'+str(state.get('elapsed')),
            'minute':state.get('elapsed'),'target_at':observation['observed_at'],
            'score':f"{state.get('home_goals','?')}-{state.get('away_goals','?')}",
            'poly_c':{side:to_cents(q['price']) for side,q in choices.get('poly',{}).items()},
            'kalshi_c':{side:to_cents(q['price']) for side,q in choices.get('kalshi',{}).items()},
            'devig':None,'model':None,'provenance':{'source':'paper_observation','observed_at':observation['observed_at']}})
    return out


def _explicit_book_marks(conn, ledger, version):
    import hashlib
    from pathlib import Path
    from prediction_market_soccer.util.research_inputs import CandidateMarketData,SourceSnapshot
    provenance = json.loads(version['provenance_json'])
    info = provenance.get('candidate_input')
    from prediction_market_soccer.util.forward_replay_certification import KIND,ForwardReplayPriceData
    from prediction_market_soccer.util.redecision_certification import KIND as REDECISION_KIND
    if isinstance(info,dict) and info.get('kind') == REDECISION_KIND:
        if info.get('run_id') != provenance.get('run_id'):
            raise ValueError('Registered redecision price run mismatch')
        source = RegisteredRedecisionPriceData(info)
        try:
            if version['baseline_count'] != len(source.records):
                raise ValueError('Registered redecision baseline size differs')
            forward = {r['fixture_id']:_forward_price_marks(conn,r['fixture_id']) for r in ledger['records'][version['baseline_count']:]}
            return from_redecision(ledger, source, forward_marks=forward)
        finally:
            source.close()
    from prediction_market_soccer.ops.owner_authorized_correction import KIND as OWNER_KIND
    if isinstance(info,dict) and info.get('kind')==OWNER_KIND:
        # Legacy rows: the sealed corrected-price candidate. Forward rows (tagged by evidence
        # tier, whatever their ordinal — the replacement re-baselined them) keep their own
        # observed paper marks; a candidate built from venue history must not describe them.
        if info.get('run_id')!=provenance.get('run_id'):
            raise ValueError('Owner-authorised correction price run mismatch')
        path=Path(info['path']).resolve(strict=True);root=Path(info['root']).resolve(strict=True)
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:
            raise ValueError('Corrected price source changed or escaped its root')
        candidate=CandidateMarketData(path,root=root,run_id=info['run_id'],scope_id=info['scope_id'])
        try:
            forward={}
            for record in ledger['records']:
                lc=record.get('leak_correction') or {}
                if record.get('evidence_level')=='forward_observed_paper':
                    forward[record['fixture_id']]=_forward_price_marks(conn,record['fixture_id'])
                elif lc.get('milestone_source')=='live':
                    forward[record['fixture_id']]=_live_row_marks(conn,record['fixture_id'])
                elif record['fixture_id'] not in candidate.manifest['fixture_ids']:
                    raise ValueError('Corrected book baseline is missing its price scope')
            return _approximate_labels(conn, from_candidate(ledger,candidate,forward_marks=forward), ledger)
        finally:
            candidate.close()
    if isinstance(info,dict) and info.get('kind')==KIND:
        if info.get('run_id')!=provenance.get('run_id'):
            raise ValueError('Forward replay price run mismatch')
        source=ForwardReplayPriceData(info)
        try:
            if version['baseline_count']!=len(source.records):
                raise ValueError('Registered replay baseline size differs from source')
            forward={r['fixture_id']:_forward_price_marks(conn,r['fixture_id']) for r in ledger['records'][version['baseline_count']:]}
            return from_forward_replay(ledger,source,forward_marks=forward)
        finally:
            source.close()
    if not isinstance(info,dict) or not all(info.get(k) for k in ('path','root','run_id','scope_id','sha256')):
        raise ValueError('Active candidate book has no validated price input; refusing legacy prices')
    if info['run_id'] != provenance.get('run_id'):
        raise ValueError('Candidate book price run mismatch')
    path=Path(info['path']).resolve(strict=True);root=Path(info['root']).resolve(strict=True)
    if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:
        raise ValueError('Candidate book price source changed or escaped its root')
    candidate=CandidateMarketData(path,root=root,run_id=info['run_id'],scope_id=info['scope_id'])
    source=None
    try:
        if candidate.manifest.get('require_observed_quotes'):
            if not all(info.get(k) for k in ('source_path','source_root','source_sha256')):
                raise ValueError('Observed candidate prices require their explicit source snapshot')
            source=SourceSnapshot(info['source_path'],root=info['source_root'],raw_archive=info.get('raw_archive'))
            if source.manifest['sha256']!=info['source_sha256'] or source.manifest['sha256']!=candidate.manifest.get('source_snapshot',{}).get('sha256'):
                raise ValueError('Candidate source snapshot hash mismatch')
            candidate.source=source
        forward={}
        for ordinal,record in enumerate(ledger['records']):
            if record['fixture_id'] not in candidate.manifest['fixture_ids']:
                if ordinal < version['baseline_count'] or not (record.get('pre_decision_id') or record.get('inplay_decision_id')):
                    raise ValueError('Candidate baseline is missing its price scope')
                forward[record['fixture_id']]=_forward_price_marks(conn,record['fixture_id'])
        return from_candidate(ledger,candidate,forward_marks=forward)
    finally:
        candidate.close()
        if source:
            source.close()


def build(conn=None, *, freeze=True, ledger=None) -> dict:
    """Read-only export. ``freeze`` is retained for compatibility and never freezes.

    Batch builders pass the exact performance report ledger. Standalone/live
    calls may only read its published snapshot, never reconstruct one.
    """
    if conn is not None:
        return _build(conn, ledger)
    conn = sqlite3.connect(f"file:{CONFIG.paths.data / 'soccer.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _build(conn, ledger)
    finally:
        conn.close()


@writer
def main() -> None:
    doc = build(freeze=False)
    CONFIG.paths.ensure()
    from prediction_market_soccer.ops.run_status import write_both
    write_both("milestone_marks.json", doc)
    print(f"milestone_marks.json: {doc['n']} recorded matches, {doc['n_price_only']} price-only matches")


if __name__ == "__main__":
    main()
