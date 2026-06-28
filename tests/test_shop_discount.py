"""商店買卡優惠優先測試（DECISION_CONFIG_PLAN step7/c）。

驗收四件事：
  1. 提示字外部化：signatures.SHOP_DISCOUNT_TOKENS 存在、_has_discount_keyword 經 signatures 仍正確。
  2. config 讀取器 _prefer_discount_for_cards：預設 False；prefer_discount=true 時依 discount_scope。
  3. byte-identical：prefer_discount=false（預設）時 _select_shop_card_to_buy 選最上最左卡（同現行）。
  4. 優惠優先生效：prefer_discount=true + scope=cards 時，下方有優惠標記的卡格優先於上方無優惠卡格被選。

ctx 建構沿用 tests/test_state_replay 的商店慣例（SimpleNamespace + FakeOCR(global_results) +
last_frame/frame_w/frame_h/shop_purchased_slots）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

import core.states as states
import vision.signatures as signatures
from tests.fakes import FakeOCR


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _ctx(config):
    return SimpleNamespace(config=config)


class HasDiscountKeywordTests(unittest.TestCase):
    """_has_discount_keyword 經 signatures.SHOP_DISCOUNT_TOKENS 仍正確。"""

    def test_tokens_constant_exists(self) -> None:
        self.assertIn("優惠", signatures.SHOP_DISCOUNT_TOKENS)
        self.assertIn("折扣", signatures.SHOP_DISCOUNT_TOKENS)

    def test_discount_keyword_hit(self) -> None:
        self.assertTrue(states._has_discount_keyword("限時優惠價 120"))
        self.assertTrue(states._has_discount_keyword("折扣 80"))

    def test_discount_keyword_miss(self) -> None:
        self.assertFalse(states._has_discount_keyword("潛能特飲 等級1"))
        self.assertFalse(states._has_discount_keyword(""))


class PreferDiscountForCardsReaderTests(unittest.TestCase):
    """_prefer_discount_for_cards 三層防呆 + scope 語意。"""

    def test_default_false(self) -> None:
        self.assertFalse(states._prefer_discount_for_cards(_ctx({})))
        self.assertFalse(states._prefer_discount_for_cards(_ctx({"shop": {"buy": {}}})))

    def test_prefer_off_explicit(self) -> None:
        ctx = _ctx({"shop": {"buy": {"prefer_discount": False, "discount_scope": "cards"}}})
        self.assertFalse(states._prefer_discount_for_cards(ctx))

    def test_prefer_on_scope_cards(self) -> None:
        ctx = _ctx({"shop": {"buy": {"prefer_discount": True, "discount_scope": "cards"}}})
        self.assertTrue(states._prefer_discount_for_cards(ctx))

    def test_prefer_on_scope_all(self) -> None:
        ctx = _ctx({"shop": {"buy": {"prefer_discount": True, "discount_scope": "all"}}})
        self.assertTrue(states._prefer_discount_for_cards(ctx))

    def test_prefer_on_scope_notes_only_is_false_for_cards(self) -> None:
        # notes_only（預設）= 買卡不優先優惠（現行）。
        ctx = _ctx({"shop": {"buy": {"prefer_discount": True, "discount_scope": "notes_only"}}})
        self.assertFalse(states._prefer_discount_for_cards(ctx))

    def test_prefer_on_scope_default_is_notes_only(self) -> None:
        # prefer_discount=true 但未給 scope → 預設 notes_only → 買卡不優先。
        ctx = _ctx({"shop": {"buy": {"prefer_discount": True}}})
        self.assertFalse(states._prefer_discount_for_cards(ctx))


def _two_card_ctx(config=None):
    """合成兩格卡片：上方無優惠（流速紊亂，slot 3:1），下方有優惠（風蝕環劫，slot 3:2）。

    上格標題 center ≈ (710, 291)；下格標題 center ≈ (710, 491) + 同格獨立「優惠」徽章 token。
    模擬真實：優惠標記是與標題分開的 OCR token（紅圈徽章），不混進標題字
    （標題含「優惠」會被 _looks_like_shop_card_text 排除）。下格因徽章被 _shop_slot_discounted
    標為優惠格。frame 1280x720 → roi(320,72) col_w128 row_h180:
      上格 (710,291)→col3 row1=「3:1」;下格 (710,491)→col3 row2=「3:2」。
    """
    return SimpleNamespace(
        ocr=FakeOCR(global_results=[
            ("流速紊亂 等級3", 1.0, _bbox(650, 275, 120, 32)),    # 上格標題（無優惠）slot 3:1
            ("風蝕環劫 等級2", 1.0, _bbox(650, 475, 120, 32)),    # 下格標題（乾淨）slot 3:2
            ("優惠", 1.0, _bbox(770, 475, 36, 20)),               # 下格優惠徽章 token，仍 slot 3:2
        ]),
        last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
        frame_w=1280,
        frame_h=720,
        shop_purchased_slots=set(),
        config=config or {},
    )


class SelectShopCardByteIdenticalTests(unittest.TestCase):
    """prefer_discount=false（預設）→ 逐位元同現行（選最上最左卡）。"""

    def test_default_picks_topmost_card(self) -> None:
        ctx = _two_card_ctx(config={})  # 無 shop config → prefer 關
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, level, text = selected
        self.assertEqual((cx, cy), (710, 291))  # 上格（最上）
        self.assertIn("流速紊亂", text)

    def test_prefer_off_explicit_picks_topmost_card(self) -> None:
        ctx = _two_card_ctx(config={"shop": {"buy": {"prefer_discount": False}}})
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, _level, text = selected
        self.assertEqual((cx, cy), (710, 291))
        self.assertIn("流速紊亂", text)

    def test_prefer_on_notes_only_still_topmost(self) -> None:
        # scope=notes_only → 買卡不優先優惠 → 仍選最上。
        ctx = _two_card_ctx(config={"shop": {"buy": {"prefer_discount": True, "discount_scope": "notes_only"}}})
        selected = states._select_shop_card_to_buy(ctx)
        cx, cy, _level, text = selected
        self.assertEqual((cx, cy), (710, 291))
        self.assertIn("流速紊亂", text)


class SelectShopCardPreferDiscountTests(unittest.TestCase):
    """prefer_discount=true + scope=cards → 下方優惠卡優先於上方無優惠卡。"""

    def test_scope_cards_prefers_discounted_lower_card(self) -> None:
        ctx = _two_card_ctx(config={"shop": {"buy": {"prefer_discount": True, "discount_scope": "cards"}}})
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, level, text = selected
        self.assertEqual((cx, cy), (710, 491))  # 下格（有優惠）優先
        self.assertIn("風蝕環劫", text)

    def test_scope_all_prefers_discounted_lower_card(self) -> None:
        ctx = _two_card_ctx(config={"shop": {"buy": {"prefer_discount": True, "discount_scope": "all"}}})
        selected = states._select_shop_card_to_buy(ctx)
        cx, cy, _level, text = selected
        self.assertEqual((cx, cy), (710, 491))
        self.assertIn("風蝕環劫", text)


if __name__ == "__main__":
    unittest.main()
