"""達標後狂買特定音符 note_spree 測試（DECISION_CONFIG_PLAN step9/D，opt-in）。

驗收：
  1. 讀取器 _note_spree_cfg：預設 (False,[],0)；enabled+notes+max_spend 正常解析；
     壞型別退預設；enabled=true 但 notes 空 → (True, [], ...)（呼叫端仍 return False）。
  2. byte-identical：enabled=false → _try_note_spree return False 且**零 OCR**
     （mock ctx.ocr.read_text 計數驗證未被呼叫）。
  3. 生效：enabled+notes 命中 → 買該音符（click）；依清單順序；去重（已購格跳過）；
     affordability（買不起跳過）；max_spend（超上限不買 → return False）；
     進店重置（_try_enter_shop 把 shop_spree_spent 歸零）。

ctx 建構沿用 tests/test_shop_discount 的商店慣例（SimpleNamespace + FakeOCR(global_results) +
last_frame/frame_w/frame_h/shop_purchased_slots + FakeInput）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

import core.states as states
from tests.fakes import FakeInput, FakeOCR


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _cfg_ctx(config):
    """純讀取器測試用的最小 ctx（只需 config）。"""
    return SimpleNamespace(config=config)


# ── 1280x720 → _shop_goods_roi = (320, 72, 921, 518)；col_w=128 row_h=180 ──
#   「強攻之音」center (710, 291) → col(710-320)//128=3 row(291-72)//180=1 → slot "3:1"
#   「守護之音」center (710, 471) → col3 row(471-72)//180=2 → slot "3:2"
def _shop_ctx(config, *, money=9999, notes_on_shelf=None, purchased=None, spree_spent=0):
    """合成商店貨架 ctx。notes_on_shelf：[(text, cx, cy)] 列表，造 OCR token。"""
    notes_on_shelf = notes_on_shelf if notes_on_shelf is not None else [
        ("強攻之音 120 等級3", 710, 291),   # slot 3:1，price 120
        ("守護之音 200 等級2", 710, 471),   # slot 3:2，price 200
    ]
    results = []
    for text, cx, cy in notes_on_shelf:
        # bbox center == (cx, cy)：w=120 h=32 → 左上 (cx-60, cy-16)
        results.append((text, 1.0, _bbox(cx - 60, cy - 16, 120, 32)))
    return SimpleNamespace(
        ocr=FakeOCR(global_results=results),
        input=FakeInput(),
        last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
        frame_w=1280,
        frame_h=720,
        shop_purchased_slots=set(purchased or set()),
        shop_spree_spent=spree_spent,
        current_money=money,
        config=config,
    )


def _spree_config(enabled=True, notes=None, max_spend=0):
    return {
        "shop": {
            "post_target": {
                "note_spree": {
                    "enabled": enabled,
                    "notes": notes if notes is not None else ["強攻之音"],
                    "max_spend": max_spend,
                }
            }
        }
    }


class NoteSpreeCfgReaderTests(unittest.TestCase):
    """_note_spree_cfg 三層防呆 + 正常解析。"""

    def test_default_disabled(self) -> None:
        self.assertEqual(states._note_spree_cfg(_cfg_ctx({})), (False, [], 0))
        self.assertEqual(states._note_spree_cfg(_cfg_ctx({"shop": {}})), (False, [], 0))
        self.assertEqual(
            states._note_spree_cfg(_cfg_ctx({"shop": {"post_target": {}}})),
            (False, [], 0),
        )

    def test_enabled_false_explicit(self) -> None:
        cfg = _spree_config(enabled=False, notes=["強攻之音"], max_spend=500)
        self.assertEqual(states._note_spree_cfg(_cfg_ctx(cfg)), (False, [], 0))

    def test_enabled_parses_notes_and_max_spend(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音", "守護之音"], max_spend=500)
        enabled, notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertTrue(enabled)
        self.assertEqual(notes, ["強攻之音", "守護之音"])
        self.assertEqual(max_spend, 500)

    def test_notes_filter_blank_and_stringify(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音", "", "  ", 123], max_spend=0)
        enabled, notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertTrue(enabled)
        self.assertEqual(notes, ["強攻之音", "123"])
        self.assertEqual(max_spend, 0)

    def test_enabled_but_notes_empty(self) -> None:
        # enabled=true 但 notes 空 → enabled True、notes []（呼叫端 _try_note_spree 仍 return False）。
        cfg = _spree_config(enabled=True, notes=[], max_spend=100)
        enabled, notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertTrue(enabled)
        self.assertEqual(notes, [])
        self.assertEqual(max_spend, 100)

    def test_bad_types_fall_back(self) -> None:
        # note_spree 非 dict → 退預設。
        cfg = {"shop": {"post_target": {"note_spree": ["強攻之音"]}}}
        self.assertEqual(states._note_spree_cfg(_cfg_ctx(cfg)), (False, [], 0))
        # post_target 非 dict → 退預設。
        cfg2 = {"shop": {"post_target": "nope"}}
        self.assertEqual(states._note_spree_cfg(_cfg_ctx(cfg2)), (False, [], 0))

    def test_bad_notes_type_gives_empty_list(self) -> None:
        # notes 非 list → []（enabled 仍依 enabled 旗標）。
        cfg = {"shop": {"post_target": {"note_spree": {"enabled": True, "notes": "強攻之音"}}}}
        enabled, notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertTrue(enabled)
        self.assertEqual(notes, [])
        self.assertEqual(max_spend, 0)

    def test_bad_max_spend_falls_to_zero(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend="abc")
        _enabled, _notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertEqual(max_spend, 0)

    def test_negative_max_spend_clamped_to_zero(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=-50)
        _enabled, _notes, max_spend = states._note_spree_cfg(_cfg_ctx(cfg))
        self.assertEqual(max_spend, 0)


class TryNoteSpreeByteIdenticalTests(unittest.TestCase):
    """enabled=false（或 notes 空）→ return False 且零 OCR（byte-identical 佐證）。"""

    def _counting_ctx(self, config):
        ctx = _shop_ctx(config)
        ctx._ocr_calls = 0
        real_read = ctx.ocr.read_text

        def _counting_read(img, roi=None):
            ctx._ocr_calls += 1
            return real_read(img, roi=roi)

        ctx.ocr.read_text = _counting_read
        return ctx

    def test_disabled_returns_false_zero_ocr(self) -> None:
        ctx = self._counting_ctx({})  # 無 note_spree config → 預設關
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx._ocr_calls, 0)
        self.assertEqual(ctx.input.clicks, [])

    def test_enabled_false_explicit_zero_ocr(self) -> None:
        ctx = self._counting_ctx(_spree_config(enabled=False))
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx._ocr_calls, 0)
        self.assertEqual(ctx.input.clicks, [])

    def test_enabled_but_empty_notes_zero_ocr(self) -> None:
        # enabled=true 但 notes 空 → 快速退出、零 OCR（清單空保護）。
        ctx = self._counting_ctx(_spree_config(enabled=True, notes=[]))
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx._ocr_calls, 0)
        self.assertEqual(ctx.input.clicks, [])


class TryNoteSpreeEffectiveTests(unittest.TestCase):
    """enabled+notes 生效：買音符、清單順序、去重、affordability、max_spend。"""

    def test_buys_listed_note(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999)
        self.assertTrue(states._try_note_spree(ctx))
        # 點到「強攻之音」格 (710, 291)。
        self.assertIn((710, 291), ctx.input.clicks)
        # 該格標已購（去重）。
        self.assertIn("3:1", ctx.shop_purchased_slots)
        # 記本店狂買花費 = price 120。
        self.assertEqual(ctx.shop_spree_spent, 120)

    def test_list_order_respected(self) -> None:
        # 清單順序 [守護之音, 強攻之音] → 先買守護之音（即使它在貨架下方）。
        cfg = _spree_config(enabled=True, notes=["守護之音", "強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999)
        self.assertTrue(states._try_note_spree(ctx))
        self.assertIn((710, 471), ctx.input.clicks)   # 守護之音格
        self.assertIn("3:2", ctx.shop_purchased_slots)
        self.assertEqual(ctx.shop_spree_spent, 200)

    def test_dedup_skips_purchased_slot(self) -> None:
        # 強攻之音格 3:1 已在 purchased → 跳過；清單只此一音符 → return False、零 click。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999, purchased={"3:1"})
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_dedup_falls_through_to_next_list_note(self) -> None:
        # 強攻之音格已購 → 跳過 → 換清單下一個守護之音 → 買它。
        cfg = _spree_config(enabled=True, notes=["強攻之音", "守護之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999, purchased={"3:1"})
        self.assertTrue(states._try_note_spree(ctx))
        self.assertIn((710, 471), ctx.input.clicks)
        self.assertNotIn((710, 291), ctx.input.clicks)

    def test_unaffordable_skips(self) -> None:
        # 餘額 100 < 強攻之音價 120 → 買不起跳過 → 清單只此 → return False、零 click。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=100)
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_unaffordable_falls_through_to_cheaper_next(self) -> None:
        # 餘額 150：強攻之音 120 買得起會先買；改清單只放守護之音 200 → 買不起 → False。
        cfg = _spree_config(enabled=True, notes=["守護之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=150)
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_money_zero_does_not_filter(self) -> None:
        # money=0（讀不到餘額）→ 不套 affordability，照買（保守不誤跳）。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=0)
        self.assertTrue(states._try_note_spree(ctx))
        self.assertIn((710, 291), ctx.input.clicks)

    def test_max_spend_blocks_when_would_exceed(self) -> None:
        # max_spend=100，強攻之音價 120 → spent(0)+120 > 100 → 跳過 → 清單只此 → False。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=100)
        ctx = _shop_ctx(cfg, money=9999, spree_spent=0)
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_max_spend_allows_within_budget(self) -> None:
        # max_spend=300，強攻之音 120 → spent(0)+120 <= 300 → 買，spent 累計 120。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=300)
        ctx = _shop_ctx(cfg, money=9999, spree_spent=0)
        self.assertTrue(states._try_note_spree(ctx))
        self.assertEqual(ctx.shop_spree_spent, 120)

    def test_max_spend_accounts_prior_spend(self) -> None:
        # 已花 250、max_spend=300、價 120 → 250+120=370 > 300 → 跳過 → False。
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=300)
        ctx = _shop_ctx(cfg, money=9999, spree_spent=250)
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_max_spend_skip_then_buy_next_cheaper(self) -> None:
        # 清單 [強攻之音(120), 守護之音(200)]，max_spend=150：
        #   強攻之音 spent+120=120<=150 → 買它（先命中即買）。改驗證「貴的被擋、便宜的買」：
        #   清單 [守護之音(200), 強攻之音(120)]，max_spend=150 → 守護 200>150 擋，換強攻 120<=150 買。
        cfg = _spree_config(enabled=True, notes=["守護之音", "強攻之音"], max_spend=150)
        ctx = _shop_ctx(cfg, money=9999, spree_spent=0)
        self.assertTrue(states._try_note_spree(ctx))
        self.assertIn((710, 291), ctx.input.clicks)     # 強攻之音（便宜）被買
        self.assertNotIn((710, 471), ctx.input.clicks)  # 守護之音（貴）被擋
        self.assertEqual(ctx.shop_spree_spent, 120)

    def test_note_not_on_shelf_returns_false(self) -> None:
        # 清單音符不在貨架 → 掃完無命中 → False、零 click。
        cfg = _spree_config(enabled=True, notes=["不存在之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999)
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])

    def test_no_candidates_returns_false(self) -> None:
        cfg = _spree_config(enabled=True, notes=["強攻之音"], max_spend=0)
        ctx = _shop_ctx(cfg, money=9999, notes_on_shelf=[])
        self.assertFalse(states._try_note_spree(ctx))
        self.assertEqual(ctx.input.clicks, [])


class EnterShopResetsSpreeSpentTests(unittest.TestCase):
    """進店（_try_enter_shop verified 成功）把 shop_spree_spent 歸零（每進店重置）。"""

    def test_enter_shop_resets_spree_spent(self) -> None:
        import core.actions as actions
        import vision.signatures as signatures

        # 造一個 _try_enter_shop 會成功進店的 ctx：_should_enter_shop 過、選項命中、verified 成功。
        enter_token = signatures.SHOP_CHOICE_ENTER_OPTION_TOKENS[0]
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[(f"{enter_token}", 1.0, _bbox(600, 400, 120, 32))]),
            input=FakeInput(),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280,
            frame_h=720,
            shop_purchased_slots={"3:1", "3:2"},
            shop_spree_spent=999,
            current_money=9999,
            config={},
        )

        # _read_money_via_icon / _should_enter_shop / _select_shop_choice_option / click_verified
        # 在無真 wm 下行為複雜 → 直接 stub 成功路徑，聚焦驗證「進店後 spree_spent 歸零」。
        orig_should = states._should_enter_shop
        orig_money = states._read_money_via_icon
        orig_select = states._select_shop_choice_option
        orig_click = actions.click_verified
        try:
            states._should_enter_shop = lambda money: True
            states._read_money_via_icon = lambda c: 9999
            states._select_shop_choice_option = lambda c, keywords, trace_mode: (660, 416, enter_token)
            actions.click_verified = lambda c, target, *, expect, timeout=3.0, source="": True
            result = states._try_enter_shop(ctx, visit_count=1)
        finally:
            states._should_enter_shop = orig_should
            states._read_money_via_icon = orig_money
            states._select_shop_choice_option = orig_select
            actions.click_verified = orig_click

        self.assertEqual(result, "STATE_SHOP")
        self.assertEqual(ctx.shop_spree_spent, 0)          # 進店重置
        self.assertEqual(ctx.shop_purchased_slots, set())  # 既有：purchased 也清空


if __name__ == "__main__":
    unittest.main()
