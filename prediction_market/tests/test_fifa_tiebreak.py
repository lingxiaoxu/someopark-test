"""FIFA group tie-break (criteria 4-6: head-to-head among teams level on points/GD/GF).

Mirrors the exact key computation in tournament.simulate so a regression in the formula
is caught: teams tied on (points, GD, GF) are separated by head-to-head points → h2h GD →
h2h GF, computed ONLY over the tied subset (the (pts,GD,GF) identity mask).
"""
import numpy as np


def _fifa_order(pts, gd, gf, hh_pts, hh_gd, hh_gf):
    """Replicates tournament.simulate's FIFA ranking key. Inputs shaped (n,4) / (n,4,4).
    Returns the team order per sim (col0 = 1st). lots set to 0 so ties are deterministic."""
    pid = (pts.astype(np.int64) * 100000 + (gd.astype(np.int64) + 200) * 100 + gf.astype(np.int64))
    same = pid[:, :, None] == pid[:, None, :]
    h2hp = np.einsum('nij,nij->ni', same, hh_pts)
    h2hd = np.einsum('nij,nij->ni', same, hh_gd)
    h2hf = np.einsum('nij,nij->ni', same, hh_gf)
    key = pts * 1e12 + gd * 1e10 + gf * 1e8 + h2hp * 1e6 + h2hd * 1e4 + h2hf * 1e2
    return np.argsort(-key, axis=1)


def _zeros_hh():
    return (np.zeros((1, 4, 4), dtype=np.int16) for _ in range(3))


def test_two_way_tie_broken_by_head_to_head():
    # teams 0 and 1 tied on pts/GD/GF; team0 beat team1 2-1 head-to-head → team0 ranks above.
    pts = np.array([[4, 4, 3, 1]], float)
    gd = np.array([[1, 1, 0, -2]], float)
    gf = np.array([[3, 3, 2, 1]], float)
    hh_pts, hh_gd, hh_gf = _zeros_hh()
    hh_pts[0, 0, 1] = 3;  hh_pts[0, 1, 0] = 0
    hh_gd[0, 0, 1] = 1;   hh_gd[0, 1, 0] = -1
    hh_gf[0, 0, 1] = 2;   hh_gf[0, 1, 0] = 1
    order = _fifa_order(pts, gd, gf, hh_pts, hh_gd, hh_gf)[0]
    assert list(order) == [0, 1, 2, 3], order   # team0 above team1 via head-to-head


def test_three_way_tie_uses_mini_table():
    # teams 0,1,2 all tied on pts/GD/GF; mini-table (only matches among 0,1,2):
    #   0 beat 1, 1 beat 2, 2 beat 0  → all 3 h2h pts equal → falls through to lots (random),
    # but team3 (clearly worse) must still finish last regardless.
    pts = np.array([[3, 3, 3, 0]], float)
    gd = np.array([[0, 0, 0, -3]], float)
    gf = np.array([[2, 2, 2, 0]], float)
    hh_pts, hh_gd, hh_gf = _zeros_hh()
    for a, b in ((0, 1), (1, 2), (2, 0)):
        hh_pts[0, a, b] = 3;  hh_pts[0, b, a] = 0
        hh_gd[0, a, b] = 1;   hh_gd[0, b, a] = -1
        hh_gf[0, a, b] = 1;   hh_gf[0, b, a] = 0
    order = _fifa_order(pts, gd, gf, hh_pts, hh_gd, hh_gf)[0]
    assert order[3] == 3, order   # the clearly-worse team is always 4th


def test_no_tie_pure_points_order():
    pts = np.array([[9, 6, 3, 0]], float)
    gd = np.array([[5, 2, -1, -6]], float)
    gf = np.array([[7, 4, 2, 1]], float)
    hh_pts, hh_gd, hh_gf = _zeros_hh()
    order = _fifa_order(pts, gd, gf, hh_pts, hh_gd, hh_gf)[0]
    assert list(order) == [0, 1, 2, 3], order
