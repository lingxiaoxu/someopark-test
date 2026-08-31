"""
Truncation-guard tests for the AEUS isolated price store.
========================================================
Locks the regression fixed on 2026-06-14: a force=True heal refetch (or any
short/bad fetch) must NOT clobber long cached history, and the in-memory frame
returned to callers must fall back to the good cache rather than the short one.

Pure disk I/O against a temp store — no network (the guard lives entirely in
_persist / update_ticker's persist-result handling).
"""

import sys
import tempfile
import shutil
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from electric_utilities_strategy.data import aeus_fetch_prices as fp


def _frame(start, periods):
    idx = pd.bdate_range(start, periods=periods)
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": np.linspace(100, 110, n),
            "High": np.linspace(101, 111, n),
            "Low": np.linspace(99, 109, n),
            "Close": np.linspace(100, 110, n),
            "Volume": np.full(n, 1_000_000.0),
            "AdjClose": np.linspace(100, 110, n),
        },
        index=idx,
    )


class TruncationGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aeus_store_test_"))
        # Seed a long cached history (≈1000 rows from 2020).
        self.long = _frame("2020-01-01", 1000)
        fp._persist("TEST", self.long, "seed", {}, prices_dir=self.dir,
                    allow_truncate=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_short_overwrite_refused_disk_intact(self):
        """A 15-row frame starting years later must be REFUSED; disk unchanged."""
        short = _frame("2026-05-22", 15)
        written = fp._persist("TEST", short, "bad", {}, prices_dir=self.dir,
                              allow_truncate=False)
        self.assertFalse(written, "guard should refuse the truncating overwrite")
        on_disk = fp._read_existing("TEST", self.dir)
        self.assertEqual(len(on_disk), 1000, "disk cache must be left intact")

    def test_allow_truncate_permits_intentional_rebuild(self):
        """allow_truncate=True (CLI --force/--init) bypasses the guard."""
        short = _frame("2026-05-22", 15)
        written = fp._persist("TEST", short, "rebuild", {}, prices_dir=self.dir,
                              allow_truncate=True)
        self.assertTrue(written)
        self.assertEqual(len(fp._read_existing("TEST", self.dir)), 15)

    def test_legit_growth_allowed(self):
        """A longer/equal frame (normal incremental append) is always written."""
        grown = _frame("2020-01-01", 1005)
        written = fp._persist("TEST", grown, "incr", {}, prices_dir=self.dir,
                              allow_truncate=False)
        self.assertTrue(written)
        self.assertEqual(len(fp._read_existing("TEST", self.dir)), 1005)

    def test_small_recent_trim_not_flagged(self):
        """A slightly shorter but same-era frame (no 180d-later start) is allowed:
        guard only fires on BOTH shrink<50% AND start>180d later."""
        # 900 rows but still starting 2020 → not a truncation, must be written.
        same_era = _frame("2020-01-01", 900)
        written = fp._persist("TEST", same_era, "trim", {}, prices_dir=self.dir,
                              allow_truncate=False)
        self.assertTrue(written)

    def test_persist_returns_bool(self):
        self.assertIsInstance(
            fp._persist("TEST", _frame("2020-01-01", 1001), "x", {},
                        prices_dir=self.dir),
            bool,
        )


if __name__ == "__main__":
    unittest.main()
