"""reg_score: the 90' regulation score used to settle the 90' 3-way market.

A knockout that goes to extra time stores the ET-inclusive final in home_goals/
away_goals, but the Tie market settles on the 90' (score.fulltime) result. These
lock that a 1-1@90' finishing 2-1 AET settles 'Tie', not 'home'.
"""
import json

from prediction_market_soccer.util.pricing import reg_score


def _raw(ft_h, ft_a, et_h=None, et_a=None):
    return json.dumps({"score": {"fulltime": {"home": ft_h, "away": ft_a},
                                 "extratime": {"home": et_h, "away": et_a}}})


def test_extra_time_settles_on_90min():
    # 1-1 at 90', 2-1 after ET → home_goals/away_goals = 2/1, but 90' result is a draw.
    gh, ga = reg_score(_raw(1, 1, 2, 1), 2, 1)
    assert (gh, ga) == (1, 1)
    assert ("home" if gh > ga else ("draw" if gh == ga else "away")) == "draw"


def test_regulation_win_unchanged():
    # No ET — fulltime equals the stored final.
    assert reg_score(_raw(2, 0), 2, 0) == (2, 0)


def test_missing_fulltime_falls_back():
    assert reg_score(json.dumps({"score": {}}), 3, 1) == (3, 1)
    assert reg_score(None, 0, 0) == (0, 0)
    assert reg_score("not json", 1, 2) == (1, 2)


def test_accepts_dict_or_str():
    d = {"score": {"fulltime": {"home": 0, "away": 0}}}
    assert reg_score(d, 1, 0) == (0, 0)            # dict
    assert reg_score(json.dumps(d), 1, 0) == (0, 0)  # str
