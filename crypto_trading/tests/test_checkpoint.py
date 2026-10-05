"""Throttled checkpoints (2026-10-02 disk incident): material changes always reach
disk at once, volatile-only changes at most every `interval` seconds."""
from crypto_trading.crypto_common.checkpoint import Checkpoint


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(volatile, interval=60):
    written, clock = [], Clock()
    cp = Checkpoint(lambda path, st: written.append((path, repr(st))), volatile=volatile,
                    interval=interval, clock=clock)
    return cp, written, clock


def test_first_save_always_writes_then_volatile_only_is_skipped_until_due():
    cp, w, clock = make(("last_tick", "cycles"))
    st = {"episodes": {"a": 1}, "last_tick": 1, "cycles": 1}
    assert cp.save("p", st) and len(w) == 1
    for i in range(2, 50):
        clock.t = i
        st["last_tick"], st["cycles"] = i, i
        assert not cp.save("p", st)
    assert len(w) == 1
    clock.t = 60
    st["last_tick"] = 60
    assert cp.save("p", st) and len(w) == 2          # heartbeat still refreshed every interval


def test_material_change_is_written_immediately_with_current_volatile_fields():
    cp, w, clock = make(("last_tick",))
    st = {"episodes": {}, "last_tick": 0}
    cp.save("p", st)
    clock.t, st["last_tick"] = 3, 3
    st["episodes"]["x"] = {"decision": "accept"}
    assert cp.save("p", st)
    assert "'last_tick': 3" in w[-1][1] and "accept" in w[-1][1]


def test_force_writes_even_without_change():
    cp, w, clock = make(("last_tick",))
    st = {"a": 1, "last_tick": 0}
    cp.save("p", st)
    assert cp.save("p", st, force=True) and len(w) == 2


def test_wildcards_cover_dict_keys_and_list_elements_but_not_siblings():
    cp, w, clock = make(("books/*/positions/*/mid_history", "markets/*/orders/*/checked_ts"))
    st = {"books": {"tilted": {"positions": {"T1": {"mid_history": [1], "orders": [{"filled": 0}]}}}},
          "markets": {"T1": {"orders": [{"checked_ts": 1, "filled": 0}]}}}
    cp.save("p", st)
    st["books"]["tilted"]["positions"]["T1"]["mid_history"].append(2)
    st["markets"]["T1"]["orders"][0]["checked_ts"] = 2
    assert not cp.save("p", st)                       # only volatile leaves moved
    st["markets"]["T1"]["orders"][0]["filled"] = 5
    assert cp.save("p", st)                           # a fill next to a volatile leaf is material
    st["books"]["tilted"]["positions"]["T1"]["orders"][0]["filled"] = 1
    assert cp.save("p", st)


def test_strip_does_not_mutate_the_state():
    cp, w, clock = make(("a/b",))
    st = {"a": {"b": 1, "c": 2}}
    cp.material_digest(st)
    assert st == {"a": {"b": 1, "c": 2}}
