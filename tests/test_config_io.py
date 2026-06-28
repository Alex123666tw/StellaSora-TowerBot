# tests/test_config_io.py
# §10 步3 ── gui/config_io 純函數測試（先紅後綠，不依賴 QApplication）
#
# 規格依據：GUI_DESIGN_SPEC §3、§10 步3
# 涵蓋：
#   - get_by_path / set_by_path（缺失路徑、巢狀建立）
#   - save 保留未知 key（set 一個 key 後其他 key 不變）
#   - enum 中英轉換 round-trip
#   - coerce_value_for_config 型別正規化（含壞值退 default）
#   - settings_for_settings_page == by_module 四模組過濾結果
#   - split_by_tier 常用/細項分組

from __future__ import annotations

import copy

import pytest

from gui import config_io
from gui.settings_schema import ALL_SETTINGS, by_key, by_module, Setting


# ─── get_by_path ──────────────────────────────────────────────────────────────

class TestGetByPath:
    def test_top_level_found(self):
        cfg = {"a": 1}
        assert config_io.get_by_path(cfg, "a") == (True, 1)

    def test_nested_found(self):
        cfg = {"shop": {"buy": {"strategy": "all"}}}
        assert config_io.get_by_path(cfg, "shop.buy.strategy") == (True, "all")

    def test_missing_top_level(self):
        assert config_io.get_by_path({"a": 1}, "b") == (False, None)

    def test_missing_nested(self):
        cfg = {"shop": {"buy": {}}}
        assert config_io.get_by_path(cfg, "shop.buy.strategy") == (False, None)

    def test_intermediate_not_dict(self):
        # 中途遇到非 dict（list / scalar）應視為找不到，而非丟例外
        cfg = {"shop": 5}
        assert config_io.get_by_path(cfg, "shop.buy.strategy") == (False, None)

    def test_value_falsy_still_found(self):
        # 值為 0 / False / "" / [] 都應回 found=True（區分「缺」與「值是 falsy」）
        cfg = {"a": 0, "b": False, "c": "", "d": []}
        assert config_io.get_by_path(cfg, "a") == (True, 0)
        assert config_io.get_by_path(cfg, "b") == (True, False)
        assert config_io.get_by_path(cfg, "c") == (True, "")
        assert config_io.get_by_path(cfg, "d") == (True, [])

    def test_empty_key(self):
        assert config_io.get_by_path({"a": 1}, "") == (False, None)

    def test_does_not_mutate(self):
        cfg = {"shop": {"buy": {"strategy": "all"}}}
        before = copy.deepcopy(cfg)
        config_io.get_by_path(cfg, "shop.buy.missing")
        assert cfg == before


# ─── set_by_path ──────────────────────────────────────────────────────────────

class TestSetByPath:
    def test_set_top_level_existing(self):
        cfg = {"a": 1}
        config_io.set_by_path(cfg, "a", 2)
        assert cfg["a"] == 2

    def test_set_nested_existing(self):
        cfg = {"shop": {"buy": {"strategy": "all"}}}
        config_io.set_by_path(cfg, "shop.buy.strategy", "cards_only")
        assert cfg["shop"]["buy"]["strategy"] == "cards_only"

    def test_creates_missing_intermediate(self):
        cfg = {}
        config_io.set_by_path(cfg, "shop.buy.strategy", "all")
        assert cfg == {"shop": {"buy": {"strategy": "all"}}}

    def test_creates_partial_missing(self):
        cfg = {"shop": {}}
        config_io.set_by_path(cfg, "shop.buy.strategy", "all")
        assert cfg["shop"]["buy"]["strategy"] == "all"

    def test_overwrites_non_dict_intermediate(self):
        # 中間層原是 scalar，set 深路徑應覆寫成 dict（以新路徑為準，不丟例外）
        cfg = {"shop": 5}
        config_io.set_by_path(cfg, "shop.buy.strategy", "all")
        assert cfg["shop"] == {"buy": {"strategy": "all"}}

    def test_returns_same_object(self):
        cfg = {}
        ret = config_io.set_by_path(cfg, "a.b", 1)
        assert ret is cfg

    def test_empty_key_noop(self):
        cfg = {"a": 1}
        config_io.set_by_path(cfg, "", 99)
        assert cfg == {"a": 1}


# ─── 保留未知 key（核心：讀-改-寫不掉沒碰的 key）────────────────────────────────

class TestPreserveUnknownKeys:
    def test_set_one_key_keeps_siblings(self):
        cfg = {
            "decision": {"mode": "legacy", "required": ["A", "B"]},
            "shop": {"buy": {"strategy": "all"}},
            "custom_user_key": {"nested": [1, 2, 3]},
            "top_scalar": 42,
        }
        before = copy.deepcopy(cfg)
        config_io.set_by_path(cfg, "decision.mode", "recommendation_badge")

        # 改的 key 生效
        assert cfg["decision"]["mode"] == "recommendation_badge"
        # 同層其餘 key 不變
        assert cfg["decision"]["required"] == before["decision"]["required"]
        # 其他完全沒碰的子樹原樣保留
        assert cfg["shop"] == before["shop"]
        assert cfg["custom_user_key"] == before["custom_user_key"]
        assert cfg["top_scalar"] == before["top_scalar"]

    def test_add_new_nested_keeps_unknown(self):
        cfg = {"unknown_block": {"x": 1}, "decision": {"mode": "legacy"}}
        config_io.set_by_path(cfg, "shop.refresh.start_from_visit", 3)
        assert cfg["unknown_block"] == {"x": 1}
        assert cfg["decision"] == {"mode": "legacy"}
        assert cfg["shop"]["refresh"]["start_from_visit"] == 3


# ─── enum 中英轉換 round-trip ──────────────────────────────────────────────────

class TestEnumConversion:
    def _enum_setting(self) -> Setting:
        s = by_key("decision.mode")
        assert s is not None and s.type == "enum"
        return s

    def test_stored_to_display(self):
        s = self._enum_setting()
        assert config_io.enum_to_display(s, "recommendation_badge") == "推薦徽章"
        assert config_io.enum_to_display(s, "legacy") == "累計模式"

    def test_display_to_stored(self):
        s = self._enum_setting()
        assert config_io.enum_to_stored(s, "推薦徽章") == "recommendation_badge"
        assert config_io.enum_to_stored(s, "累計模式") == "legacy"

    def test_round_trip_all_options_all_enums(self):
        # 每個 enum 的每個選項都能 英→中→英 與 中→英→中 還原
        enum_settings = [s for s in ALL_SETTINGS if s.type == "enum"]
        assert enum_settings, "schema 應至少有一個 enum 旋鈕"
        for s in enum_settings:
            for zh, en in s.options:
                assert config_io.enum_to_display(s, en) == zh, f"{s.key}: {en}→中文"
                assert config_io.enum_to_stored(s, zh) == en, f"{s.key}: {zh}→英文"
                # round-trip
                assert config_io.enum_to_stored(s, config_io.enum_to_display(s, en)) == en

    def test_unknown_value_falls_back_gracefully(self):
        s = self._enum_setting()
        # 舊 config 異常值不丟例外，回退字串形式
        assert config_io.enum_to_display(s, "totally_unknown") == "totally_unknown"
        assert config_io.enum_to_stored(s, "不存在的選項") == "不存在的選項"


# ─── coerce_value_for_config 型別正規化 ───────────────────────────────────────

class TestCoerceValue:
    def test_int_good(self):
        s = by_key("card_counter.target_total")
        assert config_io.coerce_value_for_config(s, 80) == 80
        assert config_io.coerce_value_for_config(s, "80") == 80

    def test_int_bad_falls_back_to_default(self):
        s = by_key("card_counter.target_total")
        assert config_io.coerce_value_for_config(s, "abc") == s.default

    def test_float_good(self):
        s = by_key("bot.poll_interval")
        assert config_io.coerce_value_for_config(s, "1.5") == 1.5
        assert config_io.coerce_value_for_config(s, 2) == 2.0

    def test_bool(self):
        s = by_key("shop.buy.affordability")
        assert config_io.coerce_value_for_config(s, True) is True
        assert config_io.coerce_value_for_config(s, 0) is False

    def test_enum_accepts_stored_value(self):
        s = by_key("decision.mode")
        assert config_io.coerce_value_for_config(s, "legacy") == "legacy"

    def test_enum_accepts_display_value(self):
        s = by_key("decision.mode")
        assert config_io.coerce_value_for_config(s, "累計模式") == "legacy"

    def test_list_from_string(self):
        s = by_key("decision.required")
        assert config_io.coerce_value_for_config(s, "A, B ,C") == ["A", "B", "C"]

    def test_list_from_list(self):
        s = by_key("decision.required")
        assert config_io.coerce_value_for_config(s, ["X", "Y"]) == ["X", "Y"]

    def test_int_toggle_coerces_like_int(self):
        # §0b 第2點:int_toggle（潛能門檻開關+數值）在 config 層就是 int（0=停用）。
        s = by_key("result.potential_total_threshold")
        assert s.type == "int_toggle"
        assert config_io.coerce_value_for_config(s, 220) == 220
        assert config_io.coerce_value_for_config(s, "220") == 220

    def test_int_toggle_bad_falls_back_to_default(self):
        s = by_key("result.potential_total_threshold")
        assert config_io.coerce_value_for_config(s, "abc") == s.default


# ─── list 文字解析 / 格式化 ───────────────────────────────────────────────────

class TestListText:
    def test_parse_basic(self):
        assert config_io.parse_list_text("A, B, C") == ["A", "B", "C"]

    def test_parse_strips_and_drops_empty(self):
        assert config_io.parse_list_text(" A , , B ,") == ["A", "B"]

    def test_parse_fullwidth_comma(self):
        assert config_io.parse_list_text("甲，乙，丙") == ["甲", "乙", "丙"]

    def test_parse_empty(self):
        assert config_io.parse_list_text("") == []

    def test_format_round_trip(self):
        items = ["爆裂追擊", "快拳連打"]
        assert config_io.parse_list_text(config_io.format_list_text(items)) == items

    def test_format_non_list(self):
        assert config_io.format_list_text(None) == ""
        assert config_io.format_list_text("x") == ""


# ─── 設定頁 render 範圍 == by_module 四模組 ───────────────────────────────────

class TestSettingsPageScope:
    def test_scope_equals_four_modules(self):
        result = config_io.settings_for_settings_page(ALL_SETTINGS)
        expected = (
            by_module("選卡") + by_module("商店")
            + by_module("事件") + by_module("結算")
        )
        assert result == expected

    def test_scope_excludes_run_and_advanced(self):
        result = config_io.settings_for_settings_page(ALL_SETTINGS)
        modules = {s.module for s in result}
        assert "執行" not in modules
        assert "進階" not in modules

    def test_scope_preserves_order(self):
        result = config_io.settings_for_settings_page(ALL_SETTINGS)
        # 在來源中的相對順序應保留
        indices = [ALL_SETTINGS.index(s) for s in result]
        assert indices == sorted(indices)

    def test_scope_custom_modules(self):
        result = config_io.settings_for_settings_page(ALL_SETTINGS, modules=("結算",))
        assert all(s.module == "結算" for s in result)
        assert result == by_module("結算")


# ─── split_by_tier 常用/細項 ──────────────────────────────────────────────────

class TestSplitByTier:
    def test_common_is_normal_only(self):
        settings = config_io.settings_for_settings_page(ALL_SETTINGS)
        common, detail = config_io.split_by_tier(settings)
        assert all(s.tier == "normal" for s in common)
        assert all(s.tier != "normal" for s in detail)

    def test_partition_is_complete(self):
        settings = config_io.settings_for_settings_page(ALL_SETTINGS)
        common, detail = config_io.split_by_tier(settings)
        assert len(common) + len(detail) == len(settings)
        # Setting 含 list/dict default → 不可 hash，故以 id 比對覆蓋完整性
        partition_ids = {id(s) for s in common} | {id(s) for s in detail}
        assert partition_ids == {id(s) for s in settings}

    def test_detail_holds_test_and_advanced(self):
        settings = config_io.settings_for_settings_page(ALL_SETTINGS)
        _common, detail = config_io.split_by_tier(settings)
        tiers = {s.tier for s in detail}
        # 四模組細項裡至少要有 test 或 advanced（schema 既有）
        assert tiers <= {"test", "advanced", "danger"}
        assert "test" in tiers or "advanced" in tiers


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
