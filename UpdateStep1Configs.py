"""
UpdateStep1Configs.py — Regenerate pair lists in Step 1 grid-search JSON configs
from pair_universe_mrpt.json and pair_universe_mtfs.json.

Run this whenever you change either universe file, BEFORE running Step 1 grid search.

Usage:
    python UpdateStep1Configs.py

Updates (in-place, preserving all other fields):
    run_configs/runs_20260304_step1_grid32.json   — MRPT
    run_configs/mtfs_runs_step1_grid30.json       — MTFS
"""

import json
import os
from pair_universe import mrpt_pairs, mtfs_pairs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MRPT_STEP1 = os.path.join(BASE_DIR, 'run_configs', 'runs_20260304_step1_grid32.json')
MTFS_STEP1 = os.path.join(BASE_DIR, 'run_configs', 'mtfs_runs_step1_grid30.json')


def update_pairs_in_config(config_path, new_pairs: list[list[str]]):
    with open(config_path) as f:
        cfg = json.load(f)
    for run in cfg['runs']:
        run['pairs'] = new_pairs
    with open(config_path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print(f'Updated {os.path.basename(config_path)}: {len(cfg["runs"])} runs × {len(new_pairs)} pairs')


def main():
    mrpt = [[s1, s2] for s1, s2 in mrpt_pairs()]
    mtfs = [[s1, s2] for s1, s2 in mtfs_pairs()]

    update_pairs_in_config(MRPT_STEP1, mrpt)
    update_pairs_in_config(MTFS_STEP1, mtfs)

    print('\nDone. Run Step 1 grid search next:')
    print('  python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json')
    print('  python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step1_grid30.json')


if __name__ == '__main__':
    main()
