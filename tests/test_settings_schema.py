# tests/test_settings_schema.py
# §10 步2 settings_schema 結構完整性 + key↔config + default 一致性測試
# 規格依據：GUI_DESIGN_SPEC §2、§6、§7

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

# ─── 合法集合 ─────────────────────────────────────────────────────────────────

VALID_TIERS = {"normal", "test", "advanced", "danger"}
VALID_TYPES = {
    "int", "float", "bool", "enum",
    "list", "dict", "dict-list", "list-of-list",
    "hotkey", "editor", "group",
    "int_toggle",  # §0b 第2點:開關 + 數值（0=停用,>0=啟用該值）
}

# ─── 豁免清單（對應 config 但路徑待後端補齊，或屬 GUI 專屬旋鈕）─────────────
# 格式：key -> 原因說明
CONFIG_EXEMPT_KEYS: dict[str, str] = {
    # 編輯器與群組類型不對應 config 路徑
    "event_rules": "editor 型別，對應 data/event_rules.yaml 而非 config.yaml 旋鈕",
    "shop.post_target.note_spree": "group 型別（含子欄 enabled/notes/max_spend），由子欄獨立對應 config",
}

# editor / group 型別本身不對應單一 config 路徑，同樣跳過 key 驗證
NON_CONFIG_TYPES = {"editor", "group"}


# ─── 載入 fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def schema_module():
    """載入 gui.settings_schema 模組。"""
    return importlib.import_module("gui.settings_schema")


@pytest.fixture(scope="module")
def all_settings(schema_module):
    """取得所有 Setting 條目清單。"""
    return schema_module.ALL_SETTINGS


@pytest.fixture(scope="module")
def config_data():
    """載入 config.yaml（以模組位置為錨點）。"""
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── 輔助函式 ─────────────────────────────────────────────────────────────────

def resolve_dot_path(data: dict, dot_path: str) -> tuple[bool, Any]:
    """用 dot 路徑解析 config dict。回傳 (found, value)。"""
    parts = dot_path.split(".")
    cur = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


# ─── (a) 結構完整性 ───────────────────────────────────────────────────────────

class TestStructuralIntegrity:
    """每條 Setting 必填欄齊全、tier/type 在合法集合內、enum 必有非空 options。"""

    def test_all_settings_not_empty(self, all_settings):
        assert len(all_settings) > 0, "ALL_SETTINGS 不能是空清單"

    @pytest.mark.parametrize("attr", ["key", "label", "type", "default", "module", "tier", "help"])
    def test_required_fields_present(self, schema_module, attr):
        """每條 Setting 都必須有指定欄位且不為 None（default 允許 None 僅限 editor 型）。"""
        Setting = schema_module.Setting
        for s in schema_module.ALL_SETTINGS:
            val = getattr(s, attr)
            if attr == "default" and s.type in ("editor", "group"):
                # editor/group 的 default 允許 None
                continue
            assert val is not None, (
                f"Setting(key={s.key!r}) 的 {attr!r} 欄位不得為 None"
            )

    def test_key_not_empty(self, all_settings):
        for s in all_settings:
            assert s.key and s.key.strip(), f"Setting key 不可為空（label={s.label!r}）"

    def test_label_not_empty(self, all_settings):
        for s in all_settings:
            assert s.label and s.label.strip(), f"Setting label 不可為空（key={s.key!r}）"

    def test_tier_valid(self, all_settings):
        for s in all_settings:
            assert s.tier in VALID_TIERS, (
                f"key={s.key!r} 的 tier={s.tier!r} 不在合法集合 {VALID_TIERS}"
            )

    def test_type_valid(self, all_settings):
        for s in all_settings:
            assert s.type in VALID_TYPES, (
                f"key={s.key!r} 的 type={s.type!r} 不在合法集合 {VALID_TYPES}"
            )

    def test_enum_has_options(self, all_settings):
        for s in all_settings:
            if s.type == "enum":
                assert s.options and len(s.options) > 0, (
                    f"enum 型 key={s.key!r} 必須有非空 options"
                )

    def test_help_not_empty(self, all_settings):
        for s in all_settings:
            assert s.help and s.help.strip(), (
                f"key={s.key!r} 的 help 欄位不得為空"
            )

    def test_module_not_empty(self, all_settings):
        for s in all_settings:
            assert s.module and s.module.strip(), (
                f"key={s.key!r} 的 module 欄位不得為空"
            )

    def test_total_count_reasonable(self, all_settings):
        """總條數應在合理範圍內（依 §2 共約 36 條）。"""
        assert len(all_settings) >= 30, (
            f"Setting 條數 {len(all_settings)} 偏少，預期 §2 至少 30 條"
        )


# ─── (b) key 對得上 config ────────────────────────────────────────────────────

class TestKeyMapsToConfig:
    """對「對應 config 的旋鈕」，dot path 在 config.yaml 中可解析到。"""

    def _should_skip(self, s) -> tuple[bool, str]:
        """回傳 (skip, reason)。"""
        if s.key in CONFIG_EXEMPT_KEYS:
            return True, CONFIG_EXEMPT_KEYS[s.key]
        if s.type in NON_CONFIG_TYPES:
            return True, f"type={s.type!r} 不對應 config 路徑"
        return False, ""

    def test_config_keys_resolvable(self, all_settings, config_data):
        failures = []
        skipped = []
        for s in all_settings:
            skip, reason = self._should_skip(s)
            if skip:
                skipped.append(f"  [豁免] {s.key}: {reason}")
                continue
            found, _ = resolve_dot_path(config_data, s.key)
            if not found:
                failures.append(f"  [找不到] key={s.key!r} (label={s.label!r})")

        # 印出豁免清單（資訊用）
        if skipped:
            print(f"\n豁免項（共 {len(skipped)} 條）：")
            for msg in skipped:
                print(msg)

        assert not failures, (
            f"以下 key 在 config.yaml 中找不到對應路徑：\n" + "\n".join(failures)
        )


# ─── (c) default 一致性 ───────────────────────────────────────────────────────

class TestDefaultConsistency:
    """schema.default == config.yaml 解析值（型別也對齊）。"""

    def _should_skip(self, s) -> tuple[bool, str]:
        if s.key in CONFIG_EXEMPT_KEYS:
            return True, CONFIG_EXEMPT_KEYS[s.key]
        if s.type in NON_CONFIG_TYPES:
            return True, f"type={s.type!r}"
        # list/dict/dict-list/list-of-list 型別允許「結構等效」而非 byte-identical
        # 因為 yaml.safe_load 的 list 與 Python list 已等效，此處一律比
        return False, ""

    def test_defaults_match_config(self, all_settings, config_data):
        failures = []
        for s in all_settings:
            skip, _ = self._should_skip(s)
            if skip:
                continue
            found, config_val = resolve_dot_path(config_data, s.key)
            if not found:
                # key 不在 config → 已由 test_config_keys_resolvable 捕捉
                continue

            schema_default = s.default
            # 型別對齊檢查：int vs float 容許（yaml 有時讀 int 但 schema 填 float）
            if config_val != schema_default:
                # 嘗試型別寬鬆比對
                try:
                    if type(config_val)(schema_default) == config_val:
                        continue
                except (TypeError, ValueError):
                    pass
                failures.append(
                    f"  key={s.key!r}: "
                    f"schema.default={schema_default!r} ({type(schema_default).__name__}) "
                    f"!= config={config_val!r} ({type(config_val).__name__})"
                )

        assert not failures, (
            "以下旋鈕的 schema.default 與 config.yaml 不一致（關鍵！）：\n"
            + "\n".join(failures)
        )


# ─── (d) options 雙向一致 ────────────────────────────────────────────────────

class TestOptionsConsistency:
    """enum 型 options 中文/英文值無重複、可中→英與英→中查回。"""

    def test_options_no_duplicate_labels(self, all_settings):
        for s in all_settings:
            if s.type != "enum" or not s.options:
                continue
            labels = [pair[0] for pair in s.options]
            assert len(labels) == len(set(labels)), (
                f"key={s.key!r} 的 options 中文 label 有重複：{labels}"
            )

    def test_options_no_duplicate_values(self, all_settings):
        for s in all_settings:
            if s.type != "enum" or not s.options:
                continue
            values = [pair[1] for pair in s.options]
            assert len(values) == len(set(values)), (
                f"key={s.key!r} 的 options 英文值有重複：{values}"
            )

    def test_options_bidirectional_lookup(self, all_settings, schema_module):
        """options 可中→英與英→中查回（雙向映射不歧義）。"""
        for s in all_settings:
            if s.type != "enum" or not s.options:
                continue
            zh_to_en = {pair[0]: pair[1] for pair in s.options}
            en_to_zh = {pair[1]: pair[0] for pair in s.options}
            # 確認對應表長度不因 key 衝突而縮水
            assert len(zh_to_en) == len(s.options), (
                f"key={s.key!r} 中文 label 衝突導致映射縮水"
            )
            assert len(en_to_zh) == len(s.options), (
                f"key={s.key!r} 英文值衝突導致映射縮水"
            )
            # 驗證來回一致
            for zh, en in s.options:
                assert zh_to_en[zh] == en
                assert en_to_zh[en] == zh


# ─── (e) 豁免清單文件化測試 ──────────────────────────────────────────────────

class TestExemptionDocumentation:
    """CONFIG_EXEMPT_KEYS 列出的豁免項應該在 ALL_SETTINGS 中存在，且不被遺忘。"""

    def test_exempt_keys_exist_in_schema(self, all_settings):
        schema_keys = {s.key for s in all_settings}
        missing = []
        for key, reason in CONFIG_EXEMPT_KEYS.items():
            if key not in schema_keys:
                missing.append(f"  豁免 key={key!r}（原因：{reason}）不在 ALL_SETTINGS 中")
        assert not missing, (
            "以下豁免 key 不在 schema 中（豁免清單過時）：\n" + "\n".join(missing)
        )


# ─── (f) 模組覆蓋度 ──────────────────────────────────────────────────────────

class TestModuleCoverage:
    """每個預期模組都有至少一條 Setting。"""

    EXPECTED_MODULES = {"選卡", "商店", "事件", "結算", "執行", "進階"}

    def test_all_modules_represented(self, all_settings):
        found_modules = {s.module for s in all_settings}
        missing = self.EXPECTED_MODULES - found_modules
        assert not missing, (
            f"以下模組在 ALL_SETTINGS 中沒有任何 Setting：{missing}"
        )

    def test_module_counts(self, all_settings, capsys):
        counts: dict[str, int] = {}
        for s in all_settings:
            counts[s.module] = counts.get(s.module, 0) + 1
        with capsys.disabled():
            print("\n各模組條數：")
            for mod, cnt in sorted(counts.items()):
                print(f"  {mod}: {cnt} 條")


# ─── (g) §0b 第二輪調整新增項 ────────────────────────────────────────────────

class TestSection0bAdditions:
    """§0b 第5點 商店補旋鈕 + 第2點 潛能門檻開關化。default 一致性由 TestDefaultConsistency
    泛型涵蓋（config 已有 buy_non_discounted/note_priority key）;本類釘住「存在 + 型別」。"""

    def test_buy_non_discounted_present(self, schema_module):
        s = schema_module.by_key("shop.buy.buy_non_discounted")
        assert s is not None, "schema 缺 shop.buy.buy_non_discounted（§0b 第5點）"
        assert s.type == "bool"
        assert s.default is True
        assert s.module == "商店"

    def test_note_priority_present(self, schema_module):
        s = schema_module.by_key("shop.buy.note_priority")
        assert s is not None, "schema 缺 shop.buy.note_priority（§0b 第5點）"
        assert s.type == "list"
        assert s.default == []
        assert s.module == "商店"

    def test_potential_total_threshold_is_int_toggle(self, schema_module):
        s = schema_module.by_key("result.potential_total_threshold")
        assert s is not None
        assert s.type == "int_toggle", "§0b 第2點:潛能加總門檻應為開關+數值（int_toggle）"
        assert s.default == 0
