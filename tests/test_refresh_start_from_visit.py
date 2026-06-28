"""任務 §3.3（GUI_DESIGN_SPEC §3.3）：shop.refresh.start_from_visit 後端守衛。

釘住 _refresh_trigger_allows 的 start_from_visit 語意：
  - 不設 / 設 1（預設）→ 守衛不啟用,行為與現行 trigger 邏輯逐位元相同（byte-identical）。
  - start_from_visit=N（N>1）→ shop_visit_count < N 時一律不刷（回 False）,
    且守衛位於 trigger 判斷之前（即使 when_gap 有缺口也不刷）。
  - shop_visit_count >= N → 守衛放行,落回既有 trigger 判斷。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.states as states


def _make_ctx(refresh_cfg: dict, visit: int, **overrides) -> SimpleNamespace:
    ctx = SimpleNamespace(
        config={'shop': {'refresh': refresh_cfg}},
        shop_visit_count=visit,
        target_notes={},
        current_notes={},
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class RefreshStartFromVisitTests(unittest.TestCase):
    def test_default_unset_byte_identical(self) -> None:
        # 不設 start_from_visit + trigger=exhausted → 任何造訪次數都允許（現行）。
        for visit in (1, 2, 5):
            ctx = _make_ctx({'trigger': 'exhausted'}, visit)
            self.assertTrue(states._refresh_trigger_allows(ctx))

    def test_default_one_byte_identical(self) -> None:
        # 明設 start_from_visit=1（預設值）→ 守衛不啟用,第一次就可刷。
        ctx = _make_ctx({'trigger': 'exhausted', 'start_from_visit': 1}, 1)
        self.assertTrue(states._refresh_trigger_allows(ctx))

    def test_blocks_visits_before_threshold(self) -> None:
        # start_from_visit=3 + 第 2 次造訪 + trigger=exhausted（本會 True）→ 守衛擋下不刷。
        ctx = _make_ctx({'trigger': 'exhausted', 'start_from_visit': 3}, 2)
        self.assertFalse(states._refresh_trigger_allows(ctx))

    def test_allows_at_and_after_threshold(self) -> None:
        # start_from_visit=3 + 第 3、4 次造訪 + exhausted → 放行。
        for visit in (3, 4):
            ctx = _make_ctx({'trigger': 'exhausted', 'start_from_visit': 3}, visit)
            self.assertTrue(states._refresh_trigger_allows(ctx))

    def test_guard_precedes_trigger(self) -> None:
        # 守衛優先於 trigger：第 2 次造訪 + start_from_visit=3 + when_gap 有缺口
        # （本會 True）→ 仍因 visit 不足回 False。
        ctx = _make_ctx(
            {'trigger': 'when_gap', 'start_from_visit': 3}, 2,
            target_notes={'風': 45}, current_notes={},
        )
        self.assertFalse(states._refresh_trigger_allows(ctx))


if __name__ == "__main__":
    unittest.main()
