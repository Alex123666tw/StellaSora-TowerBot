"""
擷取層降級鏈回歸測試 (Phase: 擷取 bug 修復)

實機證據:L3 session 20260613_144354 失敗,reason=state_unknown_persistent。
根因:遊戲視窗最小化時 capture_background()(PrintWindow)會回傳 result==1(成功)
但內容全黑的 buffer;舊版 capture() 只在拋例外時降級,對「成功但全黑」的 frame
直接 return,降級鏈永不觸發 → OCR 讀空 → 狀態判 UNKNOWN。

本測試以 monkeypatch 替換 WindowManager 實例上的三個 capture_* 方法
(不碰真 win32),驗證:
- 全黑 frame 視同失敗,降級到下一個方法 (test_falls_through_on_black)
- 正常 frame 行為不變,不去搶前景 (test_no_regression_on_normal)
- 三方法全空白 → raise (test_all_blank_raises)
- _is_blank_frame helper 本身判定正確 (test_is_blank_frame)
"""
import os

import cv2
import numpy as np
import pytest

from utils.window_mgr import WindowManager


def _imread_unicode(path: str):
    """Windows 上 cv2.imread 無法處理非 ASCII 路徑(本專案路徑含中文),
    改用 np.fromfile + cv2.imdecode 讀取。"""
    buf = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _black_frame() -> np.ndarray:
    """1080p 純黑 frame,模擬最小化視窗的 PrintWindow 輸出。"""
    return np.zeros((1080, 1920, 3), np.uint8)


def _gray_frame(value: int = 127) -> np.ndarray:
    """單一灰階 frame——注意這是「無變異」的,刻意用在 non-blank 的 spy 不適用。"""
    return np.full((1080, 1920, 3), value, np.uint8)


def _textured_frame() -> np.ndarray:
    """帶隨機紋理的 non-blank frame,std 遠大於門檻,代表合法遊戲畫面。"""
    rng = np.random.default_rng(1234)
    return rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8)


def _make_wm() -> WindowManager:
    """WindowManager.__init__ 只設 window_name/hwnd,不碰 win32,可直接建構。"""
    wm = WindowManager(window_name="StellaSora")
    wm.hwnd = 12345  # 假 hwnd,避免任何 find_window 路徑真的去找視窗
    return wm


# ---------------------------------------------------------------------------
# capture() 降級鏈
# ---------------------------------------------------------------------------

def test_falls_through_on_black(monkeypatch):
    """capture_background 回全黑 → 視同失敗,降級到 mss 前台截圖。"""
    wm = _make_wm()
    fg = _textured_frame()

    monkeypatch.setattr(wm, "capture_background", lambda: _black_frame())
    monkeypatch.setattr(wm, "capture_foreground", lambda: fg)

    def _dxcam_should_not_run():
        raise AssertionError("dxcam 不該被呼叫:foreground 已回傳合法 frame")

    monkeypatch.setattr(wm, "capture_dxcam", _dxcam_should_not_run)

    img, method = wm.capture()
    assert img is fg
    assert "mss" in method


def test_no_regression_on_normal(monkeypatch):
    """capture_background 回正常 frame → 直接 return PrintWindow 結果,
    完全不碰 foreground / dxcam(no-regression 證據)。"""
    wm = _make_wm()
    bg = _textured_frame()

    monkeypatch.setattr(wm, "capture_background", lambda: bg)

    def _foreground_spy():
        raise AssertionError("回歸:正常 PrintWindow frame 不該觸發 foreground 搶前景")

    def _dxcam_spy():
        raise AssertionError("回歸:正常 PrintWindow frame 不該觸發 dxcam")

    monkeypatch.setattr(wm, "capture_foreground", _foreground_spy)
    monkeypatch.setattr(wm, "capture_dxcam", _dxcam_spy)

    img, method = wm.capture()
    assert img is bg
    assert "PrintWindow" in method


def test_all_blank_raises(monkeypatch):
    """三個方法都回全黑(或拋例外)→ capture() 應 raise。"""
    wm = _make_wm()

    monkeypatch.setattr(wm, "capture_background", lambda: _black_frame())

    def _foreground_raises():
        raise Exception("mss 模擬失敗")

    monkeypatch.setattr(wm, "capture_foreground", _foreground_raises)
    monkeypatch.setattr(wm, "capture_dxcam", lambda: _black_frame())

    with pytest.raises(Exception):
        wm.capture()


def test_all_blank_raises_all_black(monkeypatch):
    """三個方法全部回全黑(無例外)→ capture() 應 raise。"""
    wm = _make_wm()
    monkeypatch.setattr(wm, "capture_background", lambda: _black_frame())
    monkeypatch.setattr(wm, "capture_foreground", lambda: _black_frame())
    monkeypatch.setattr(wm, "capture_dxcam", lambda: _black_frame())

    with pytest.raises(Exception):
        wm.capture()


# ---------------------------------------------------------------------------
# _is_blank_frame helper
# ---------------------------------------------------------------------------

def test_is_blank_frame():
    wm = _make_wm()

    # 全黑 → True
    assert wm._is_blank_frame(_black_frame()) is True

    # None → True
    assert wm._is_blank_frame(None) is True

    # size 0 → True
    assert wm._is_blank_frame(np.zeros((0, 0, 3), np.uint8)) is True

    # 隨機紋理 frame → False
    assert wm._is_blank_frame(np.random.randint(0, 255, (100, 100, 3), np.uint8)) is False

    # 真實彩色語料(實機截圖,std≈79)→ False
    real_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "logs", "session_failures", "20260531_195246", "last_frame.png",
    )
    if os.path.exists(real_path):
        real = _imread_unicode(real_path)
        assert real is not None
        assert wm._is_blank_frame(real) is False

    # 那張實機黑圖(std==0)→ True
    black_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "logs", "session_failures", "20260613_144354", "last_frame.png",
    )
    if os.path.exists(black_path):
        black = _imread_unicode(black_path)
        assert black is not None
        assert wm._is_blank_frame(black) is True
