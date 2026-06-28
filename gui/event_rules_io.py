# gui/event_rules_io.py — 事件規則編輯器的讀寫純函數層（GUI_DESIGN_SPEC §10 步8 / §4）。
#   讀寫 data/event_rules.yaml 的 overrides（使用者自訂事件覆蓋規則）;
#   與 core/states._load_event_rules（bot 端讀取）共用同一檔。
#   override 結構：{id, match_any:[...], pick_any:[...], note}。

from __future__ import annotations

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RULES_PATH = _PROJECT_ROOT / "data" / "event_rules.yaml"


def normalize_override(o: dict) -> dict:
    """正規化一條 override：補齊 id/match_any/pick_any/note、去空白、濾空項。"""
    return {
        "id": str(o.get("id", "") or "").strip(),
        "match_any": [str(x).strip() for x in (o.get("match_any") or []) if str(x).strip()],
        "pick_any": [str(x).strip() for x in (o.get("pick_any") or []) if str(x).strip()],
        "note": str(o.get("note", "") or "").strip(),
    }


def is_valid_override(o: dict) -> bool:
    """有效規則 = id 非空 + match_any 非空 + pick_any 非空。"""
    n = normalize_override(o)
    return bool(n["id"] and n["match_any"] and n["pick_any"])


def load_overrides() -> list:
    """讀 data/event_rules.yaml 的 overrides（正規化）。檔案缺/格式錯 → 空 list,絕不拋。"""
    if not _RULES_PATH.exists():
        return []
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return []
    ov = (data or {}).get("overrides", []) if isinstance(data, dict) else []
    if not isinstance(ov, list):
        return []
    return [normalize_override(o) for o in ov if isinstance(o, dict)]


def save_overrides(overrides: list) -> None:
    """把 overrides（正規化後）寫回 data/event_rules.yaml,保留順序（=優先序）。"""
    _RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = [normalize_override(o) for o in overrides if isinstance(o, dict)]
    with open(_RULES_PATH, "w", encoding="utf-8") as f:
        f.write("# 使用者自訂事件覆蓋規則（GUI 事件編輯器寫入）。比對命中 → 直接選指定選項;\n")
        f.write("# 未命中 → 落回現有 strategy 評分。順序 = 優先序。\n")
        yaml.dump({"overrides": clean}, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
