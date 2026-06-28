"""任務 B（GUI_DESIGN_SPEC §3.2）：result.potential_total_threshold 語意釘樁。

此後端已存在（_result_meets_target 的輔依據 OR）。本檔只加 regression test 釘住語意,
不改 states.py 程式：
  (1) rating 低於門檻（或讀不到）但 potential_total >= 門檻（>0）→ 達標（OR 生效）。
  (2) rating 與 potential_total 都低於各自門檻 → 不達標。
  (3) potential_total_threshold = 0 → 完全停用（potential_total 不影響判定）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.states as states


def _make_ctx(result_cfg: dict, satisfied: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        config={"result": result_cfg},
        required_potentials_satisfied=lambda: satisfied,
    )


class PotentialTotalThresholdTests(unittest.TestCase):
    def test_or_engages_when_rating_below_but_potential_meets(self) -> None:
        # 評分 27 < 門檻 30,但潛能總等級 56 >= 門檻 50 → OR 生效 → 達標。
        ctx = _make_ctx({"rating_threshold": 30, "potential_total_threshold": 50})
        self.assertTrue(states._result_meets_target(ctx, rating=27, potential_total=56))

    def test_or_engages_when_rating_unreadable_but_potential_meets(self) -> None:
        # 評分讀不到（rating=0,主依據停用）但潛能 56 >= 50 → 達標（不退回 required_potentials）。
        # required_potentials_satisfied 故意設 False,證明走的是潛能 OR 而非退回分支。
        ctx = _make_ctx({"rating_threshold": 30, "potential_total_threshold": 50}, satisfied=False)
        self.assertTrue(states._result_meets_target(ctx, rating=0, potential_total=56))

    def test_both_below_thresholds_discards(self) -> None:
        # 評分 27 < 30 且潛能 43 < 50 → 兩者皆未達 → 不達標。
        ctx = _make_ctx({"rating_threshold": 30, "potential_total_threshold": 50})
        self.assertFalse(states._result_meets_target(ctx, rating=27, potential_total=43))

    def test_threshold_zero_disables_potential(self) -> None:
        # potential_total_threshold = 0 → 潛能輔依據完全停用,僅看評分。
        ctx = _make_ctx({"rating_threshold": 30, "potential_total_threshold": 0})
        # 潛能很高（99）但門檻=0 停用 → 評分 27 < 30 仍不達標。
        self.assertFalse(states._result_meets_target(ctx, rating=27, potential_total=99))
        # 評分達標則達標（與潛能無關）。
        self.assertTrue(states._result_meets_target(ctx, rating=35, potential_total=0))


if __name__ == "__main__":
    unittest.main()
