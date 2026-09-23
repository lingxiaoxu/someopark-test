"""Alias identity guards: isolated /tmp state and no network or production writes."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest
import requests

from controller import registry as registry_mod
import ticker_aliases


FIGI = "BBG000BD8PN9"
CIK = "0001390777"
CUSIP = "064058100"
ISIN = registry_mod.isin_from_cusip(CUSIP)
OTHER_ISIN = registry_mod.isin_from_cusip("037833100")


def alias(current="NEWT", **anchors):
    return {"current": current, "changed": "2026-05-21", **anchors}


def security(ticker="NEWT", *, figi=FIGI, cik=CIK, cusip=CUSIP):
    return {
        "polygon_ticker": ticker, "figi": figi, "cik": cik, "cusip": cusip,
        "name": ticker, "asset_class": "equity", "status": "active",
        "registered_at": "2026-08-12T00:00:00+00:00",
        "ticker_history": [{"ticker": ticker, "from": "2026-08-12", "to": None}],
    }


@pytest.fixture
def isolated_registry(monkeypatch):
    # Patch both the persistence paths and the cache object. Restoring only cache
    # contents would leak the fixture's alias path/cache into another test module.
    with tempfile.TemporaryDirectory(prefix="registry-alias-identity-", dir="/tmp") as td:
        root = Path(td)
        reg_dir = root / "controller" / "registry"
        reg_dir.mkdir(parents=True)
        for name, value in {
            "REPO": root,
            "REG_DIR": reg_dir,
            "MASTER_PATH": reg_dir / "security_master.json",
            "NODES_PATH": reg_dir / "node_registry.json",
            "CHANGELOG": reg_dir / "changelog.jsonl",
        }.items():
            monkeypatch.setattr(registry_mod, name, str(value))
        monkeypatch.setattr(registry_mod, "_now", lambda: "2026-09-21T12:00:00+00:00")
        monkeypatch.setattr(ticker_aliases, "ALIAS_PATH", root / "ticker_aliases.json")
        monkeypatch.setattr(ticker_aliases, "_CACHE", {"mtime": None, "data": {}, "delist": {}})
        monkeypatch.setattr(ticker_aliases, "_today", lambda: "2026-09-21")
        monkeypatch.setenv("POLYGON_API_KEY", "test-only-not-a-real-credential")
        (root / "inventory_bdc.json").write_text(json.dumps({"holdings": {}}))
        Path(registry_mod.NODES_PATH).write_text("{}")

        def no_network(*args, **kwargs):
            raise AssertionError("This test must not issue network requests")

        monkeypatch.setattr(requests.sessions.Session, "request", no_network)
        monkeypatch.setattr(requests, "get", no_network)
        monkeypatch.setattr(registry_mod, "collect_universe", no_network)
        monkeypatch.setattr(registry_mod, "fetch_ftd_cusip_map", no_network)

        class Sandbox:
            def seed(self, *, aliases=None, master=None):
                ticker_aliases.ALIAS_PATH.write_text(json.dumps({
                    "schema": "v2", "aliases": aliases or {}, "delistings": {},
                }))
                ticker_aliases._CACHE.update(mtime=None, data={}, delist={})
                Path(registry_mod.MASTER_PATH).write_text(json.dumps(master or {}))

            def refresh(self, universe, references, cusips=None):
                """Stub only external inputs; exercise real build_master orchestration."""
                calls = []
                monkeypatch.setattr(registry_mod, "collect_universe", lambda: list(universe))
                monkeypatch.setattr(registry_mod, "fetch_ftd_cusip_map", lambda: cusips or {})

                def reference_get(url, **kwargs):
                    prefix = "https://api.polygon.io/v3/reference/tickers/"
                    assert url.startswith(prefix), url
                    ticker = url[len(prefix):]
                    assert ticker in references, f"Unexpected reference ticker {ticker}"
                    calls.append(ticker)
                    doc = references[ticker]

                    class Response:
                        status_code = 200

                        def json(self):
                            return {"results": doc}

                    return Response()

                monkeypatch.setattr(requests, "get", reference_get)
                return calls

        sandbox = Sandbox()
        sandbox.seed()
        yield sandbox


def test_unaliased_direct_lookup_still_accepts_existing_record_without_anchors(isolated_registry):
    isolated_registry.seed(master={ISIN: security("PLAIN", figi=None, cik=None)})
    reg = registry_mod.Registry()
    assert reg.isin_of("PLAIN") == ISIN
    with pytest.raises(registry_mod.RegistryError):
        reg.isin_of("UNKNOWN")


@pytest.mark.parametrize("target_cik", ["1390777", "0001390777", 1390777])
def test_valid_cik_only_alias_accepts_leading_zero_normalization(isolated_registry, target_cik):
    isolated_registry.seed(
        aliases={"OLDT": alias(cik=CIK)},
        master={ISIN: security(figi=None, cik=target_cik)},
    )
    assert registry_mod.Registry().isin_of("OLDT") == ISIN


def test_valid_figi_only_alias_does_not_require_cik(isolated_registry):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI)},
        master={ISIN: security(cik=None)},
    )
    assert registry_mod.Registry().isin_of("OLDT") == ISIN


@pytest.mark.parametrize("alias_anchors,target_anchors", [
    ({}, {"figi": FIGI, "cik": CIK}),
    ({"figi": FIGI, "cik": CIK}, {"figi": None, "cik": None}),
    ({"figi": FIGI}, {"figi": None, "cik": CIK}),
    ({"cik": CIK}, {"figi": FIGI, "cik": None}),
])
def test_alias_without_any_comparable_anchor_is_rejected(
    isolated_registry, alias_anchors, target_anchors,
):
    isolated_registry.seed(
        aliases={"OLDT": alias(**alias_anchors)},
        master={ISIN: security(**target_anchors)},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


@pytest.mark.parametrize("target_anchors", [
    {"figi": "BBGOTHER", "cik": CIK},
    {"figi": FIGI, "cik": "9999999"},
])
def test_one_matching_anchor_cannot_hide_other_anchor_mismatch(isolated_registry, target_anchors):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI, cik=CIK)},
        master={ISIN: security(**target_anchors)},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


@pytest.mark.parametrize("first_anchors,last_anchors", [
    ({"figi": FIGI, "cik": CIK}, {"figi": FIGI, "cik": "1390777"}),
    ({"figi": FIGI}, {"cik": CIK}),
])
def test_valid_multihop_alias_chain_keeps_one_identity(
    isolated_registry, first_anchors, last_anchors,
):
    isolated_registry.seed(
        aliases={
            "OLDT": alias("MIDT", **first_anchors),
            "MIDT": alias(**last_anchors),
        },
        master={ISIN: security()},
    )
    reg = registry_mod.Registry()
    assert reg.isin_of("OLDT") == reg.isin_of("MIDT") == reg.isin_of("NEWT") == ISIN


def test_cyclic_alias_chain_is_rejected(isolated_registry):
    isolated_registry.seed(
        aliases={
            "OLDT": alias("MIDT", figi=FIGI, cik=CIK),
            "MIDT": alias("OLDT", figi=FIGI, cik=CIK),
        },
        master={ISIN: security("OLDT")},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


def test_alias_chain_truncated_before_final_name_is_rejected(isolated_registry):
    # Six edges exceed canonical's five-hop cap. Put the capped intermediate
    # name in the master so a partial resolution could otherwise appear valid.
    names = ["OLDT"] + [f"MID{i}" for i in range(1, ticker_aliases._MAX_HOPS + 1)] + ["NEWT"]
    isolated_registry.seed(
        aliases={old: alias(new, figi=FIGI, cik=CIK) for old, new in zip(names, names[1:])},
        master={ISIN: security(names[ticker_aliases._MAX_HOPS])},
    )
    assert ticker_aliases.canonical("OLDT") != names[-1]
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


@pytest.mark.parametrize("bad_hop", ["OLDT", "MIDT"])
@pytest.mark.parametrize("bad_anchors", [{"figi": "BBGOTHER", "cik": CIK}, {}])
def test_each_multihop_alias_entry_must_verify_final_identity(isolated_registry, bad_hop, bad_anchors):
    aliases = {
        "OLDT": alias("MIDT", figi=FIGI, cik=CIK),
        "MIDT": alias(figi=FIGI, cik=CIK),
    }
    aliases[bad_hop] = alias(aliases[bad_hop]["current"], **bad_anchors)
    isolated_registry.seed(aliases=aliases, master={ISIN: security()})
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


def test_valid_old_direct_hit_remains_usable_before_master_refresh(isolated_registry):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI, cik=CIK)},
        master={ISIN: security("OLDT")},
    )
    assert registry_mod.Registry().isin_of("OLDT") == ISIN


@pytest.mark.parametrize("target_anchors", [
    {"figi": "BBGOTHER", "cik": CIK},
    {"figi": None, "cik": None},
])
def test_old_direct_hit_cannot_bypass_alias_identity_guard(isolated_registry, target_anchors):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI, cik=CIK)},
        master={ISIN: security("OLDT", **target_anchors)},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


def test_old_and_new_direct_entries_must_not_identify_different_isins(isolated_registry):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI, cik=CIK)},
        master={
            ISIN: security("OLDT"),
            OTHER_ISIN: security("NEWT", cusip="037833100"),
        },
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.Registry().isin_of("OLDT")


def test_recycled_old_name_is_a_separate_entity_not_an_active_alias(isolated_registry):
    isolated_registry.seed(
        aliases={"OLDT": {**alias(figi=FIGI, cik=CIK), "recycled": "2026-09-01"}},
        master={
            ISIN: security("NEWT"),
            OTHER_ISIN: security("OLDT", figi="BBGOTHER", cik="9999999", cusip="037833100"),
        },
    )
    reg = registry_mod.Registry()
    assert reg.isin_of("OLDT") == OTHER_ISIN
    assert reg.isin_of("NEWT") == ISIN


@pytest.mark.parametrize("universe", [["OLDT"], ["NEWT"], ["NEWT", "OLDT"]])
@pytest.mark.parametrize("reference_anchors", [
    {"composite_figi": "BBGOTHER", "cik": CIK},
    {"composite_figi": FIGI, "cik": "9999999"},
    {},
])
def test_refresh_checks_alias_identity_even_for_new_name_or_seen_target(
    isolated_registry, universe, reference_anchors,
):
    master = {ISIN: security()}
    isolated_registry.seed(aliases={"OLDT": alias(figi=FIGI, cik=CIK)}, master=master)
    isolated_registry.refresh(
        universe, {"NEWT": {"cusip": CUSIP, "type": "CS", **reference_anchors}},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.build_master()
    assert json.loads(Path(registry_mod.MASTER_PATH).read_text()) == master


def test_refresh_valid_rename_preserves_isin_and_fetches_current_name_once(isolated_registry):
    isolated_registry.seed(
        aliases={"OLDT": alias(figi=FIGI, cik=CIK)},
        master={ISIN: security("OLDT")},
    )
    calls = isolated_registry.refresh(
        ["NEWT", "OLDT"],
        {"NEWT": {"composite_figi": FIGI, "cik": 1390777, "type": "CS"}},
        cusips={"NEWT": CUSIP},
    )
    registry_mod.build_master()
    assert calls == ["NEWT"]
    reg = registry_mod.Registry()
    assert reg.isin_of("OLDT") == reg.isin_of("NEWT") == ISIN
    assert reg.master[ISIN]["polygon_ticker"] == "NEWT"


def test_refresh_matching_figi_cik_cannot_silently_replace_existing_isin(isolated_registry):
    master = {ISIN: security("OLDT")}
    isolated_registry.seed(aliases={"OLDT": alias(figi=FIGI, cik=CIK)}, master=master)
    isolated_registry.refresh(
        ["NEWT"],
        {"NEWT": {"cusip": "037833100", "composite_figi": FIGI, "cik": CIK, "type": "CS"}},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.build_master()
    assert json.loads(Path(registry_mod.MASTER_PATH).read_text()) == master


@pytest.mark.parametrize("universe", [["OLDT"], ["NEWT"], ["NEWT", "OLDT"]])
def test_refresh_valid_last_hop_cannot_hide_conflicting_earlier_hop(isolated_registry, universe):
    isolated_registry.seed(
        aliases={
            "OLDT": alias("MIDT", figi="BBGOTHER", cik=CIK),
            "MIDT": alias(figi=FIGI, cik=CIK),
        },
        master={ISIN: security()},
    )
    isolated_registry.refresh(
        universe,
        {"NEWT": {"cusip": CUSIP, "composite_figi": FIGI, "cik": CIK, "type": "CS"}},
    )
    with pytest.raises(registry_mod.RegistryError):
        registry_mod.build_master()
