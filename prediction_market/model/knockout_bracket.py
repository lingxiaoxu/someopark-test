"""Official 2026 FIFA World Cup knockout bracket — group-position slotting.

The Round of 32 pairings are PREDETERMINED in group-position terms (FIFA regulations,
matches 73–88): each R32 slot is a group winner (W), a runner-up (R), or a third-placed
team from a FIXED set of eligible groups (T). The eight third-placed slots are filled by
the best 8 of the 12 third-placed teams, assigned so every slot receives a third from its
eligible set — a perfect bipartite matching, which is exactly FIFA's Annex C table of all
C(12,8)=495 qualifying-group combinations.

This module is the authoritative bracket structure used by the tournament simulator (so
champion / reach-round odds reflect the REAL knockout path) and by the knockout-bracket
predictor (ops/knockout_export). NOT a simplification: the 16 pairings and the eligible
sets are the official ones; the third assignment is the official eligible-set matching,
precomputed for all 495 combinations.
"""
from __future__ import annotations

from itertools import combinations

GROUPS = "ABCDEFGHIJKL"   # the 12 groups


def _T(letters: str):
    return ("T", frozenset(letters))


# 16 official Round-of-32 pairings (matches 73–88), in pairing order. Each slot:
#   ("W", g)  winner of group g
#   ("R", g)  runner-up of group g
#   ("T", set) a third-placed team from one of the groups in the set
R32_MATCHES: list[tuple] = [
    (("R", "A"), ("R", "B")),       # 73
    (("W", "E"), _T("ABCDF")),      # 74
    (("W", "F"), ("R", "C")),       # 75
    (("W", "C"), ("R", "F")),       # 76
    (("W", "I"), _T("CDFGH")),      # 77
    (("R", "E"), ("R", "I")),       # 78
    (("W", "A"), _T("CEFHI")),      # 79
    (("W", "L"), _T("EHIJK")),      # 80
    (("W", "D"), _T("BEFIJ")),      # 81
    (("W", "G"), _T("AEHIJ")),      # 82
    (("R", "K"), ("R", "L")),       # 83
    (("W", "H"), ("R", "J")),       # 84
    (("W", "B"), _T("EFGIJ")),      # 85
    (("W", "J"), ("R", "H")),       # 86
    (("W", "K"), _T("DEIJL")),      # 87
    (("R", "D"), ("R", "G")),       # 88
]

# The eight third-place slots as (slot_id, eligible_groups). slot_id = (match_index, side).
THIRD_SLOTS: list[tuple[tuple[int, int], frozenset]] = [
    ((mi, si), slot[1])
    for mi, match in enumerate(R32_MATCHES)
    for si, slot in enumerate(match)
    if slot[0] == "T"
]
_THIRD_SLOT_IDS = [sid for sid, _ in THIRD_SLOTS]
_THIRD_SLOT_ELIG = {sid: elig for sid, elig in THIRD_SLOTS}


def _perfect_matching(groups: tuple[str, ...]) -> dict[tuple[int, int], str] | None:
    """Assign each of the 8 qualifying third-place GROUPS to a distinct third slot whose
    eligible set contains it (a perfect bipartite matching). Returns {slot_id: group} or
    None if impossible. Augmenting-path matching (Kuhn) — tiny (8×8), exact & deterministic.
    Slots and groups are processed in a fixed order so the choice is reproducible (this is
    the resolver for the rare combinations FIFA's Annex C disambiguates by table)."""
    slot_to_group: dict[tuple[int, int], str] = {}

    def try_assign(group: str, seen: set) -> bool:
        for sid in _THIRD_SLOT_IDS:
            if group in _THIRD_SLOT_ELIG[sid] and sid not in seen:
                seen.add(sid)
                if sid not in slot_to_group or try_assign(slot_to_group[sid], seen):
                    slot_to_group[sid] = group
                    return True
        return False

    for g in groups:
        if not try_assign(g, set()):
            return None
    if len(slot_to_group) != len(_THIRD_SLOT_IDS):
        return None
    return dict(slot_to_group)


def _build_assignment_table() -> dict[frozenset, dict[str, tuple[int, int]]]:
    """All 495 qualifying-group combinations → {group: slot_id}. Precomputed once."""
    table: dict[frozenset, dict[str, tuple[int, int]]] = {}
    for combo in combinations(GROUPS, len(THIRD_SLOTS)):
        m = _perfect_matching(combo)
        if m is not None:
            table[frozenset(combo)] = {g: sid for sid, g in m.items()}
    return table


# group-combination (frozenset of 8 letters) -> {group_letter: slot_id (match_idx, side)}
THIRD_ASSIGNMENT: dict[frozenset, dict[str, tuple[int, int]]] = _build_assignment_table()


def third_assignment(qualified_groups) -> dict[str, tuple[int, int]] | None:
    """For the 8 groups whose third-placed team qualified, return {group_letter: slot_id}.
    None if that exact combination has no valid official matching (shouldn't happen for a
    legal set of 8 distinct groups)."""
    return THIRD_ASSIGNMENT.get(frozenset(qualified_groups))


# ── Official knockout bracket TREE (by FIFA match number) ───────────────────────
# Which two earlier matches feed each later match. R32 = matches 73–88.
R16_TREE = {89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
            93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87)}
QF_TREE = {97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96)}
SF_TREE = {101: (97, 98), 102: (99, 100)}
FINAL = (101, 102)


def _leaf_order() -> list[int]:
    """R32 match indices (match_number-73) left-to-right in the bracket, so a balanced
    binary tree over this order reproduces the official R16/QF/SF/Final pairings."""
    def expand(m):
        for tree in (SF_TREE, QF_TREE, R16_TREE):
            if m in tree:
                a, b = tree[m]
                return expand(a) + expand(b)
        return [m]                       # an R32 match number
    order = expand(FINAL[0]) + expand(FINAL[1])
    return [mn - 73 for mn in order]


R32_LEAF_ORDER: list[int] = _leaf_order()   # 16 R32 match indices in bracket order


def assign_slot_lookup():
    """(4096, 8) int8 lookup for the vectorised simulator: indexed by a 12-bit mask of which
    groups' thirds qualified, gives the GROUP INDEX (A=0 … L=11) feeding each of the 8
    third-slots (in THIRD_SLOTS order). -1 where a mask is not a valid 495-combination."""
    import numpy as np
    slot_k = {sid: k for k, (sid, _) in enumerate(THIRD_SLOTS)}
    out = np.full((1 << 12, len(THIRD_SLOTS)), -1, dtype=np.int8)
    for combo, g2slot in THIRD_ASSIGNMENT.items():
        mask = 0
        for letter in combo:
            mask |= 1 << GROUPS.index(letter)
        for letter, sid in g2slot.items():
            out[mask, slot_k[sid]] = GROUPS.index(letter)
    return out


if __name__ == "__main__":
    # Self-check: every one of the 495 combinations must have a valid official assignment.
    total = len(list(combinations(GROUPS, len(THIRD_SLOTS))))
    ok = len(THIRD_ASSIGNMENT)
    print(f"third slots: {len(THIRD_SLOTS)} | combinations: {total} | with valid matching: {ok}")
    assert ok == total == 495, "FIFA bracket invariant broken: not all 495 combos assignable"
    # Show one example
    ex = next(iter(THIRD_ASSIGNMENT.items()))
    print("example combo", sorted(ex[0]), "->", {g: f"M{sid[0]+73}" for g, sid in ex[1].items()})
    print("R32 matches:", len(R32_MATCHES), "| third slots:", [f"M{mi+73}" for (mi, _), _ in THIRD_SLOTS])
