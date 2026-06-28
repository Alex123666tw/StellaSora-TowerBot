import logging
import time

import win32api
import win32con
import win32gui

from utils.privilege import require_windows_admin

logger = logging.getLogger(__name__)


class InputSimulator:
    VALID_MODES = {"foreground", "auto"}

    def __init__(
        self,
        window_name: str = 'StellaSora',
        mode: str = 'foreground',
        prefer_background: bool | None = None,
        allow_non_admin: bool = False,
    ):
        self.window_name = window_name
        if prefer_background is not None and mode == 'foreground':
            mode = 'auto' if prefer_background else 'foreground'
        if mode not in self.VALID_MODES:
            raise ValueError(f'Unsupported input mode: {mode}')
        self.mode = mode
        self.hwnd: int = 0
        self._bg_mode_verified: bool | None = None
        self.allow_non_admin = allow_non_admin

    def _ensure_admin(self) -> None:
        if self.allow_non_admin:
            return
        require_windows_admin("星塔旅人 Bot")

    def attach(self) -> int:
        self._ensure_admin()
        hwnd = win32gui.FindWindow(None, self.window_name)
        if not hwnd:
            raise RuntimeError(f'Window not found: {self.window_name}')
        self.hwnd = hwnd
        logger.info(f'[Input] attached HWND={hwnd} ({self.window_name})')
        return hwnd

    def _ensure_hwnd(self) -> None:
        if not self.hwnd:
            self.attach()

    def click_background(self, x: int, y: int, delay: float = 0.05) -> bool:
        self._ensure_admin()
        self._ensure_hwnd()
        lparam = win32api.MAKELONG(int(x), int(y))
        try:
            win32gui.SendMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
            time.sleep(delay)
            win32gui.SendMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lparam)
            logger.debug(f'[Input] background click ({x}, {y})')
            return True
        except Exception as e:
            logger.warning(f'[Input] background click failed: {e}')
            return False

    def click_background_with_cursor(self, x: int, y: int, delay: float = 0.05) -> bool:
        self._ensure_admin()
        self._ensure_hwnd()
        client_x = int(x)
        client_y = int(y)
        try:
            orig_cursor_pos = win32api.GetCursorPos()
        except Exception:
            orig_cursor_pos = None

        try:
            screen_x, screen_y = win32gui.ClientToScreen(self.hwnd, (client_x, client_y))
            win32api.SetCursorPos((screen_x, screen_y))
            time.sleep(0.01)
        except Exception as e:
            logger.warning(f'[Input] SetCursorPos failed: {e}')
            return False

        lparam = win32api.MAKELONG(client_x, client_y)
        try:
            win32gui.SendMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
            time.sleep(delay)
            win32gui.SendMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lparam)
            logger.debug(
                f'[Input] background+cursor raw=({x},{y}) client=({client_x},{client_y}) screen=({screen_x},{screen_y})'
            )
            success = True
        except Exception as e:
            logger.warning(f'[Input] SendMessage click failed: {e}')
            success = False

        if orig_cursor_pos:
            try:
                win32api.SetCursorPos(orig_cursor_pos)
            except Exception:
                pass
        return success

    def click_foreground(self, x: int, y: int, delay: float = 0.05) -> bool:
        self._ensure_admin()
        self._ensure_hwnd()
        client_x = int(x)
        client_y = int(y)
        try:
            screen_x, screen_y = win32gui.ClientToScreen(self.hwnd, (client_x, client_y))
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
            time.sleep(0.05)

            import pydirectinput
            pydirectinput.FAILSAFE = False
            pydirectinput.moveTo(screen_x, screen_y)
            time.sleep(0.02)
            pydirectinput.mouseDown()
            time.sleep(delay)
            pydirectinput.mouseUp()
            logger.debug(
                f'[Input] foreground click raw=({x},{y}) client=({client_x},{client_y}) screen=({screen_x},{screen_y})'
            )
            return True
        except Exception as e:
            logger.warning(f'[Input] foreground click failed: {e}')
            return False

    def press_key(self, key: str, delay: float = 0.05) -> bool:
        """前景送鍵（pydirectinput / SendInput 掃描碼，遊戲讀得到）。

        遊戲讀 DirectInput/raw input，SendMessage WM_KEYDOWN 多半被忽略，
        故走與 click_foreground 相同的前置：SetForegroundWindow → pydirectinput.press。
        """
        self._ensure_admin()
        self._ensure_hwnd()
        try:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
            time.sleep(0.05)
            import pydirectinput
            pydirectinput.FAILSAFE = False
            pydirectinput.press(key)
            logger.debug(f'[Input] foreground key press {key!r}')
            return True
        except Exception as e:
            logger.warning(f'[Input] key press {key!r} failed: {e}')
            return False

    def press_esc(self, delay: float = 0.05) -> bool:
        return self.press_key('esc', delay)

    def click(self, x: int, y: int, delay: float = 0.05) -> bool:
        self._ensure_admin()
        if self.mode == 'foreground' or self._bg_mode_verified is False:
            return self.click_foreground(x, y, delay)

        success = self.click_background_with_cursor(x, y, delay)
        if not success:
            logger.warning('[Input] background click unavailable, fallback to foreground click.')
            self._bg_mode_verified = False
            return self.click_foreground(x, y, delay)

        self._bg_mode_verified = True
        return True
