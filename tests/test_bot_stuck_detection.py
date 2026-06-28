"""
Phase 1.4 主迴圈內建卡死偵測測試 (tests/test_bot_stuck_detection.py) — 解 R4。

R4：`NO_PROGRESS_STATES` 恰好不含實際卡死的狀態（SHOP / POTENTIAL_SELECT /
EVENT），bot 只能靠外部 watchdog 收屍；且外部 watchdog 的 roi_hash 進度定義
被畫面動畫擊穿（實機證據 20260612_211534：事件畫面動畫使 roi_hash 每 ~3 秒
變一次，bot 凍結 14 分鐘無人收屍）。

紅測試（修復前必須紅）：
  1. 固定 frame、狀態恆 SHOP / POTENTIAL_SELECT / EVENT、零點擊
     → K 次輪詢內必須 finalize_failure("state_stuck_no_progress")。
  2. 動畫盲區（模擬 20260612_211534）：狀態恆 POTENTIAL_SELECT、零成功點擊、
     frame 每拍不同（動畫）但 OCR 文字集合不變
     → 必須在 ≤30 次等效輪詢內 finalize。
  3. UNKNOWN 輪詢不得計入 stuck 計數（UNKNOWN 有自己的 finalize 路徑）。

反例（不得誤殺）：
  - reroll 成功（click_verified 式成功點擊）→ 合法原地進度。
  - OCR 文字集合實質變化（換卡）→ 合法原地進度。
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import core.states as states
from core.bot import BotContext, StateMachine
from tests.fakes import (
    FakeDecisionEngine,
    FakeInput,
    FakeWindowManager,
    FakeWindowManagerSequence,
)
from vision.state_detector import DetectionResult

_FAKE_WIN32GUI = SimpleNamespace(IsWindow=lambda hwnd: True)

# 模擬選卡畫面的固定 OCR 文字集合（動畫光效不產生文字 → 每拍相同）
POTENTIAL_TEXTS = ["請選擇 1個", "爆裂追擊", "快拳連打", "終結打擊", "重抽"]
POTENTIAL_TEXTS_B = ["請選擇 1個", "勇猛挑戰", "回旋反擊", "巔峰狀態", "重抽"]
SHOP_TEXTS = ["商店", "單價", "購買", "潛能特飲", "離開"]
EVENT_TEXTS = ["事件", "音樂治療", "接受", "拒絕"]


class _FakeTextFeed:
    """模擬 RecordingOcr 介面（generation / last_texts）。

    由 _FakeDetector 在每次 detect() 時 tick()，等同生產環境
    「StateDetector 的 OCR 呼叫經 RecordingOcr 記錄」的時序。
    """

    def __init__(self, sequence: list[list[str]]) -> None:
        self._sequence = [list(items) for items in sequence]
        self.generation = 0
        self.last_texts: list[str] | None = None

    def tick(self) -> None:
        if self._sequence:
            self.last_texts = (
                self._sequence.pop(0) if len(self._sequence) > 1 else list(self._sequence[0])
            )
        self.generation += 1


class _FakeDetector:
    """依序回傳指定狀態的假 StateDetector（佇列剩一個後固定回傳）。"""

    mode = "v2"

    def __init__(self, detected_states: list[str], feed: _FakeTextFeed | None = None) -> None:
        self._states = list(detected_states)
        self._feed = feed

    def detect(self, frame, current_state) -> DetectionResult:
        if self._feed is not None:
            self._feed.tick()
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return DetectionResult(state=state, confidence=1.0, evidence=("fake_detector",))


def _build_machine(
    detector,
    *,
    initial_state: str,
    wm=None,
    bot_config: dict | None = None,
    max_polls: int = 40,
):
    """不經 __init__（避免真 OCR/win32）手工組裝 StateMachine + 全 Fake 硬體層。"""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    wm = wm if wm is not None else FakeWindowManager(frame)
    fake_input = FakeInput()
    ctx = BotContext(
        engine=FakeDecisionEngine(),
        config={
            "run": {"max_runs": 1},
            "bot": {"poll_interval": 0.0, **(bot_config or {})},
        },
        wm=wm,
        input=fake_input,
        ocr=None,
        matcher=None,
        max_runs=1,
    )
    ctx.current_state = initial_state

    machine = StateMachine.__new__(StateMachine)
    machine.ctx = ctx
    machine._config_path = "config.yaml"
    machine._poll_interval = 0.0
    machine._failure_finalized = False
    machine._detector = detector

    polls = {"count": 0}

    def _bounded_sleep(duration: float) -> None:
        polls["count"] += 1
        if polls["count"] >= max_polls:
            ctx.running = False

    machine._interruptible_sleep = _bounded_sleep

    finalize_calls: list[tuple[str, dict | None, int]] = []

    def _fake_finalize(reason: str, extra: dict | None = None):
        finalize_calls.append((reason, extra, polls["count"]))
        ctx.running = False
        return None

    machine.finalize_failure = _fake_finalize
    return machine, ctx, fake_input, finalize_calls, polls


def _noop_handler(_ctx):
    """模擬 Phase 1.3 之後的安全 handler：目標找不到 → 不點、不換狀態。"""
    return None


class StuckDetectionRedTests(unittest.TestCase):
    """紅測試 1：固定 frame、狀態不變、零點擊 → 必須 finalize。"""

    def test_static_screen_in_uncovered_states_finalizes_state_stuck(self) -> None:
        cases = {
            "STATE_SHOP": SHOP_TEXTS,
            "STATE_POTENTIAL_SELECT": POTENTIAL_TEXTS,
            "STATE_EVENT": EVENT_TEXTS,
        }
        for state, texts in cases.items():
            with self.subTest(state=state):
                feed = _FakeTextFeed([texts])
                detector = _FakeDetector([state], feed=feed)
                machine, ctx, fake_input, finalize_calls, polls = _build_machine(
                    detector, initial_state=state
                )
                machine._ocr_recorder = feed

                with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
                     patch.dict(StateMachine.STATE_HANDLERS, {state: _noop_handler}), \
                     patch.object(states.time, "sleep", return_value=None):
                    machine.run()

                self.assertEqual(fake_input.clicks, [])
                self.assertEqual(
                    len(finalize_calls), 1,
                    f"{state} 無進度必須 finalize（現行 NO_PROGRESS_STATES 不含此狀態，"
                    f"修復前此處為紅）：finalize_calls={finalize_calls}",
                )
                reason, extra, polls_at_finalize = finalize_calls[0]
                self.assertEqual(reason, "state_stuck_no_progress")
                self.assertEqual((extra or {}).get("state"), state)
                self.assertLessEqual(
                    polls_at_finalize, 30,
                    "必須在 ≤30 次等效輪詢內收屍",
                )

    def test_animation_blind_spot_20260612_finalizes_within_30_polls(self) -> None:
        """紅測試 2：模擬 session 20260612_211534 的動畫盲區。

        狀態恆 POTENTIAL_SELECT（誤判）、點擊零成功、frame 每拍不同
        （畫面動畫 → roi_hash 每拍變化）、但 OCR 文字集合不變
        （光效不產生文字）→ 必須在 ≤30 次等效輪詢內 finalize。
        roi_hash 變化不得再被當成進度。
        """
        animated_frames = [
            np.full((720, 1280, 3), i % 256, dtype=np.uint8) for i in range(40)
        ]
        wm = FakeWindowManagerSequence(animated_frames)
        feed = _FakeTextFeed([POTENTIAL_TEXTS])  # 文字集合恆定
        detector = _FakeDetector(["STATE_POTENTIAL_SELECT"], feed=feed)
        machine, ctx, fake_input, finalize_calls, polls = _build_machine(
            detector, initial_state="STATE_POTENTIAL_SELECT", wm=wm
        )
        machine._ocr_recorder = feed

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.dict(
                 StateMachine.STATE_HANDLERS,
                 {"STATE_POTENTIAL_SELECT": _noop_handler},
             ), \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        self.assertEqual(fake_input.clicks, [])
        self.assertEqual(
            len(finalize_calls), 1,
            f"動畫盲區必須被內建偵測收屍（修復前此處為紅）：finalize_calls={finalize_calls}",
        )
        reason, extra, polls_at_finalize = finalize_calls[0]
        self.assertEqual(reason, "state_stuck_no_progress")
        self.assertLessEqual(polls_at_finalize, 30, "≤30 次等效輪詢內必須 finalize")

    def test_unknown_polls_do_not_feed_stuck_counter(self) -> None:
        """UNKNOWN 輪詢不計入 stuck 計數（UNKNOWN 已有自己的 finalize 路徑）。

        交錯 UNKNOWN / SHOP（K=4）：
          - unknown_streak 每次被已知狀態重置（恆 1 < 4）→ 不得走 UNKNOWN finalize。
          - stuck 計數只吃 SHOP 輪詢 → finalize 時的輪詢數必須 ≥ 2K-1
            （若 UNKNOWN 也被計入，會提早一倍收屍）。
        """
        pattern = ["STATE_UNKNOWN", "STATE_SHOP"] * 16
        feed = _FakeTextFeed([SHOP_TEXTS])
        detector = _FakeDetector(pattern, feed=feed)
        machine, ctx, fake_input, finalize_calls, polls = _build_machine(
            detector,
            initial_state="STATE_SHOP",
            bot_config={"stuck_poll_limit": 4, "max_unknown_streak": 4},
        )
        machine._ocr_recorder = feed

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.dict(StateMachine.STATE_HANDLERS, {"STATE_SHOP": _noop_handler}), \
             patch.object(states, "_settle_and_refresh", return_value=True), \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        self.assertEqual(
            len(finalize_calls), 1,
            f"交錯 UNKNOWN/SHOP 最終必須由 stuck 偵測收屍：finalize_calls={finalize_calls}",
        )
        reason, _extra, polls_at_finalize = finalize_calls[0]
        self.assertEqual(reason, "state_stuck_no_progress")
        self.assertGreaterEqual(
            polls_at_finalize, 7,
            "UNKNOWN 輪詢不得計入 stuck 計數（K=4 交錯下至少需 ~2K 次輪詢）",
        )


class StuckDetectionGuardTests(unittest.TestCase):
    """反例：合法原地進度不得誤殺。"""

    def test_verified_reroll_click_is_progress_no_false_kill(self) -> None:
        """reroll 成功（click_verified 式成功且通過驗證的點擊）→ 不得誤殺。"""

        def _reroll_handler(ctx):
            # 模擬 actions.click_verified 成功回 True 時寫入的 click_trace 欄位
            ctx.record_click(
                source="potential_reroll",
                x=1216,
                y=648,
                success=True,
                target="text:重抽",
                expect="roi_change:full_frame",
                attempt=1,
            )
            return None

        feed = _FakeTextFeed([POTENTIAL_TEXTS])  # 文字集合恆定，靠點擊訊號活命
        detector = _FakeDetector(["STATE_POTENTIAL_SELECT"], feed=feed)
        machine, ctx, fake_input, finalize_calls, polls = _build_machine(
            detector, initial_state="STATE_POTENTIAL_SELECT", max_polls=40
        )
        machine._ocr_recorder = feed

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.dict(
                 StateMachine.STATE_HANDLERS,
                 {"STATE_POTENTIAL_SELECT": _reroll_handler},
             ), \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        self.assertEqual(
            finalize_calls, [],
            f"驗證通過的點擊是強進度訊號，不得誤殺：finalize_calls={finalize_calls}",
        )

    def test_ocr_text_set_change_is_progress_no_false_kill(self) -> None:
        """OCR 文字集合實質變化（reroll 換卡後卡名全換）→ 不得誤殺。"""
        feed = _FakeTextFeed([POTENTIAL_TEXTS, POTENTIAL_TEXTS_B] * 20)
        detector = _FakeDetector(["STATE_POTENTIAL_SELECT"], feed=feed)
        machine, ctx, fake_input, finalize_calls, polls = _build_machine(
            detector, initial_state="STATE_POTENTIAL_SELECT", max_polls=40
        )
        machine._ocr_recorder = feed

        with patch.dict(sys.modules, {"win32gui": _FAKE_WIN32GUI}), \
             patch.dict(
                 StateMachine.STATE_HANDLERS,
                 {"STATE_POTENTIAL_SELECT": _noop_handler},
             ), \
             patch.object(states.time, "sleep", return_value=None):
            machine.run()

        self.assertEqual(
            finalize_calls, [],
            f"OCR 文字集合實質變化是進度訊號，不得誤殺：finalize_calls={finalize_calls}",
        )


if __name__ == "__main__":
    unittest.main()
