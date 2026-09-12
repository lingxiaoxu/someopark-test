"""Read W8 evidence/heartbeat without running the strategy or trading."""
from __future__ import annotations
import json
import time
from crypto_trading.crypto_strategies.live_watch import common
from crypto_trading.crypto_strategies.live_watch.w8_complete_set import NAME
from crypto_trading.crypto_strategies.event_binary.complete_set import net_quantity


def report() -> dict:
    path = common.STATE_DIR / f"{NAME}_state.json"
    if not path.exists():
        return {"strategy": NAME, "status": "NOT_STARTED"}
    st = json.loads(path.read_text())
    age = time.time()-st.get("last_tick_ts", 0)
    books = {}
    for name, book in st["books"].items():
        clean = [r for r in book["trades"] if not r.get("coverage_gap")
                 and not r.get("unverified_order_quantity", 0)]
        windows = {}
        for row in book["trades"]:
            windows.setdefault(row["close_ts"], []).append(row)
        active = {ts: rows for ts, rows in windows.items() if any(r["fills"] for r in rows)}
        clean_windows = [rows for rows in active.values()
                         if {r["series"] for r in rows} == {"KXBTC15M", "KXETH15M"}
                         and all(not r.get("coverage_gap") and not r.get("unverified_order_quantity", 0)
                                 for r in rows)]
        open_marks_fresh = all(time.time()-st["inputs"].get(t, {}).get("last_book_ts", 0) <= 30
                               and st["inputs"].get(t, {}).get("interval_complete", False)
                               and not m.get("unverified_order_quantity", 0)
                               and time.time() < m["close_ts"]
                               and "last_mark" in m for t, m in book["positions"].items())
        open_mark = (sum(m["last_mark"]["net_usd"] for m in book["positions"].values())
                     if open_marks_fresh else None)
        books[name] = {"settled_markets": len(book["trades"]),
                       "settled_active_windows": len(active),
                       "clean_settled_markets": len(clean),
                       "clean_active_windows": len(clean_windows),
                       "clean_window_net_usd": sum(r["net_usd"] for rs in clean_windows for r in rs),
                       "cum_net_usd": book["cum_net_usd"],
                       "paired_net_usd": book["paired_net_usd"],
                       "residual_net_usd": book["residual_net_usd"],
                       "open_markets": len(book["positions"]),
                       "open_mark_net_usd": open_mark,
                       "total_model_net_usd": (book["cum_net_usd"]+open_mark if open_mark is not None else None),
                       "pending_settlement_markets": [t for t, m in book["positions"].items()
                                                      if time.time() >= m["close_ts"]],
                       "open_net_contracts": {t: net_quantity(m) for t, m in book["positions"].items()},
                       "paper_fills_in_open_markets": sum(len(m["fills"]) for m in book["positions"].values())}
    return dict(strategy=NAME, status=st["status"] if age < 30 else "STALE",
                heartbeat_age_s=round(age, 2), registered_at=st["registered_at"],
                execution_mode=st["execution_mode"], ticks=st["ticks"],
                books=books, coverage_gaps=len(st["gaps"]), verdict=st["verdict"],
                evidence_quality="COVERAGE_GAPS" if any(
                    b["clean_active_windows"] < b["settled_active_windows"] for b in books.values()) else "NO_KNOWN_GAPS",
                http_read_control=st.get("http_read_control"),
                last_cycle=st.get("last_cycle"),
                current_errors={k: v for k, v in st.get("last_cycle", {}).get("markets", {}).items()
                                if v.get("status") in ("DATA_ERROR", "DISCOVERY_STALE", "CATCHING_UP")},
                historical_error_keys=len(st.get("errors", {})))


def main():
    print(json.dumps(report(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
