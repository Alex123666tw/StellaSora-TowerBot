# gui/config_io.py
# §10 步3 ── 設定頁 config 讀寫 + 中英轉換的「純函數」層
#
# 規格依據：GUI_DESIGN_SPEC §3（設定頁兩欄 schema 驅動）、§10 步3（泛型 load/save）
#
# 設計目標（可單元測試、與 PyQt 完全解耦）：
#   1. dot-path 巢狀 get/set（get_by_path / set_by_path）——「讀-改-寫」保留未知 key 的基礎。
#   2. enum 中英雙向轉換（enum_to_display / enum_to_stored）——中文呈現、英文存底。
#   3. 控件值 ↔ config 值的型別正規化（coerce_value_for_config / parse_list_text / format_list_text）。
#   4. 「決定設定頁 render 哪些 Setting、分常用/細項」的純邏輯（settings_for_settings_page / split_by_tier）。
#
# 重要不變式（byte-identical 精神）：
#   - 未改任何旋鈕時，save 後 config.yaml 這些 key 值不變（schema.default == config 現值，已由
#     tests/test_settings_schema.py 驗證）。
#   - load-modify-write：save 一律先讀現有 config，再 set 特定 key，未碰的 key 原樣保留。

from __future__ import annotations

import copy
from typing import Any

from gui.settings_schema import Setting


# ─── (1) dot-path 巢狀 get/set ────────────────────────────────────────────────

def get_by_path(cfg: dict, dot_key: str) -> tuple[bool, Any]:
    """以 dot 路徑（如 ``shop.buy.strategy``）讀 cfg 巢狀值。

    回傳 ``(found, value)``：
      - 路徑完整存在 → ``(True, 值)``
      - 任一層缺失或中途遇到非 dict → ``(False, None)``
    不修改 cfg。
    """
    if not dot_key:
        return False, None
    cur: Any = cfg
    for part in dot_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def set_by_path(cfg: dict, dot_key: str, value: Any) -> dict:
    """以 dot 路徑就地寫入 cfg（巢狀路徑缺失時自動建立中間 dict）。

    回傳同一個（被就地修改的）cfg，方便串接。
    - 中間層不存在 → 建空 dict。
    - 中間層存在但非 dict（型別衝突）→ 覆寫成 dict（以新路徑為準）。
    其餘未碰到的 key 一律保留（這是「保留未知 key」的核心）。
    """
    if not dot_key:
        return cfg
    parts = dot_key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return cfg


# ─── (2) enum 中英雙向轉換 ────────────────────────────────────────────────────

def enum_to_display(setting: Setting, stored_value: Any) -> str:
    """enum 存底英文值 → 中文顯示字串。

    查不到對應時回退「該值的字串形式」（不丟例外，避免舊 config 異常值讓 GUI 崩）。
    """
    for zh, en in setting.options:
        if en == stored_value:
            return zh
    return str(stored_value)


def enum_to_stored(setting: Setting, display_value: str) -> Any:
    """enum 中文顯示字串 → 存底英文值。

    查不到對應時回退原字串（不丟例外）。
    """
    for zh, en in setting.options:
        if zh == display_value:
            return en
    return display_value


# ─── (3) 控件值 ↔ config 值的型別處理 ────────────────────────────────────────

def parse_list_text(text: str) -> list[str]:
    """LineEdit 逗號分隔字串 → 去空白、去空項的 list[str]。

    同時吃中英文逗號（「,」與「，」），與既有 save_config 的 split 行為相容。
    """
    if not text:
        return []
    normalized = text.replace("，", ",")
    return [x.strip() for x in normalized.split(",") if x.strip()]


def format_list_text(value: Any) -> str:
    """list → LineEdit 顯示字串（「, 」分隔）。非 list 一律回退空字串顯示再交給多行框。"""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return ""


def coerce_value_for_config(setting: Setting, raw: Any) -> Any:
    """把控件讀到的原始值正規化成寫入 config 的型別。

    - int   → int（壞值退 default）
    - float → float（壞值退 default）
    - bool  → bool
    - enum  → 已是存底英文值則原樣；若傳入中文顯示字串則轉英文
    - list  → list[str]（若傳入字串視為逗號分隔）
    其餘型別（dict / dict-list / list-of-list）→ 原樣回傳（本步用唯讀摘要，不在此轉）。
    """
    t = setting.type
    if t in ("int", "int_toggle"):
        # int_toggle（§0b 第2點:潛能門檻開關+數值）在 config 層就是 int（0=停用,>0=啟用該值）。
        try:
            return int(raw)
        except (TypeError, ValueError):
            return setting.default
    if t == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return setting.default
    if t == "bool":
        return bool(raw)
    if t == "enum":
        # 控件理想上回傳存底英文值（currentData）；保險起見也接受中文顯示字串。
        for _zh, en in setting.options:
            if raw == en:
                return raw
        return enum_to_stored(setting, raw)
    if t == "list":
        if isinstance(raw, str):
            return parse_list_text(raw)
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return list(setting.default) if isinstance(setting.default, list) else []
    return raw


# ─── (4) 設定頁 render 範圍 + 區域分組（純邏輯）────────────────────────────────

# 設定頁要 render 的模組（GUI_DESIGN_SPEC §10 步3：選卡/商店/事件/結算）。
# 「執行」「進階」不在本步：執行的 max_runs 由既有 control 卡的 spin_runs 處理；
# 進階模組留步4 進階頁。
SETTINGS_PAGE_MODULES: tuple[str, ...] = ("選卡", "商店", "事件", "結算")


def settings_for_settings_page(
    all_settings: list[Setting],
    modules: tuple[str, ...] = SETTINGS_PAGE_MODULES,
) -> list[Setting]:
    """從 ALL_SETTINGS 篩出「設定頁該 render」的 Setting（依模組過濾，保持原順序）。"""
    allowed = set(modules)
    return [s for s in all_settings if s.module in allowed]


def split_by_tier(settings: list[Setting]) -> tuple[list[Setting], list[Setting]]:
    """把一組 Setting 依 tier 分成 (常用區, 細項區)，各自保持原順序。

    區域對應（GUI_DESIGN_SPEC §3）：
      - 常用區：tier == 'normal'
      - 細項區：tier in ('test', 'advanced')
    本步四模組不含 'danger'（danger 全在進階模組，留步4）；若意外混入則歸細項區，不漏顯示。
    """
    common = [s for s in settings if s.tier == "normal"]
    detail = [s for s in settings if s.tier != "normal"]
    return common, detail


# 本步以「佔位、不接後端」處理的型別（顯示 label + 「（後續步驟）」禁用入口）。
PLACEHOLDER_TYPES: frozenset[str] = frozenset({"group", "editor", "hotkey"})

# 本步以「唯讀摘要 / 簡易多行文字框」處理的複合型別。
SUMMARY_TYPES: frozenset[str] = frozenset({"dict", "dict-list", "list-of-list"})
