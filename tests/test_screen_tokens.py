"""
畫面提示字 tokens 外部化測試 (tests/test_screen_tokens.py)

驗收 data/screen_tokens.yaml 的兩件事：
  (a) YAML 存在，且每個必要 token 組都載入成功且非空。
  (b) 「載入值 == 硬寫 fallback」回歸鎖：把 YAML 暫時藏起來重新 import
      vision.signatures，得到純 fallback 版本；再與正常（YAML 載入）版本逐組
      比對 —— 兩者必須完全相同。確保把 token 從 .py 搬進 YAML 的過程沒有漏字、
      錯字、漏組或排序跑掉。

設計：
  - 純資料 token 組才上這份測試（衍生/組合常數如 EXPLORE_COMPLETE_NEXT_TOKENS
    由原子組相加而成，留在 .py，不在外部化清單）。
  - 不初始化 EasyOCR、不讀語料圖；只比對 module 常數與 YAML / fallback。
"""
from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = PROJECT_ROOT / "data" / "screen_tokens.yaml"

# 已外部化到 data/screen_tokens.yaml 的純資料 token 組：
#   (signatures.py 的 module 常數名, YAML key)
EXTERNALIZED_TOKENS: tuple[tuple[str, str], ...] = (
    ("EVENT_CHOICE_HINTS", "event_choice_hints"),
    ("UPGRADE_EVENT_MARKER_TOKENS", "upgrade_event_marker_tokens"),
    ("UPGRADE_EVENT_RARE_REWARD_TOKENS", "upgrade_event_rare_reward_tokens"),
    ("POTENTIAL_SELECT_HINTS", "potential_select_hints"),
    ("POTENTIAL_CARD_HINTS", "potential_card_hints"),
    ("RECOMMEND_TEXT_TOKENS", "recommend_text_tokens"),
    ("SPACE_CONTINUE_HINT_TOKENS", "space_continue_hint_tokens"),
    ("CONCERT_SKILL_ACTIVATE_TOKENS", "concert_skill_activate_tokens"),
    ("POTENTIAL_SELECT_KEYWORDS", "potential_select_keywords"),
    ("SHOP_BUY_TOKENS", "shop_buy_tokens"),
    ("SHOP_PRICE_TOKENS", "shop_price_tokens"),
    ("SHOP_ITEM_TOKENS", "shop_item_tokens"),
    ("SHOP_CONTROL_TOKENS", "shop_control_tokens"),
    ("SHOP_LEAVE_TOKENS", "shop_leave_tokens"),
    ("SHOP_NOTE_GOODS_TOKENS", "shop_note_goods_tokens"),
    ("SHOP_REFRESH_TOKENS", "shop_refresh_tokens"),
    ("TAKE_BUTTON_TOKENS", "take_button_tokens"),
    ("REROLL_BUTTON_TOKENS", "reroll_button_tokens"),
    ("UPGRADE_HEADER_TOKENS", "upgrade_header_tokens"),
    ("UPGRADE_SINGLE_CARD_HEADER_TOKENS", "upgrade_single_card_header_tokens"),
    ("SHOP_CHOICE_UPGRADE_OPTION_TOKENS", "shop_choice_upgrade_option_tokens"),
    ("SHOP_UPGRADE_FREE_TOKENS", "shop_upgrade_free_tokens"),
    ("SHOP_CHOICE_ENTER_OPTION_TOKENS", "shop_choice_enter_option_tokens"),
    ("SHOP_CHOICE_SKIP_OPTION_TOKENS", "shop_choice_skip_option_tokens"),
    ("FAST_BATTLE_BUTTON_TOKENS", "fast_battle_button_tokens"),
    ("NEXT_STEP_BUTTON_TOKENS", "next_step_button_tokens"),
    ("PREPARE_START_BUTTON_TOKENS", "prepare_start_button_tokens"),
    ("RECONNECT_BUTTON_TOKENS", "reconnect_button_tokens"),
    ("SETTLEMENT_RETURN_TOKENS", "settlement_return_tokens"),
    ("LEAVE_TOWER_CONFIRM_TOKENS", "leave_tower_confirm_tokens"),
    ("CONFIRM_BUTTON_TOKENS", "confirm_button_tokens"),
    ("RESULT_SCREEN_MARKER_TOKENS", "result_screen_marker_tokens"),
    ("RESULT_SAVE_BUTTON_TOKENS", "result_save_button_tokens"),
    ("RESULT_LOCKED_TOKENS", "result_locked_tokens"),
    ("RESULT_UNLOCKED_TOKENS", "result_unlocked_tokens"),
    ("DISCARD_CONFIRM_TOKENS", "discard_confirm_tokens"),
)


class ScreenTokensYamlTests(unittest.TestCase):
    """(a) YAML 存在且每組載入非空。"""

    def test_yaml_file_exists(self) -> None:
        self.assertTrue(
            TOKENS_PATH.is_file(),
            f"找不到提示字外部資料檔：{TOKENS_PATH}",
        )

    def test_yaml_parses_to_mapping(self) -> None:
        data = yaml.safe_load(TOKENS_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict, "screen_tokens.yaml 應為 mapping（key → 字串清單）")

    def test_every_required_group_present_and_nonempty(self) -> None:
        data = yaml.safe_load(TOKENS_PATH.read_text(encoding="utf-8"))
        for _const_name, key in EXTERNALIZED_TOKENS:
            with self.subTest(key=key):
                self.assertIn(key, data, f"screen_tokens.yaml 缺少必要組「{key}」")
                value = data[key]
                self.assertIsInstance(value, list, f"「{key}」應為清單")
                self.assertTrue(value, f"「{key}」不得為空")
                self.assertTrue(
                    all(isinstance(x, str) for x in value),
                    f"「{key}」每個元素都必須是字串",
                )

    def test_module_constant_equals_yaml(self) -> None:
        """每個 module 常數 == YAML 中對應組（順序也須一致）。"""
        from vision import signatures

        data = yaml.safe_load(TOKENS_PATH.read_text(encoding="utf-8"))
        for const_name, key in EXTERNALIZED_TOKENS:
            with self.subTest(const=const_name):
                self.assertEqual(
                    getattr(signatures, const_name),
                    tuple(data[key]),
                    f"{const_name} 與 YAML「{key}」不一致",
                )


class ScreenTokensFallbackParityTests(unittest.TestCase):
    """(b) 回歸鎖：YAML 載入值 == 硬寫 fallback（搬移無漏字/錯字/漏組）。"""

    @staticmethod
    def _reload_with_token_path(path: str | None):
        """以指定 YAML 路徑（None=指向不存在檔）重新 import vision.signatures。"""
        import vision.signatures as sig_mod

        prev = os.environ.get("SCREEN_TOKENS_PATH")
        os.environ["SCREEN_TOKENS_PATH"] = path if path is not None else str(
            PROJECT_ROOT / "data" / "__does_not_exist__.yaml"
        )
        try:
            return importlib.reload(sig_mod)
        finally:
            if prev is None:
                os.environ.pop("SCREEN_TOKENS_PATH", None)
            else:
                os.environ["SCREEN_TOKENS_PATH"] = prev

    def test_loaded_values_equal_hardcoded_fallback(self) -> None:
        import vision.signatures as sig_mod

        # 正常版本（YAML 載入）
        loaded = {name: getattr(sig_mod, name) for name, _key in EXTERNALIZED_TOKENS}

        try:
            # 指向不存在的 YAML 後重新 import → 全部走 fallback。
            fallback_mod = self._reload_with_token_path(None)
            self.assertEqual(
                fallback_mod._TOKENS, {},
                "藏起 YAML 後 _TOKENS 應為空 → 全部常數走硬寫 fallback",
            )
            for name, _key in EXTERNALIZED_TOKENS:
                with self.subTest(const=name):
                    self.assertEqual(
                        loaded[name],
                        getattr(fallback_mod, name),
                        f"{name}：YAML 載入值與硬寫 fallback 不一致（搬移有漏字/錯字）",
                    )
        finally:
            # 還原（用正常路徑重新 import），避免污染後續測試。
            importlib.reload(sig_mod)

    def test_import_survives_missing_yaml(self) -> None:
        """YAML 不存在時 import 不得失敗，且常數仍非空（= fallback）。"""
        import vision.signatures as sig_mod

        try:
            reloaded = self._reload_with_token_path(None)
            self.assertTrue(reloaded.EVENT_CHOICE_HINTS)
            self.assertTrue(reloaded.SHOP_ITEM_TOKENS)
            # 衍生常數仍可由 fallback 原子組組出。
            self.assertIn("繼續", reloaded.EXPLORE_COMPLETE_NEXT_TOKENS)
        finally:
            importlib.reload(sig_mod)


if __name__ == "__main__":
    unittest.main()
