"""任務 §3.4（GUI_DESIGN_SPEC §3.4）：shop.order_by_visit per-visit 三選一順序。

釘住 _shop_order(ctx, visit_count) 語意（比照 _shop_upgrade_times per-visit dict）：
  - 不設 / 空 order_by_visit / visit_count=None → 退全域 shop.order（byte-identical）。
  - order_by_visit 命中該次造訪 → 用該次指定順序（int / str key 都比對）。
  - 命中但值無效 → 退全域。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.states as states


def _make_ctx(shop_cfg: dict) -> SimpleNamespace:
    return SimpleNamespace(config={'shop': shop_cfg})


class ShopOrderByVisitTests(unittest.TestCase):
    def test_global_when_no_override(self) -> None:
        # 不設 order_by_visit → 退全域 order。
        self.assertEqual(states._shop_order(_make_ctx({'order': 'upgrade_first'}), 1), 'upgrade_first')
        self.assertEqual(states._shop_order(_make_ctx({'order': 'shop_first'}), 1), 'shop_first')

    def test_empty_order_by_visit_byte_identical(self) -> None:
        # 空 dict（config 預設）→ 退全域。
        ctx = _make_ctx({'order': 'upgrade_first', 'order_by_visit': {}})
        self.assertEqual(states._shop_order(ctx, 2), 'upgrade_first')

    def test_visit_none_uses_global(self) -> None:
        # visit_count=None（舊呼叫相容）→ 不查 per-visit,退全域。
        ctx = _make_ctx({'order': 'upgrade_first', 'order_by_visit': {2: 'shop_first'}})
        self.assertEqual(states._shop_order(ctx), 'upgrade_first')

    def test_per_visit_override_int_key(self) -> None:
        # order_by_visit 命中 → 用該次順序;未命中的次數退全域。
        ctx = _make_ctx({'order': 'upgrade_first', 'order_by_visit': {2: 'shop_first'}})
        self.assertEqual(states._shop_order(ctx, 2), 'shop_first')
        self.assertEqual(states._shop_order(ctx, 1), 'upgrade_first')

    def test_per_visit_override_str_key(self) -> None:
        # YAML 的 key 可能是字串 → int visit 也要比對到。
        ctx = _make_ctx({'order': 'upgrade_first', 'order_by_visit': {'2': 'shop_first'}})
        self.assertEqual(states._shop_order(ctx, 2), 'shop_first')

    def test_invalid_override_falls_back(self) -> None:
        # 命中但值無效 → 退全域。
        ctx = _make_ctx({'order': 'shop_first', 'order_by_visit': {2: 'garbage'}})
        self.assertEqual(states._shop_order(ctx, 2), 'shop_first')


if __name__ == "__main__":
    unittest.main()
