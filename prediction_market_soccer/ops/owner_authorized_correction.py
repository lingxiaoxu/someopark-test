"""ops/owner_authorized_correction.py — the owner-authorised leak-correction book bundle.

WHY A THIRD CERTIFICATION KIND EXISTS
    ops/version_workflow.validate_backtest_bundle can activate exactly two kinds of candidate
    book: an observed-forward exact replay and a strict-PIT observed-input redecision. Both
    require durable availability evidence that does not exist for the August 2026 history,
    and the generic research path ends in an unconditional refusal. That refusal is right for
    research. It is also why the book the front end shows (+11,682c) still carries prices a
    forensic pass proved leaky: nothing could replace them.

    On 2026-09-11 the owner explicitly ordered the freeze lifted, the leak corrected, and the
    result frozen as a new book. This module is the narrow, recorded path for that order:
      * the AUTHORISATION is a document in the bundle (who, when, the verbatim instruction,
        the exact scope hash), hashed into the certification, pinned into the version row;
      * the CORRECTION is limited to what the leak touched — prices from a sealed candidate
        built by the repair's own collector at the corrected clock; decisions, rules and model
        held fixed (ops/leak_corrected_book) — and every corrected record says so;
      * the LABEL is honest: strict_pit_certified is False and must stay False here;
        activation_eligible is True only because owner_override is True;
      * the FORWARD rows are proven unchanged (leg values byte-equal to the previous book).
    It cannot be used to certify any other research: a bundle without an authorisation, with
    a different analysis_purpose, or with strict_pit_certified=True is rejected outright.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

KIND = "soccer_owner_authorized_leak_correction_v1"
NAMES = ("input_manifest.json", "candidate_report.json", "completed_manifest.json", "authorization.json")
ALLOWED_EVIDENCE = {"leak_corrected_approx_pit_paper", "legacy_live_marked_paper", "posthoc_candlestick_paper",
                    "forward_observed_paper"}
ELIGIBILITY = {"corrected", "live_rerun", "kept_unresolved", "forward_unchanged", "no_leg"}
# Every running total the report carries. They are the only fields of a FORWARD row that a
# correction of earlier rows may change; each is recomputed in book order by the rebuild.
_CUMULATIVE = ("pre_cum_pnl_cents", "inplay_cum_pnl_cents", "combined_cum_pnl_cents", "combined_pnl_cents",
               "cum_pnl", "cum_pnl_cents", "realized_cum_pnl_cents", "argmax_cum_pnl_cents")


def _sha_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return _sha_bytes(_canon(value).encode("utf-8"))


def forward_signature(records: list[dict]) -> str:
    """Hash of the forward rows with the cumulative fields removed — the only fields a
    correction of earlier rows may legitimately change."""
    rows = [{k: v for k, v in r.items() if k not in _CUMULATIVE} for r in records
            if r.get("evidence_level") == "forward_observed_paper"]
    return digest(rows)


def eligibility_rows(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        lc = r.get("leak_correction") or {}
        fwd = r.get("evidence_level") == "forward_observed_paper"
        for track, has_leg in (("pre", bool(r.get("bet"))), ("inplay", bool(r.get("inplay_side")))):
            if fwd:
                status = "forward_unchanged"
            elif lc.get("status") == "unresolved":
                status = "kept_unresolved"
            elif lc.get("milestone_source") == "live":
                status = "live_rerun"
            elif lc:
                status = "corrected" if has_leg or track == "inplay" else "no_leg"
            else:
                status = "no_leg"
            out.append({"fixture_id": r["fixture_id"], "track": track, "status": status})
    return out


def build_bundle(*, output_dir, root, candidate_input: dict, report: dict, summary: dict,
                 previous_book: dict, authorization: dict, model_version: str, method_version: str,
                 run_id: str, rule_version: str) -> dict:
    """Write the five documents. ``report`` must already carry a validated strategy_ledger."""
    from prediction_market_soccer.util.research_inputs import _path, candidate_method_hashes
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    directory = _path(Path(output_dir), root)
    directory.mkdir(parents=True, exist_ok=False)
    ledger = validate_strategy_ledger(report["strategy_ledger"])
    if report.get("bet_log") != ledger["records"]:
        raise ValueError("report bet_log differs from its ledger")
    records = ledger["records"]
    fixture_ids = [r["fixture_id"] for r in records]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("duplicate fixture in ledger")
    for key in ("owner", "authorized_at", "statement"):
        if not str(authorization.get(key) or "").strip():
            raise ValueError("authorization requires owner, authorized_at and the verbatim statement")
    inputs = {"analysis_purpose": KIND, "model_version": model_version, "method_version": method_version,
              "fixture_ids": fixture_ids, "tracks": ["pre", "inplay"],
              "candidate_input": {**candidate_input, "kind": KIND},
              "candidate_input_hash": candidate_input["input_manifest_hash"],
              "candidate_items_hash": candidate_input["items_hash"],
              "previous_book": {**previous_book, "forward_signature": forward_signature(previous_book["records"])},
              "rule_version": rule_version, "code_hashes": candidate_method_hashes(),
              "lambdas_recorded_per_record": True}
    inputs["previous_book"].pop("records", None)
    eligibility = eligibility_rows(records)
    manifest = {"status": "completed", "run_id": run_id, "model_version": model_version,
                "method_version": method_version, "ledger_id": ledger["ledger_id"], "ledger_hash": digest(ledger),
                "input_manifest_hash": digest(inputs), "scope_hash": digest(fixture_ids),
                "scope_total": len(eligibility), "eligibility": eligibility,
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "strict_pit_certified": False, "activation_eligible": True, "owner_override": True,
                "pit_label": "approximate_reconstruction_leak_corrected", "summary": summary,
                "audit_complete": True, "financial_complete": True}
    report = {**report, "input_manifest_hash": manifest["input_manifest_hash"], "eligibility": eligibility}
    auth = {"kind": KIND, **authorization, "scope_hash": manifest["scope_hash"],
            "previous_version_id": previous_book.get("version_id"), "run_id": run_id}
    docs = {"input_manifest.json": inputs, "candidate_report.json": report,
            "completed_manifest.json": manifest, "authorization.json": auth}
    encoded = {name: _canon(doc).encode("utf-8") for name, doc in docs.items()}
    cert = {"kind": KIND, "documents": {name: _sha_bytes(body) for name, body in encoded.items()},
            "certified_at": manifest["completed_at"]}
    cert["certification_id"] = digest(cert)
    for name, body in encoded.items():
        with (directory / name).open("xb") as stream:
            stream.write(body)
    with (directory / "certification.json").open("x", encoding="utf-8") as stream:
        stream.write(_canon(cert))
    return {"directory": str(directory), "manifest": manifest, "certification": cert}


def validate_bundle(bundle, *, root) -> dict:
    """Return the verified documents or raise. Every refusal names its reason."""
    from prediction_market_soccer.util.research_inputs import CandidateMarketData, _path, candidate_method_hashes
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    directory = _path(Path(bundle), root, exists=True)
    docs = {}
    for name in NAMES + ("certification.json",):
        body = _path(directory / name, root, exists=True).read_bytes()
        docs[name] = (json.loads(body), _sha_bytes(body))
    cert = docs["certification.json"][0]
    if cert.get("kind") != KIND:
        raise ValueError("Not an owner-authorised correction bundle")
    if cert.get("certification_id") != digest({k: v for k, v in cert.items() if k != "certification_id"}):
        raise ValueError("Certification identity does not match its documents")
    for name in NAMES:
        if cert["documents"].get(name) != docs[name][1]:
            raise ValueError("Certified document changed: " + name)
    inputs, report, manifest, auth = (docs[n][0] for n in NAMES)
    if inputs.get("analysis_purpose") != KIND or (inputs.get("candidate_input") or {}).get("kind") != KIND:
        raise ValueError("Bundle purpose is not the owner-authorised correction")
    if auth.get("kind") != KIND or not all(str(auth.get(k) or "").strip() for k in ("owner", "authorized_at", "statement")):
        raise ValueError("Owner authorisation is missing or incomplete")
    if manifest.get("strict_pit_certified") is not False or manifest.get("owner_override") is not True \
            or manifest.get("activation_eligible") is not True or manifest.get("status") != "completed":
        raise ValueError("Correction manifest flags are not the honest owner-override set")
    if manifest.get("pit_label") != "approximate_reconstruction_leak_corrected":
        raise ValueError("Correction manifest carries an unexpected PIT label")
    if inputs.get("code_hashes") != candidate_method_hashes():
        raise ValueError("Method code changed since the correction was built; rebuild the bundle")
    ledger = validate_strategy_ledger(report.get("strategy_ledger"))
    if report.get("bet_log") != ledger["records"]:
        raise ValueError("Report records differ from the ledger")
    if (manifest.get("ledger_hash") != digest(ledger) or manifest.get("ledger_id") != ledger["ledger_id"]
            or manifest.get("input_manifest_hash") != digest(inputs) or report.get("input_manifest_hash") != digest(inputs)):
        raise ValueError("Input/report/ledger evidence chain mismatch")
    ids = inputs["fixture_ids"]
    if ids != [r["fixture_id"] for r in ledger["records"]] or len(set(ids)) != len(ids):
        raise ValueError("Declared fixture scope differs from the ledger order")
    if manifest.get("scope_hash") != digest(ids) or auth.get("scope_hash") != digest(ids):
        raise ValueError("Authorisation scope does not match the ledger")
    expected = {(fid, t) for fid in ids for t in inputs["tracks"]}
    actual = [(r["fixture_id"], r["track"]) for r in manifest.get("eligibility", [])]
    if set(actual) != expected or len(actual) != len(expected) or manifest.get("scope_total") != len(expected):
        raise ValueError("Eligibility does not cover every fixture and track")
    if report.get("eligibility") != manifest["eligibility"] or any(r["status"] not in ELIGIBILITY for r in actual and manifest["eligibility"]):
        raise ValueError("Eligibility rows are inconsistent or carry an unknown status")
    for r in ledger["records"]:
        if r.get("evidence_level") not in ALLOWED_EVIDENCE:
            raise ValueError("Record carries an unknown evidence tier: " + str(r.get("evidence_level")))
        fwd = r.get("evidence_level") == "forward_observed_paper"
        lc = r.get("leak_correction")
        if not fwd and not isinstance(lc, dict):
            raise ValueError("A legacy record lacks its leak_correction provenance")
        if not fwd and lc.get("run_id") != inputs["candidate_input"]["run_id"]:
            raise ValueError("A legacy record was corrected by a different price run")
        if fwd and lc is not None:
            raise ValueError("A forward record must not be re-priced")
        if not fwd:
            src, status = lc.get("milestone_source"), lc.get("status")
            if lc.get("rule_version") != inputs.get("rule_version"):
                raise ValueError("A legacy record was rebuilt under a different rule version")
            if src == "candlestick" and status != "unresolved":
                if (r.get("evidence_level"), r.get("pit_status")) != ("leak_corrected_approx_pit_paper", "approximate_reconstruction_leak_corrected") \
                        or not isinstance(lc.get("before"), dict):
                    raise ValueError("A corrected record is mislabelled or lacks its before-state")
            elif src == "live":
                if status != "unchanged" or r.get("evidence_level") == "leak_corrected_approx_pit_paper":
                    raise ValueError("A live-marked record must be unchanged and keep its tier")
    if forward_signature(ledger["records"]) != inputs["previous_book"]["forward_signature"]:
        raise ValueError("Forward rows differ from the previous book beyond their cumulative fields")
    ci = inputs["candidate_input"]
    path = _path(Path(ci["path"]), root, exists=True)
    if ci.get("root") != str(Path(root).resolve()) or _sha_bytes(path.read_bytes()) != ci.get("sha256"):
        raise ValueError("Candidate price database changed or escaped its root")
    data = CandidateMarketData(path, root=root, run_id=ci["run_id"], scope_id=ci["scope_id"])
    try:
        if (data.completion["input_manifest_hash"] != inputs["candidate_input_hash"]
                or data.completion["items_hash"] != inputs["candidate_items_hash"]):
            raise ValueError("Bundle references another candidate price run")
        scope = set(data.manifest["fixture_ids"])
    finally:
        data.close()
    for r in ledger["records"]:
        if r.get("evidence_level") == "leak_corrected_approx_pit_paper" and r["fixture_id"] not in scope:
            raise ValueError("A corrected record lies outside the candidate price scope")
    return {name: docs[name][0] for name in NAMES + ("certification.json",)}
