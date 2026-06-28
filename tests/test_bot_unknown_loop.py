"""
Phase 1.2 主迴圈 STATE_UNKNOWN 分支測試 (tests/test_bot_unknown_loop.py)

R2 紅測試（先紅後綠）：用 FakeOCR/FakeInput 餵雜訊 OCR 模擬連續 UNKNOWN，
斷言：
  - UNKNOWN 期間不執行任何 handler、點擊數 == 0
  - 連續 2 次起呼叫 _settle_and_refresh 重判
  - 連續 N 次（config bot.max_unknown_streak，預設 4）→
    finalize_failure("state_unknown_persistent")
  - UNKNOWN 與 evidence 寫入 state_trace；ctx.current_state 不被 UNKNOWN 覆蓋

v1 行為（未命中維持原狀態）下，雜訊畫面會讓 handler 持續在錯誤畫面上
動作（盲點擊），本檔測試在修復前必須是紅的。
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import core.bot as bot
import core.states as states
from core.bot import BotContext, StateMachine
from tests.fakes import FakeDecisionEngine, FakeInput, FakeOCR, FakeWindowManager
from vision.state_detector import StateDetector

# 取自 unknown__20260531_195529__last（IDE 視窗）的代表性雜訊 OCR
NOISE_TEXTS = [
    "上一個輪次v",
    "core states.py",
    "無法載入完整檔案內容",
    "1136",
    "def handle lobby( ctx:",
    "ACTIOMABLE STATES",
]

LOBBY_TEXTS = ["星塔探索", "難度 2", "快速戰鬥", "出發"]

_FAKE_WIN32GUI = SimpleNamespace(IsWindow=lambda hwnd: True)


def _build_machine(ocr: FakeOCR, *, max_unknown_streak: int = 4, max_polls: int = 12):
    """不經 __init__（避免真 OCR/win32）手工組裝 StateMachine + 全 Fake 硬體層。"""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    wm = FakeWindowManager(frame)
    fake_input = FakeInput()
    ctx = BotContext(
        engine=FakeDecisionEngine(),
        config={
            "run": {"max_runs": 1},
            "bot": {"poll_interval": 0.0, "max_unknown_streak": max_unknown_streak},
        },
        wm=wm,
        input=fake_input,
        ocr=ocr,
        matcher=None,
        max_runs=1,
    )
    ctx.current_state = "STATE_POTENTIAL_SELECT"

    machine = StateMachine.__new__(StateMachine)
    machine.ctx = ctx
    machine._config_path = "config.yaml"
    machine._poll_interval = 0.0
    machine._failure_finalized = False
    machine._detector = StateDetector(ocr_engine=ocr)  # 預設模式（v2）

    # 防呆上限：紅燈情境下主迴圈不會自行停止，避免測試卡死
    polls = {"count": 0}

    def _bounded_sleep(duration: float) -> None:
        polls["count"] += 1
        if polls["count"] >= max_polls:
            ctx.running = False

    machine._interruptible_sleep = _bounded_sleep

    finalize_calls: list[tuple[str, dict | None]] = []

    def _fake_finalize(reason: str, extra: dict | None = None):
        finalize_calls.append((reason, extra))
        ctx.running = False
        return None

    machine.finalize_failure = _fake_finalize
    return machine, ctx, wm, fake_input, finalize_calls


class BotUnknownLoopTests(unittest.TestCase):
    def test_persistent_unknown_makes_zero_clicks_and_finalizes_failure(self) -> None:
        ocr = FakeOCR(simple_results={None: list(NOISE_TEXTS)})
        machine, ctx, wm, fake_input, finalize_calls = _build_machine(ocr)

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.object(states, "_settle_and_refresh", return_value=True) as settle, \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        # UNKNOWN 期間零點擊、零 handler 動作
        self.assertEqual(fake_input.clicks, [])
        # 第 4 次連續 UNKNOWN → finalize_failure("state_unknown_persistent")
        self.assertEqual(len(finalize_calls), 1, f"finalize_calls={finalize_calls}")
        self.assertEqual(finalize_calls[0][0], "state_unknown_persistent")
        # 連續 1 次 → 重拍重判（每次輪詢都重新截圖）
        self.assertEqual(wm.capture_calls, 4)
        # 連續 2、3 次 → _settle_and_refresh 後重判
        self.assertEqual(settle.call_count, 2)
        # UNKNOWN 不覆蓋 current_state
        self.assertEqual(ctx.current_state, "STATE_POTENTIAL_SELECT")
        # UNKNOWN 與 evidence 進 state_trace
        unknown_entries = [
            entry for entry in ctx.state_trace if entry.get("current") == "STATE_UNKNOWN"
        ]
        self.assertEqual(len(unknown_entries), 4)
        for entry in unknown_entries:
            self.assertIn("evidence", entry)

    def test_unknown_streak_resets_after_recovery(self) -> None:
        ocr = FakeOCR(
            simple_sequence=[
                list(NOISE_TEXTS),       # 第 1 拍：UNKNOWN
                list(LOBBY_TEXTS),       # 第 2 拍：恢復為 STATE_LOBBY
                list(NOISE_TEXTS),       # 第 3 拍：再一次 UNKNOWN（streak 應從 1 重數）
                list(LOBBY_TEXTS),       # 第 4 拍：又恢復
            ],
            simple_results={None: list(LOBBY_TEXTS)},
        )
        machine, ctx, wm, fake_input, finalize_calls = _build_machine(ocr, max_polls=5)

        handler_runs = {"count": 0}

        def _stub_handler(_ctx):
            handler_runs["count"] += 1
            return None

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.dict(StateMachine.STATE_HANDLERS, {"STATE_LOBBY": _stub_handler}), \
             patch.object(states, "_settle_and_refresh", return_value=True), \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        # 兩段 UNKNOWN 各只有 1 次，未達 4 → 不得 finalize
        self.assertEqual(finalize_calls, [])
        # 恢復後狀態為 LOBBY，且 handler 有被執行（UNKNOWN 拍次沒有）
        self.assertEqual(ctx.current_state, "STATE_LOBBY")
        self.assertGreaterEqual(handler_runs["count"], 1)
        # UNKNOWN 拍次不點擊（stub handler 也不點擊）
        self.assertEqual(fake_input.clicks, [])


if __name__ == "__main__":
    unittest.main()
