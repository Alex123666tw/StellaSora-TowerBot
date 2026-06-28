from __future__ import annotations

import ctypes
import os
import sys

ADMIN_REQUIRED_EXIT_CODE = 3


def is_windows_platform() -> bool:
    return os.name == "nt"


def is_windows_admin() -> bool:
    if not is_windows_platform():
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_admin_requirement_message(program_name: str = "星塔旅人 Bot") -> str:
    return (
        f"{program_name} 需要系統管理員權限才能進行 Windows 自動化。"
        "請以系統管理員身份重新啟動。"
        "建議使用視窗化或無邊框視窗化，並確保遊戲與 bot 的權限等級一致。"
    )


def require_windows_admin(program_name: str = "星塔旅人 Bot") -> None:
    if not is_windows_platform():
        return
    if not is_windows_admin():
        raise PermissionError(get_admin_requirement_message(program_name))


def exit_if_not_windows_admin(
    program_name: str = "星塔旅人 Bot",
    exit_code: int = ADMIN_REQUIRED_EXIT_CODE,
) -> None:
    try:
        require_windows_admin(program_name)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(exit_code) from exc
