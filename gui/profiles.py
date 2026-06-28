# gui/profiles.py — 設定檔（profile）多套管理的純函數層（與 PyQt 解耦,可單元測試）。
# GUI_DESIGN_SPEC §10 步5 / §5。
#   profile 存 configs/<名稱>.yaml;config.yaml 是「當前生效」設定。
#   載入 profile = 把該 yaml 覆寫進 config.yaml（runtime 讀的）。
#   命名/導出/匯入讓使用者分享設定套組（開源後社群可交流）。

from __future__ import annotations

import re
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_PROFILES_DIR = _PROJECT_ROOT / "configs"

# 合法 profile 名：中英數 + 連字號 + 空白,1~40 字;擋路徑分隔/跳脫字元。
_VALID_NAME = re.compile(r"^[\w一-鿿\- ]{1,40}$")


def profiles_dir() -> Path:
    return _PROFILES_DIR


def is_valid_name(name: str) -> bool:
    return bool(name) and bool(_VALID_NAME.match(name.strip()))


def profile_path(name: str) -> Path:
    return _PROFILES_DIR / f"{name.strip()}.yaml"


def list_profiles() -> list[str]:
    """configs/ 下所有 profile 名（不含副檔名）,排序。"""
    if not _PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))


def save_profile(name: str, cfg: dict) -> Path:
    """把 cfg 寫成 configs/<name>.yaml。名稱非法 → ValueError。"""
    if not is_valid_name(name):
        raise ValueError(f"非法設定檔名稱: {name!r}")
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = profile_path(name)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return path


def load_profile(name: str) -> dict:
    """讀 configs/<name>.yaml 回 dict。不存在 → FileNotFoundError。"""
    path = profile_path(name)
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def delete_profile(name: str) -> bool:
    """刪除 profile;成功 True,本來就不存在 False。"""
    path = profile_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def apply_profile_to_config(name: str) -> dict:
    """載入 profile → 覆寫 config.yaml（當前生效）→ 回寫入的 cfg。"""
    cfg = load_profile(name)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return cfg


def import_profile(src_path: str, name: str) -> Path:
    """從外部 yaml 檔匯入成 configs/<name>.yaml。"""
    with open(src_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return save_profile(name, cfg)


def export_profile(name: str, dst_path: str) -> None:
    """把 configs/<name>.yaml 內容寫到外部路徑（分享用）。"""
    cfg = load_profile(name)
    with open(dst_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
