"""ClubElo from the website (ingest/clubelo_web) and the website-first order in club_prior.

Fixtures are cut from the live pages of 2026-09-01 (the world table rows for Bayern /
Liverpool / Sevilla, three rows of the Norwegian top-25 JS array, a three-point Vega dataset).
The API had been serving a snapshot frozen since July, so the tests also pin the freeze
detector and the fallback order: website → stored history → API, with a frozen API answer
refused in favour of the latest website-derived file."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from prediction_market_soccer.ingest import clubelo_web as W

FX = Path(__file__).parent / "fixtures"


def test_world_table_rows_carry_slug_name_country_rank_and_elo():
    rows = W.parse_world_table((FX / "clubelo_world.html").read_text(encoding="utf-8"))
    by = {r["slug"]: r for r in rows}
    assert {"Bayern", "Liverpool", "Sevilla"} <= set(by)
    assert by["Bayern"]["name"] == "Bayern München" and by["Bayern"]["site_cc"] == "GER"
    assert by["Liverpool"]["site_cc"] == "ENG" and by["Liverpool"]["rank"] == 8
    assert all(isinstance(r["elo"], int) and 1000 < r["elo"] < 2300 for r in rows)


def test_country_array_rows_include_small_clubs():
    rows = W.parse_country_array((FX / "clubelo_country_NOR.html").read_text(encoding="utf-8"))
    assert len(rows) == 3
    assert all(r["site_cc"] == "NOR" for r in rows)
    assert all(isinstance(r["elo"], int) for r in rows)
    assert all(r["name"] and r["slug"] for r in rows)


def test_history_is_parsed_from_the_vega_dataset_in_date_order():
    pts = W.parse_history((FX / "clubelo_club.html").read_text(encoding="utf-8"))
    assert [p["date"] for p in pts] == ["2026-05-24", "2026-08-23", "2026-08-29"]
    assert abs(pts[-1]["elo"] - 1901.6966568312516) < 1e-9
    assert W.history_as_of(pts, "2026-07-06") == pytest.approx(1904.2781329839481)
    assert W.history_as_of(pts, "2026-05-01") is None          # before the first point
    assert W.history_as_of(pts, "2026-08-29") == pytest.approx(1901.6966568312516)


def test_norm_name_transliterates_the_way_the_api_spells():
    assert W.norm_name("Bodø/Glimt") == W.norm_name("Bodoe Glimt")
    assert W.norm_name("Malmö") == W.norm_name("Malmoe")
    assert W.norm_name("Zürich") == W.norm_name("Zuerich")
    assert W.norm_name("Fürth") == W.norm_name("Fuerth")
    assert W.norm_name("Atlético") == "atletico"


def test_country_codes_round_trip_between_site_and_api():
    assert W.SITE_TO_API_CODE["ROU"] == "ROM" and W.SITE_TO_API_CODE["SVK"] == "SLK"
    assert W.API_TO_SITE_CODE["ROM"] == "ROU"
    assert len(W.EURO_SITE_CODES) == 55 and len(set(W.EURO_SITE_CODES)) == 55
    assert "ROM" not in W.EURO_SITE_CODES and "ROU" in W.EURO_SITE_CODES    # site spelling only


def test_to_api_rows_uses_api_names_codes_and_history_precision(monkeypatch):
    # the API reference is pinned so an unmapped club's bare name cannot collide with a real
    # API club (the real list does contain "CFR Cluj" — that case is covered separately)
    monkeypatch.setattr(W, "_api_reference", lambda: ({"Bayern": {"country": "GER"}}, {}))
    site_rows = [{"slug": "Bayern", "name": "Bayern München", "site_cc": "GER", "rank": 1, "elo": 2027},
                 {"slug": "Cluj", "name": "CFR Cluj", "site_cc": "ROU", "rank": None, "elo": 1500}]
    nm = {"Bayern": {"api": "Bayern", "site": "Bayern München", "country_api": "GER", "method": "norm"}}
    hist = {"Bayern": [{"date": "2026-08-30", "elo": 2027.31}]}
    rows = W.to_api_rows(site_rows, "2026-09-02", name_map=nm, histories=hist)
    assert [r["Club"] for r in rows] == ["Bayern", "CFR Cluj"]           # mapped name, else site name
    assert rows[0]["Elo"] == pytest.approx(2027.31)                       # history precision within 1 Elo
    assert rows[1]["Country"] == "ROM"                                    # API's code, not the site's
    assert set(rows[0]) == {"Rank", "Club", "Country", "Level", "Elo", "From", "To"}


def test_freeze_detector():
    a = [{"Club": f"c{i}", "Elo": 1500.0 + i} for i in range(150)]
    b = [{"Club": f"c{i}", "Elo": 1500.0 + i} for i in range(150)]
    assert W.is_frozen(a, b) is True
    b[3]["Elo"] += 0.5
    assert W.is_frozen(a, b) is False
    assert W.is_frozen(a[:50], b[:50]) is False                          # too few clubs to judge
    assert W.is_frozen(a, None) is False


def test_reconstruct_date_flags_provenance_per_club():
    hist = {"Liverpool": [{"date": "2026-05-24", "elo": 1904.28}, {"date": "2026-08-23", "elo": 1903.4}]}
    nm = {"Liverpool": {"api": "Liverpool", "site": "Liverpool", "country_api": "ENG", "method": "norm"}}
    frozen = [{"Rank": "9", "Club": "Liverpool", "Country": "ENG", "Level": "1", "Elo": "1910.8", "From": "x", "To": "y"},
              {"Rank": "20", "Club": "Tre Fiori", "Country": "SMR", "Level": "1", "Elo": "1100.0", "From": "x", "To": "y"}]
    rows, prov = W.reconstruct_date("2026-07-06", histories=hist, name_map=nm, frozen_api_rows=frozen, offset=66.3)
    by = {r["Club"]: r for r in rows}
    assert by["Liverpool"]["Elo"] == pytest.approx(1904.28) and prov["Liverpool"] == "web_history"
    # the filler club is moved onto the site's scale, never mixed in raw
    assert float(by["Tre Fiori"]["Elo"]) == pytest.approx(1166.3) and prov["Tre Fiori"] == "api_frozen_rescaled"


def test_fetch_clubelo_prefers_the_website_and_never_calls_the_api_on_success(tmp_path, monkeypatch):
    from prediction_market_soccer.ingest import club_prior as CP
    monkeypatch.setattr(CP, "_PRIORS", tmp_path)
    monkeypatch.setattr(W, "PRIORS", tmp_path)
    today = W._today()
    calls = {"api": 0}
    monkeypatch.setattr(W, "fetch_daily", lambda date=None, **k: [{"slug": "Bayern", "name": "Bayern München",
                                                                  "site_cc": "GER", "rank": 1, "elo": 2027, "src": "world"}])
    monkeypatch.setattr(W, "load_daily", lambda date: None)
    monkeypatch.setattr(W, "validate_daily", lambda rows: [])          # the one-row fake is not a real parse
    monkeypatch.setattr(W, "load_name_map", lambda: {"Bayern": {"api": "Bayern", "site": "Bayern München", "country_api": "GER", "method": "norm"}})
    monkeypatch.setattr(W, "load_histories", lambda: {})
    monkeypatch.setattr(W, "_mirror", lambda p: None)
    monkeypatch.setattr(CP, "_fetch_clubelo_api", lambda as_of: calls.__setitem__("api", calls["api"] + 1) or [])
    rows = CP._fetch_clubelo(today)
    assert rows[0]["Club"] == "Bayern" and rows[0]["Country"] == "GER"
    assert calls["api"] == 0
    assert (tmp_path / f"clubelo_{today}.source").read_text() == "web"
    # second call is served from the cache (no fetch at all)
    monkeypatch.setattr(W, "fetch_daily", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched")))
    assert CP._fetch_clubelo(today)[0]["Club"] == "Bayern"


def test_fetch_clubelo_falls_back_to_the_api_and_refuses_a_frozen_answer(tmp_path, monkeypatch):
    """Website down → API. An API answer identical to the API's OWN previous snapshot is
    frozen and refused in favour of the latest website-derived file; a moving answer is
    accepted. The comparison must never be against a website file (different scale)."""
    from prediction_market_soccer.ingest import club_prior as CP
    monkeypatch.setattr(CP, "_PRIORS", tmp_path)
    monkeypatch.setattr(W, "PRIORS", tmp_path)
    monkeypatch.setattr(W, "_mirror", lambda p: None)
    today = W._today()
    from datetime import datetime, timedelta
    d1 = (datetime.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (datetime.fromisoformat(today) - timedelta(days=2)).strftime("%Y-%m-%d")
    # two days ago: a website-derived file (site scale, +66); yesterday: the API's own file
    web_rows = [{"Rank": "", "Club": f"c{i}", "Country": "ENG", "Level": "", "Elo": 1566.0 + i, "From": d2, "To": d2} for i in range(120)]
    W.write_csv(web_rows, d2, source="web")
    api_prev = [{"Rank": "", "Club": f"c{i}", "Country": "ENG", "Level": "", "Elo": 1500.0 + i, "From": d1, "To": d1} for i in range(120)]
    W.write_csv(api_prev, d1, source="api")
    monkeypatch.setattr(W, "load_daily", lambda date: None)
    monkeypatch.setattr(W, "fetch_daily", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
    monkeypatch.setattr(W, "load_histories", lambda: {})
    frozen = [dict(r, Elo=str(r["Elo"]), From=today, To=today) for r in api_prev]
    monkeypatch.setattr(CP, "_fetch_clubelo_api", lambda as_of: frozen)
    rows = CP._fetch_clubelo(today)
    assert (tmp_path / f"clubelo_{today}.source").read_text() == "web_stale"
    assert float(rows[0]["Elo"]) == 1566.0                      # the website file, not the frozen API
    (tmp_path / f"clubelo_{today}.csv").unlink(); (tmp_path / f"clubelo_{today}.source").unlink()
    moving = [dict(r, Elo=str(float(r["Elo"]) + (1.0 if i % 7 == 0 else 0.0))) for i, r in enumerate(frozen)]
    monkeypatch.setattr(CP, "_fetch_clubelo_api", lambda as_of: moving)
    CP._fetch_clubelo(today)
    assert (tmp_path / f"clubelo_{today}.source").read_text() == "api"


def test_fetch_clubelo_never_pins_the_site_state_on_another_day(tmp_path, monkeypatch):
    """Tomorrow: served from the latest website file, nothing written. Yesterday: never the
    website (that would fingerprint today's ratings as yesterday's PIT prior)."""
    from prediction_market_soccer.ingest import club_prior as CP
    monkeypatch.setattr(CP, "_PRIORS", tmp_path)
    monkeypatch.setattr(W, "PRIORS", tmp_path)
    monkeypatch.setattr(W, "_mirror", lambda p: None)
    today = W._today()
    from datetime import datetime, timedelta
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).strftime("%Y-%m-%d")
    yday = (datetime.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")
    W.write_csv([{"Rank": "", "Club": "Bayern", "Country": "GER", "Level": "", "Elo": 2027.0, "From": today, "To": today}], today, source="web")
    called = {"web": 0}
    monkeypatch.setattr(W, "fetch_daily", lambda *a, **k: called.__setitem__("web", called["web"] + 1) or [])
    monkeypatch.setattr(W, "load_daily", lambda date: None)
    monkeypatch.setattr(W, "load_histories", lambda: {})
    rows = CP._fetch_clubelo(tomorrow)
    assert rows[0]["Club"] == "Bayern" and not (tmp_path / f"clubelo_{tomorrow}.csv").exists()
    assert called["web"] == 0
    monkeypatch.setattr(CP, "_fetch_clubelo_api", lambda as_of: (_ for _ in ()).throw(RuntimeError("502")))
    rows = CP._fetch_clubelo(yday)          # no histories, API down → latest web file, no fetch
    assert called["web"] == 0 and rows[0]["Club"] == "Bayern"


def test_write_csv_keeps_the_frozen_api_file_as_a_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "PRIORS", tmp_path)
    monkeypatch.setattr(W, "_mirror", lambda p: None)
    d = "2026-07-20"
    (tmp_path / f"clubelo_{d}.csv").write_text("Rank,Club,Country,Level,Elo,From,To\n1,X,ENG,1,1500,a,b\n", encoding="utf-8")
    W.write_csv([{"Rank": "", "Club": "X", "Country": "ENG", "Level": "", "Elo": 1511.5, "From": d, "To": d}], d, source="web_history")
    assert (tmp_path / f"clubelo_{d}.csv.frozen_api").read_text().splitlines()[1].startswith("1,X,ENG,1,1500")
    new = list(csv.DictReader(io.StringIO((tmp_path / f"clubelo_{d}.csv").read_text())))
    assert float(new[0]["Elo"]) == 1511.5 and (tmp_path / f"clubelo_{d}.source").read_text() == "web_history"


def test_name_map_never_lets_a_same_named_foreign_club_take_the_api_name(monkeypatch):
    """ClubElo's world table has a Uruguayan Liverpool; the API's Liverpool is English."""
    monkeypatch.setattr(W, "_api_reference", lambda: ({"Liverpool": {"country": "ENG"}}, {}))
    rows = [{"slug": "LiverpoolURU", "name": "Liverpool", "site_cc": "URU", "rank": None, "elo": 1600},
            {"slug": "Liverpool", "name": "Liverpool", "site_cc": "ENG", "rank": 8, "elo": 1902}]
    m = W.build_name_map(rows)
    assert m["Liverpool"]["api"] == "Liverpool" and m["Liverpool"]["method"] == "norm"
    assert m["LiverpoolURU"]["api"] is None


def test_unmapped_foreign_club_never_reuses_an_api_name(monkeypatch):
    monkeypatch.setattr(W, "_api_reference", lambda: ({"Liverpool": {"country": "ENG"}}, {}))
    site_rows = [{"slug": "LiverpoolUY", "name": "Liverpool", "site_cc": "URU", "rank": None, "elo": 1571},
                 {"slug": "Liverpool", "name": "Liverpool", "site_cc": "ENG", "rank": 8, "elo": 1902}]
    nm = {"Liverpool": {"api": "Liverpool", "site": "Liverpool", "country_api": "ENG", "method": "norm"},
          "LiverpoolUY": {"api": None, "site": "Liverpool", "country_api": "URU", "method": None}}
    rows = W.to_api_rows(site_rows, "2026-09-02", name_map=nm)
    assert sorted(r["Club"] for r in rows) == ["Liverpool", "Liverpool (URU)"]
    hist = {"LiverpoolUY": [{"date": "2026-05-01", "elo": 1550.7}], "Liverpool": [{"date": "2026-05-24", "elo": 1904.3}]}
    rec, prov = W.reconstruct_date("2026-05-31", histories=hist, name_map=nm, frozen_api_rows=None, offset=0.0)
    by = {r["Club"]: r["Elo"] for r in rec}
    assert by["Liverpool"] == pytest.approx(1904.3) and by["Liverpool (URU)"] == pytest.approx(1550.7)


def test_three_letter_club_slugs_are_clubs_not_countries():
    """AEK / AIK / PSV / QPR / IDV have 3-letter upper-case hrefs like a country link."""
    row = ('<table><tr><td class="l"><a href="/GRE"><img src="/static/flags/grc.png" alt="GRE"></a> '
           '<small> 120 </small><a href="/AEK"><span class="NonAst">AEK</span><span class="Ast">AEK Athens</span></a></td>'
           '<td class="r">1650</td></tr></table>')
    rows = W.parse_world_table(row)
    assert rows and rows[0]["slug"] == "AEK" and rows[0]["site_cc"] == "GRE" and rows[0]["elo"] == 1650
    js = ("<script>var d = [['<td class=\"l\"><a href=\"/NED\"><img src=\"/static/flags/nld.png\"></a> "
          "<a href=\"/PSV\">PSV<span class=\"min481\"></span></a></td>', '1789', '+0.02', '1.10']];</script>")
    cr = W.parse_country_array(js)
    assert cr and cr[0]["slug"] == "PSV" and cr[0]["site_cc"] == "NED" and cr[0]["elo"] == 1789
    assert not W._is_country_href("/AEK") and W._is_country_href("/GRE") and W._is_country_href("/URU")


def test_reconstruction_fills_from_the_days_table_then_the_latest_api_snapshot(monkeypatch):
    monkeypatch.setattr(W, "_api_reference", lambda: ({"Liverpool": {"country": "ENG"}, "Auda": {"country": "LAT"}}, {}))
    hist = {"Liverpool": [{"date": "2026-08-20", "elo": 1903.4}]}
    nm = {"Liverpool": {"api": "Liverpool", "site": "Liverpool", "country_api": "ENG", "method": "norm"},
          "Aalesund": {"api": None, "site": "Aalesund", "country_api": "NOR", "method": None}}
    table = [{"slug": "Aalesund", "name": "Aalesund", "site_cc": "NOR", "rank": None, "elo": 1364}]
    api = [{"Rank": "", "Club": "Auda", "Country": "LAT", "Level": "", "Elo": "1100.0", "From": "x", "To": "x"},
           {"Rank": "", "Club": "Liverpool", "Country": "ENG", "Level": "", "Elo": "1910.8", "From": "x", "To": "x"}]
    rows, prov = W.reconstruct_date("2026-09-01", histories=hist, name_map=nm, frozen_api_rows=api, offset=66.0, table_rows=table)
    by = {r["Club"]: float(r["Elo"]) for r in rows}
    assert prov == {"Liverpool": "web_history", "Aalesund": "web_table", "Auda": "api_frozen_rescaled"}
    assert by["Liverpool"] == pytest.approx(1903.4) and by["Aalesund"] == 1364.0 and by["Auda"] == pytest.approx(1166.0)


@pytest.fixture(autouse=True, scope="module")
def _production_priors_untouched():
    """The tests in this module must never write into the production priors store. Snapshot
    every file (path, size, mtime) under data/priors before the module and compare after."""
    import os
    from prediction_market_soccer.config import CONFIG
    root = CONFIG.paths.priors

    def snap():
        out = {}
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[p] = (st.st_size, int(st.st_mtime))
        return out
    before = snap()
    yield
    after = snap()
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert not changed, f"tests wrote into the production priors store: {sorted(changed)[:5]}"


def test_validate_daily_rejects_a_broken_parse():
    good = [{"slug": s, "name": s, "site_cc": "ENG", "rank": i + 1, "elo": 2000 - i, "src": "world"}
            for i, s in enumerate(["Liverpool", "Real Madrid", "Bayern", "Arsenal", "Barcelona", "Man City", "Paris SG"])]
    good += [{"slug": f"c{i}", "name": f"Club {i}", "site_cc": "ENG", "rank": i + 8, "elo": 1900 - i, "src": "world"} for i in range(450)]
    good += [{"slug": f"k{i}", "name": f"Small {i}", "site_cc": cc, "rank": None, "elo": 1300, "src": f"country:{cc}"}
             for i, cc in enumerate(W.EURO_SITE_CODES)]
    assert W.validate_daily(good) == []
    assert any("world table" in p for p in W.validate_daily(good[:100]))                  # too few rows
    assert any("anchor club" in p for p in W.validate_daily([r for r in good if r["slug"] != "Bayern"]))
    bad = [dict(r) for r in good]; bad[3]["elo"] = 5                                     # a parse that read a rank as the Elo
    assert any("outside [900,2400]" in p for p in W.validate_daily(bad))
    bad = [dict(r) for r in good]; bad[5]["name"] = "<td>x</td>"
    assert any("HTML names" in p for p in W.validate_daily(bad))
    assert any("duplicate" in p for p in W.validate_daily(good + [good[0]]))


def test_store_paths_follow_the_patched_priors_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "PRIORS", tmp_path)
    assert W.DAILY == tmp_path / "clubelo_web" / "daily" and W.HIST.parent == tmp_path / "clubelo_web"


def test_to_api_rows_gives_every_club_a_unique_label(monkeypatch):
    monkeypatch.setattr(W, "_api_reference", lambda: ({}, {}))
    site_rows = [{"slug": "Guadalajara", "name": "Guadalajara", "site_cc": "MEX", "rank": None, "elo": 1600},
                 {"slug": "GuadalajaraESP", "name": "Guadalajara", "site_cc": "ESP", "rank": None, "elo": 1400}]
    rows = W.to_api_rows(site_rows, "2026-09-02", name_map={})
    labels = sorted(r["Club"] for r in rows)
    assert labels == ["Guadalajara (ESP)", "Guadalajara (MEX)"] and len(set(labels)) == 2
