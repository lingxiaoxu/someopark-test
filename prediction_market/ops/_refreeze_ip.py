"""One-off: DROP + rebuild settled_bet with the postevent in-play entry + pre-match hybrid,
then regenerate the 3 views (performance_report JSON+PDF, milestone_marks). Prints verification."""
from prediction_market.ingest import store
from prediction_market.ops.settle_bets import freeze_settled_bets
from prediction_market.ops import performance_report as PR
from prediction_market.ops import milestone_export as ME
from prediction_market.config import CONFIG
import json
from dataclasses import asdict

conn = store.init_db()
conn.execute("DROP TABLE IF EXISTS settled_bet")
conn.commit()
store.init_db()  # recreate schema (CREATE IF NOT EXISTS)
n = freeze_settled_bets(conn)
total = conn.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0]
ip_rows = conn.execute("SELECT COUNT(*) FROM settled_bet WHERE inplay_json IS NOT NULL").fetchone()[0]
print(f"re-froze {n} matches; {total} total; {ip_rows} with in-play entry")

# entry-minute distribution (should NOT cluster at 10 anymore)
from collections import Counter
mins = Counter()
for r in conn.execute("SELECT inplay_json FROM settled_bet WHERE inplay_json IS NOT NULL"):
    ip = json.loads(r["inplay_json"])
    mins[ip.get("entry_min")] += 1
print("in-play entry-min distribution:", dict(sorted(mins.items())))

# pre-match hybrid: count value vs argmax bets in the frozen ledger
kinds = Counter()
for r in conn.execute("SELECT payload FROM settled_bet"):
    mr = json.loads(r["payload"])
    kinds[mr.get("bet_kind")] += 1
print("pre-match bet_kind:", dict(kinds))

rep = PR.build(conn)
CONFIG.paths.ensure()
(CONFIG.paths.output / "performance_report.json").write_text(
    json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
print(f"realized (pre-match): {rep.realized_record}  {rep.realized_pnl_cents_total:+.1f}c")
print(f"in-play  (postevent): {rep.inplay_record}  {rep.inplay_pnl_cents_total:+.1f}c  (n={rep.n_inplay})")
print(f"COMBINED            : {rep.combined_pnl_cents_total:+.1f}c")
PR.build_pdf(rep, str(CONFIG.paths.output / "performance_report.pdf"))
print("pdf: ok")

doc = ME.build(conn)
(CONFIG.paths.output / "milestone_marks.json").write_text(
    json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
nip = sum(1 for m in doc["matches"] if m.get("inplay"))
print(f"milestone_marks: {doc['n']} matches, {nip} with in-play")
