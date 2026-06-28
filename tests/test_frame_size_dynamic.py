"""
Task 0 紅→綠測試 (tests/test_frame_size_dynamic.py)

解析度必須完全由「當前截圖」動態推導,絕不寫死。

真 bug:`BotContext.frame_w/h` 預設 1920×1080(bot.py:148-149),而主迴圈唯一
截圖點 `_detect_state`(bot.py:432-433)只設 `last_frame`,**沒有**同步更新
`frame_w/h`。於是在任何非 1920×1080 的解析度(如使用者實機的 480/~1024×576),
handler 會用 1920×1080 的比例去切一張較小的 frame → ROI/點擊座標越界/錯位 → 卡死。
這是「截圖尺寸不符預期」的真 bug,不是缺一個固定解析度。

修復前本檔應為紅:_detect_state 後 frame_w/h 仍是 1920×1080。
修復後:frame_w/h == 當前 frame 的 (w, h),且每拍都會追蹤變動。
"""
from __future__ import annotations

import unittest

import numpy as np

from core.bot import BotContext, StateMachine
from tests.fakes import (
    FakeDecisionEngine,
    FakeInput,
    FakeOCR,
    FakeWindowManager,
    FakeWindowManagerSequence,
)


def _build_machine(wm):
    """不經 __init__ 手工組裝 StateMachine;_detector=None 讓 _detect_state
    截圖後直接回傳(不依賴真 OCR / StateDetector)。"""
    ctx = BotContext(
        engine=FakeDecisionEngine(),
        config={"run": {"max_runs": 1}, "bot": {"poll_interval": 0.0}},
        wm=wm,
        input=FakeInput(),
        ocr=FakeOCR(),
        matcher=None,
        max_runs=1,
    )
    ctx.current_state = "STATE_LOBBY"

    machine = StateMachine.__new__(StateMachine)
    machine.ctx = ctx
    machine._config_path = "config.yaml"
    machine._poll_interval = 0.0
    machine._failure_finalized = False
    machine._detector = None
    return machine, ctx


class FrameSizeDynamicTests(unittest.TestCase):
    def test_default_frame_size_is_1920x1080(self) -> None:
        # 預設仍可保留(僅作首拍前的 sentinel),但截圖後必須被覆蓋。
        _machine, ctx = _build_machine(FakeWindowManager(np.zeros((720, 1280, 3), np.uint8)))
        self.assertEqual((ctx.frame_w, ctx.frame_h), (1920, 1080))

    def test_detect_state_updates_frame_size_from_capture(self) -> None:
        """餵 1280×720 frame,_detect_state 後 frame_w/h 必須變成 1280×720。"""
        frame = np.zeros((720, 1280, 3), np.uint8)
        machine, ctx = _build_machine(FakeWindowManager(frame))

        machine._detect_state()

        self.assertEqual(ctx.frame_w, 1280, "frame_w 未從當前截圖更新(仍是寫死的 1920)")
        self.assertEqual(ctx.frame_h, 720, "frame_h 未從當前截圖更新(仍是寫死的 1080)")

    def test_detect_state_tracks_changing_resolution(self) -> None:
        """跨兩拍解析度改變(1920×1080 → 1024×576)時 frame_w/h 必須逐拍追蹤。"""
        frame_a = np.zeros((1080, 1920, 3), np.uint8)
        frame_b = np.zeros((576, 1024, 3), np.uint8)
        wm = FakeWindowManagerSequence([frame_a, frame_b])
        machine, ctx = _build_machine(wm)

        machine._detect_state()
        self.assertEqual((ctx.frame_w, ctx.frame_h), (1920, 1080))

        machine._detect_state()
        self.assertEqual((ctx.frame_w, ctx.frame_h), (1024, 576))


if __name__ == "__main__":
    unittest.main()
