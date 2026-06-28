"""任務 A（GUI_DESIGN_SPEC §3.1）：result.require_all_secrets 祕聞全解 AND 閘。

釘住 _result_meets_target 的 require_all_secrets 旋鈕語意：
  - 預設關（False）→ 行為與現行逐位元相同（評分達標即達標,不看音符）。
  - 開啟（True）→ 最終達標 = base AND ctx.current_notes_satisfied()
    （即使評分/潛能達標,協奏音符沒全達標也判不達標 → 走丟棄）。
  - target_notes 為空 / current_notes_satisfied 讀不到 → 保守判不達標。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.states as states


def _make_ctx(result_cfg: dict, **overrides) -> SimpleNamespace:
    ctx = SimpleNamespace(
        config={"result": result_cfg},
        required_potentials_satisfied=lambda: True,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class RequireAllSecretsTests(unittest.TestCase):
    def test_default_off_behaves_like_current(self) -> None:
        # 預設關（不設 require_all_secrets）：評分達標即達標,不看音符。
        ctx = _make_ctx(
            {"rating_threshold": 30},
            current_notes_satisfied=lambda: False,  # 音符未全達標,但預設關不應影響
        )
        self.assertTrue(states._result_meets_target(ctx, rating=42, potential_total=0))
        # 評分未達標仍不達標。
        self.assertFalse(states._result_meets_target(ctx, rating=10, potential_total=0))

    def test_on_rating_met_and_notes_all_met_keeps(self) -> None:
        # 開啟 + 評分達標 + 音符全達標 → 達標（keep True）。
        ctx = _make_ctx(
            {"rating_threshold": 30, "require_all_secrets": True},
            current_notes_satisfied=lambda: True,
        )
        self.assertTrue(states._result_meets_target(ctx, rating=42, potential_total=0))

    def test_on_rating_met_but_notes_not_all_met_discards(self) -> None:
        # 開啟 + 評分達標 + 音符未全達標 → 不達標（keep False,AND 閘否決）。
        ctx = _make_ctx(
            {"rating_threshold": 30, "require_all_secrets": True},
            current_notes_satisfied=lambda: False,
        )
        self.assertFalse(states._result_meets_target(ctx, rating=42, potential_total=0))

    def test_on_notes_unreadable_is_conservative_discard(self) -> None:
        # 開啟 + 評分達標,但 current_notes_satisfied 不存在於 ctx（舊測試 ctx）
        # → 保守判不達標。
        ctx = _make_ctx({"rating_threshold": 30, "require_all_secrets": True})
        # 沒有 current_notes_satisfied 屬性。
        self.assertFalse(hasattr(ctx, "current_notes_satisfied"))
        self.assertFalse(states._result_meets_target(ctx, rating=42, potential_total=0))


if __name__ == "__main__":
    unittest.main()
