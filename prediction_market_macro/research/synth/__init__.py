"""research/synth — DFM-generated samples for parameter selection.

Design: `docs/PLAN_DFM_SYNTH.md`. The short version: `param_argmin` picks a parameter set
by argmin of realised PnL over the quotable events of the last 75 days, and on the monthly
series that sample is 2-3 events, which supports almost no search (see
`param_argmin.sample_cap`). This package manufactures the missing sample with the
diffusion factor model in `dfm/`, conditioned on the current macro environment.

`dfm/` is CALLED, never modified.
"""
