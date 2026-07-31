"""jobs/watchdog_job.py — hourly independent watchdog (PLAN §8.2-3)."""
from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs.scheduler import watchdog

def main():
    s = load_settings()
    conn = init_db(s.db_path)
    missed = watchdog(conn)
    print(f"[watchdog] missed/breaches: {len(missed)}")

if __name__ == "__main__":
    main()
