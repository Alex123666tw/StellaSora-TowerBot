"""任務 §3.6（GUI_DESIGN_SPEC §0b 第5點）：shop.buy.buy_non_discounted 後端。

釘住 _select_shop_card_to_buy 的 buy_non_discounted 過濾語意：
  - 預設 True（或不設 / buy 非 dict）→ 也買非特價卡,行為與現行逐位元相同
    （byte-identical,選最上最左卡,即使它無優惠）。
  - False → 只買有優惠標記的卡,跳過非特價;全無優惠 → 回 None（不買原價）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

import core.states as states
from tests.fakes import FakeOCR


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _two_card_ctx(config=None, with_discount: bool = True) -> SimpleNamespace:
    """上格無優惠（流速紊亂 slot 3:1, center 710,291）;下格乾淨（風蝕環劫 slot 3:2,
    center 710,491）+ with_discount 時下格加「優惠」徽章 token → 下格為優惠格。
    1280x720 frame（沿用 test_shop_discount 慣例）。"""
    results = [
        ("流速紊亂 等級3", 1.0, _bbox(650, 275, 120, 32)),   # 上格（無優惠）slot 3:1
        ("風蝕環劫 等級2", 1.0, _bbox(650, 475, 120, 32)),   # 下格（乾淨）slot 3:2
    ]
    if with_discount:
        results.append(("優惠", 1.0, _bbox(770, 475, 36, 20)))  # 下格優惠徽章,仍 slot 3:2
    return SimpleNamespace(
        ocr=FakeOCR(global_results=results),
        last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
        frame_w=1280,
        frame_h=720,
        shop_purchased_slots=set(),
        config=config or {},
    )


class BuyNonDiscountedReaderTests(unittest.TestCase):
    def test_default_true(self) -> None:
        self.assertTrue(states._buy_non_discounted_enabled(SimpleNamespace(config={})))
        self.assertTrue(states._buy_non_discounted_enabled(SimpleNamespace(config={"shop": {"buy": {}}})))

    def test_non_dict_buy_is_true(self) -> None:
        self.assertTrue(states._buy_non_discounted_enabled(SimpleNamespace(config={"shop": {"buy": "x"}})))

    def test_explicit_false(self) -> None:
        ctx = SimpleNamespace(config={"shop": {"buy": {"buy_non_discounted": False}}})
        self.assertFalse(states._buy_non_discounted_enabled(ctx))

    def test_explicit_true(self) -> None:
        ctx = SimpleNamespace(config={"shop": {"buy": {"buy_non_discounted": True}}})
        self.assertTrue(states._buy_non_discounted_enabled(ctx))


class SelectShopCardBuyNonDiscountedTests(unittest.TestCase):
    def test_default_buys_topmost(self) -> None:
        # 預設 True → 選最上(710,291)流速紊亂,byte-identical（即使它無優惠）。
        ctx = _two_card_ctx(config={})
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, _lv, text = selected
        self.assertEqual((cx, cy), (710, 291))
        self.assertIn("流速紊亂", text)

    def test_default_no_discount_still_buys(self) -> None:
        # 預設 True + 全無優惠 → 仍買最上（現行,不因無優惠而不買）。
        ctx = _two_card_ctx(config={}, with_discount=False)
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, _lv, _text = selected
        self.assertEqual((cx, cy), (710, 291))

    def test_false_skips_non_discounted_picks_discounted(self) -> None:
        # False → 跳過上格無優惠 → 選下格(710,491)風蝕環劫（有優惠徽章）。
        ctx = _two_card_ctx(config={"shop": {"buy": {"buy_non_discounted": False}}})
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, _lv, text = selected
        self.assertEqual((cx, cy), (710, 491))
        self.assertIn("風蝕環劫", text)

    def test_false_no_discount_returns_none(self) -> None:
        # False + 全無優惠 → None（不買原價,寧可不買）。
        ctx = _two_card_ctx(config={"shop": {"buy": {"buy_non_discounted": False}}}, with_discount=False)
        self.assertIsNone(states._select_shop_card_to_buy(ctx))


if __name__ == "__main__":
    unittest.main()
