"""
pair_universe.py — Single source of truth for trading pair configuration.

To change pairs for either strategy, edit ONLY:
    pair_universe_mrpt.json  — MRPT pairs (s1 = mean-revert long leg)
    pair_universe_mtfs.json  — MTFS pairs (s1 = momentum winner, s2 = momentum loser)

All scripts (MRPTUpdateConfigs, MRPTWalkForward, MTFSUpdateConfigs, MTFSWalkForward,
MRPTWalkForwardReport, MTFSWalkForwardReport, AuditPairs, MRPTFetchEarnings,
MRPTGenerateReport, MTFSGenerateReport, DailySignal, PortfolioMRPTRun,
PortfolioMTFSRun) load pairs via this module — no per-file changes needed when
swapping pairs.
"""

import json
import os
from functools import lru_cache

_BASE = os.path.dirname(os.path.abspath(__file__))
_MRPT_PATH = os.path.join(_BASE, 'pair_universe_mrpt.json')
_MTFS_PATH = os.path.join(_BASE, 'pair_universe_mtfs.json')


@lru_cache(maxsize=None)
def load_mrpt() -> list[dict]:
    """Return list of MRPT pair dicts: [{s1, s2, sector, z_col}, ...]"""
    with open(_MRPT_PATH) as f:
        return json.load(f)


@lru_cache(maxsize=None)
def load_mtfs() -> list[dict]:
    """Return list of MTFS pair dicts: [{s1, s2, sector, spread_col}, ...]"""
    with open(_MTFS_PATH) as f:
        return json.load(f)


def mrpt_pairs() -> list[tuple[str, str]]:
    """[(s1, s2), ...] for MRPT"""
    return [(p['s1'], p['s2']) for p in load_mrpt()]


def mtfs_pairs() -> list[tuple[str, str]]:
    """[(s1, s2), ...] for MTFS (reversed order vs MRPT)"""
    return [(p['s1'], p['s2']) for p in load_mtfs()]


def mrpt_pair_keys() -> list[str]:
    """['s1/s2', ...] for MRPT"""
    return [f"{p['s1']}/{p['s2']}" for p in load_mrpt()]


def mtfs_pair_keys() -> list[str]:
    """['s1/s2', ...] for MTFS"""
    return [f"{p['s1']}/{p['s2']}" for p in load_mtfs()]


def mrpt_z_col_map() -> dict[str, str]:
    """{'s1/s2': 'Z_sector', ...} for MRPT"""
    return {f"{p['s1']}/{p['s2']}": p['z_col'] for p in load_mrpt()}


def mtfs_spread_col_map() -> dict[str, str]:
    """{'s1/s2': 'Momentum_Spread_sector', ...} for MTFS"""
    return {f"{p['s1']}/{p['s2']}": p['spread_col'] for p in load_mtfs()}


def mrpt_sector_map() -> dict:
    """frozenset({s1, s2}) -> sector for MRPT (used in DailySignal, AuditPairs)"""
    from collections import defaultdict
    groups = defaultdict(set)
    for p in load_mrpt():
        groups[p['sector']].update({p['s1'], p['s2']})
    return {frozenset(tickers): sector for sector, tickers in groups.items()}


def mtfs_sector_map() -> dict:
    """frozenset({s1, s2}) -> sector for MTFS (used in DailySignal, AuditPairs)"""
    from collections import defaultdict
    groups = defaultdict(set)
    for p in load_mtfs():
        groups[p['sector']].update({p['s1'], p['s2']})
    return {frozenset(tickers): sector for sector, tickers in groups.items()}


def all_symbols() -> list[str]:
    """Deduplicated sorted list of all tickers across MRPT universe (for MRPTFetchEarnings)"""
    syms = set()
    for p in load_mrpt():
        syms.add(p['s1'])
        syms.add(p['s2'])
    return sorted(syms)


def sector_sets_mrpt() -> dict[str, set]:
    """{'tech': {'AAPL', 'META', ...}, ...} for PortfolioMRPTRun / PortfolioMTFSRun sector detection"""
    from collections import defaultdict
    groups = defaultdict(set)
    for p in load_mrpt():
        groups[p['sector']].update({p['s1'], p['s2']})
    return dict(groups)
