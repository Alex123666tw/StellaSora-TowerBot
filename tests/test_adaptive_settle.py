"""Phase 3③ adaptive settle 單測。

最高硬約束:adaptive_settle.enabled=false(或缺設定)時,_settle_and_refresh
必須逐位元等同舊行為 —— time.sleep(delay) 一次 + 重拍一次。其餘測試覆蓋啟用
路徑的提早返回、hash 變動重置、逾時下限、None hash 不算穩定、min_delay 下限。

設計:monkeypatch states.time 為一個可控時鐘(sleep 記錄引數並推進 now,
time() 回傳 now),frame 來源用各式假 WindowManager(capture() 回 (frame, method)),
讓 elapsed 完全由程式自身的 sleep 驅動、可確定性斷言。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import core.states as states


class FakeClock:
    """確定性時鐘:time() 回傳 now;sleep(dt) 記錄 dt 並推進 now。

    elapsed 只由被測程式自己呼叫的 sleep 推進,無真實等待、可重現。
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.now += dt


class FrameSequenceWM:
    """依呼叫順序回傳 frame;耗盡後固定回最後一張(模擬畫面靜止)。"""

    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.capture_calls = 0

    def capture(self):
        self.capture_calls += 1
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame, "fake"


class EverChangingWM:
    """每次 capture 回一張內容互異的 frame(模擬永不靜止的動畫)。"""

    def __init__(self) -> None:
        self.capture_calls = 0

    def capture(self):
        self.capture_calls += 1
        frame = np.full((4, 4, 3), self.capture_calls % 251, dtype=np.uint8)
        return frame, "fake"


class NullFrameWM:
    """每次 capture 回空陣列(size==0 → roi_hash_of 回 None)。"""

    def __init__(self) -> None:
        self.capture_calls = 0

    def capture(self):
        self.capture_calls += 1
        return np.zeros((0, 0, 3), dtype=np.uint8), "fake"


def _frame(value: int) -> np.ndarray:
    return np.full((4, 4, 3), value, dtype=np.uint8)


def _ctx(wm, adaptive: dict | None) -> SimpleNamespace:
    bot_cfg: dict = {}
    if adaptive is not None:
        bot_cfg["adaptive_settle"] = adaptive
    return SimpleNamespace(wm=wm, last_frame=None, config={"bot": bot_cfg})


class AdaptiveSettleTests(unittest.TestCase):
    # ── 鎖死等價性:預設關閉 = 舊行為 ───────────────────────────
    def test_disabled_is_exact_old_behavior(self) -> None:
        clock = FakeClock()
        wm = FrameSequenceWM([_frame(7)])
        ctx = _ctx(wm, adaptive=None)  # 無 adaptive_settle 區塊
        with patch.object(states, "time", clock):
            result = states._settle_and_refresh(ctx, delay=0.85)
        self.assertTrue(result)
        # time.sleep 必須以 delay 被呼叫「恰一次」
        self.assertEqual(clock.sleeps, [0.85])
        # 只重拍一次
        self.assertEqual(wm.capture_calls, 1)

    def test_disabled_explicit_false_is_old_behavior(self) -> None:
        clock = FakeClock()
        wm = FrameSequenceWM([_frame(7)])
        ctx = _ctx(wm, adaptive={"enabled": False, "stable_count": 2})
        with patch.object(states, "time", clock):
            result = states._settle_and_refresh(ctx, delay=0.85)
        self.assertTrue(result)
        self.assertEqual(clock.sleeps, [0.85])
        self.assertEqual(wm.capture_calls, 1)

    # ── 啟用路徑 ───────────────────────────────────────────────
    def test_stable_returns_early(self) -> None:
        clock = FakeClock()
        # 前 3 張整圖相同 → 應在遠少於 max_delay 的取樣內返回
        wm = FrameSequenceWM([_frame(5), _frame(5), _frame(5)])
        ctx = _ctx(wm, adaptive={
            "enabled": True, "stable_count": 2,
            "sample_interval": 0.1, "min_delay": 0.0,
        })
        with patch.object(states, "time", clock):
            result = states._settle_adaptive(ctx, max_delay=2.0, cfg=ctx.config["bot"]["adaptive_settle"])
        self.assertTrue(result)
        # 早退:遠少於 max_delay(2.0)/sample_interval(0.1)=20 拍
        self.assertLess(clock.now, 2.0)
        self.assertLessEqual(wm.capture_calls, 3)

    def test_hash_change_resets(self) -> None:
        clock = FakeClock()
        # [a, b, b, b]:第 2 拍變 → 不可在第 2 拍返回;b 連穩後才返回
        wm = FrameSequenceWM([_frame(1), _frame(2), _frame(2), _frame(2)])
        ctx = _ctx(wm, adaptive={
            "enabled": True, "stable_count": 2,
            "sample_interval": 0.1, "min_delay": 0.0,
        })
        with patch.object(states, "time", clock):
            result = states._settle_adaptive(ctx, max_delay=2.0, cfg=ctx.config["bot"]["adaptive_settle"])
        self.assertTrue(result)
        # 第 1 拍 a(baseline=a)、第 2 拍 b(重置 baseline=b,不返回)、
        # 第 3 拍 b(stable=2,返回)→ 至少 3 次 capture,不是在第 2 拍就早退
        self.assertGreaterEqual(wm.capture_calls, 3)
        self.assertLess(clock.now, 2.0)

    def test_timeout_floor(self) -> None:
        clock = FakeClock()
        wm = EverChangingWM()  # 每張都不同(動畫)→ 不早退
        ctx = _ctx(wm, adaptive={
            "enabled": True, "stable_count": 2,
            "sample_interval": 0.1, "min_delay": 0.0,
        })
        with patch.object(states, "time", clock):
            result = states._settle_adaptive(ctx, max_delay=1.0, cfg=ctx.config["bot"]["adaptive_settle"])
        self.assertTrue(result)
        # elapsed 必須達到 max_delay 才退場(動畫永不穩定)
        self.assertGreaterEqual(clock.now, 1.0)

    def test_none_hash_not_stable(self) -> None:
        clock = FakeClock()
        wm = NullFrameWM()  # roi_hash_of 連續回 None
        ctx = _ctx(wm, adaptive={
            "enabled": True, "stable_count": 2,
            "sample_interval": 0.1, "min_delay": 0.0,
        })
        with patch.object(states, "time", clock):
            states._settle_adaptive(ctx, max_delay=1.0, cfg=ctx.config["bot"]["adaptive_settle"])
        # None 不算穩定 → 不早退,必須跑到 max_delay
        self.assertGreaterEqual(clock.now, 1.0)

    def test_min_delay_floor(self) -> None:
        clock = FakeClock()
        # 全部相同,但 min_delay 較大 → 不可在 elapsed<min_delay 時提早返回
        wm = FrameSequenceWM([_frame(9)])
        ctx = _ctx(wm, adaptive={
            "enabled": True, "stable_count": 2,
            "sample_interval": 0.1, "min_delay": 0.5,
        })
        with patch.object(states, "time", clock):
            result = states._settle_adaptive(ctx, max_delay=2.0, cfg=ctx.config["bot"]["adaptive_settle"])
        self.assertTrue(result)
        # 返回時 elapsed 必須 >= min_delay(0.5);且不是因逾時(< max_delay 2.0)
        self.assertGreaterEqual(clock.now, 0.5)
        self.assertLess(clock.now, 2.0)


if __name__ == "__main__":
    unittest.main()
