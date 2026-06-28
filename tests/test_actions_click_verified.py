"""
Phase 1.3 點擊安全化測試 (tests/test_actions_click_verified.py)

先紅後綠（CLAUDE.md 鐵則 1）。本檔三組紅測試，遷移前必須全部失敗：

  紅 1（HandlerEmptyOcrTests）：FakeOCR 給空結果時，四個已遷移 handler
      （handle_shop / handle_potential_select / handle_event / handle_shop_choice）
      的點擊數必須為 0 —— 現行為「找不到目標仍點固定備援座標」（R3），點擊數 >= 1。
  紅 2（ClickVerifiedUnitTests）：core.actions.click_verified 點擊後必須重拍
      （capture 次數增加）驗證 expect；不滿足 → 重試一次 → 仍失敗回 False 並記 trace。
      實作前 core.actions 不存在 → ImportError 即紅。
  紅 3（Replay20260602215757Tests）：以 tests/replays/ocr_cache 的真實 OCR 語料
      重放 20260602_215757 軌跡（商店購買成功 → 誤判選卡 → OCR 找不到 Reroll）：
      handler 不得產生備援盲點（現行為盲點 (1216, 648) 後卡死）。
"""
from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import core.states as states
from tests.fakes import (
    FakeDecisionEngine,
    FakeInput,
    FakeOCR,
    FakeWindowManager,
    FakeWindowManagerSequence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OCR_CACHE_DIR = PROJECT_ROOT / "tests" / "replays" / "ocr_cache"


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _load_cache_items(file_name: str) -> list[tuple[str, float, tuple]]:
    payload = json.loads(
        (OCR_CACHE_DIR / f"{file_name}.json").read_text(encoding="utf-8")
    )
    return [
        (
            item["text"],
            float(item["confidence"]),
            tuple((int(px), int(py)) for px, py in item["bbox"]),
        )
        for item in payload["items"]
    ]


def _import_actions():
    """延遲 import：實作前本模組不存在，各單元測試個別紅。"""
    return importlib.import_module("core.actions")


class HandlerEmptyOcrTests(unittest.TestCase):
    """紅 1：OCR 空結果時，已遷移 handler 不得有任何點擊（REPAIR_PLAN 1.3 原文）。"""

    def setUp(self) -> None:
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def test_handle_shop_with_empty_ocr_must_not_click(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_SHOP",
            current_money=0,
            current_floor=5,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            wm=FakeWindowManager(self.frame),
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1280,
            frame_h=720,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "OCR 全空時不得點任何固定備援座標（R3）")

    def test_handle_potential_select_with_empty_ocr_must_not_click(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManager(self.frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "卡槽無任何 OCR 證據時不得點卡/點拿走（R3）")

    def test_handle_event_with_empty_ocr_must_not_click(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_EVENT",
            ocr=FakeOCR(),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManager(self.frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "找不到事件選項時不得點固定列座標（R3）")

    def test_handle_shop_choice_with_empty_ocr_must_not_click(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_SHOP_CHOICE",
            shop_visit_count=0,
            current_floor=5,
            current_money=0,
            ocr=FakeOCR(),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManager(self.frame),
            config={"bot": {}},
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "找不到商店選項文字時不得點固定列座標（R3）")
        self.assertEqual(
            getattr(ctx, "shop_visit_count", 0), 0,
            "沒有實際點擊時不得累計商店造訪次數",
        )


class Replay20260602215757Tests(unittest.TestCase):
    """紅 3：20260602_215757 失敗軌跡重放（真實 OCR 語料，ocr_cache）。

    軌跡：商店購買成功 → 畫面被判為選卡 → 決策要求 reroll → OCR 找不到
    Reroll 字樣 → 舊版盲點 (0.95, 0.90) 備援座標 → 卡死（外部 watchdog 收屍）。
    新行為：找不到 Reroll → 不點，回到主迴圈下一輪重判。
    """

    def test_replay_misjudged_shop_frame_must_not_blind_click_reroll(self) -> None:
        items = _load_cache_items("potential_select__20260602_215757__last.png")
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)  # 語料原圖為 1280x720
        engine = SimpleNamespace(decide=lambda options: None)  # 與當日相同：決策 = reroll
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=items),
            last_frame=frame.copy(),
            engine=engine,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManager(frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(
            ctx.input.clicks,
            [],
            "OCR 找不到 Reroll 字樣時不得盲點 (1216, 648) 備援座標"
            "（R3，session 20260602_215757 卡死根因）",
        )


class ClickVerifiedUnitTests(unittest.TestCase):
    """紅 2 + click_verified 行為契約（target 解析 / expect 驗證 / 白名單座標）。"""

    def setUp(self) -> None:
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def _ctx(self, ocr=None, wm="static", with_traces: bool = False) -> SimpleNamespace:
        ns = SimpleNamespace(
            ocr=ocr if ocr is not None else FakeOCR(),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            current_state="STATE_SHOP",
        )
        if wm == "static":
            ns.wm = FakeWindowManager(self.frame)
        elif wm is not None:
            ns.wm = wm
        if with_traces:
            ns.click_log = []
            ns.state_log = []
            ns.ocr_log = []
            ns.record_click = lambda **kw: ns.click_log.append(kw)
            ns.record_state_transition = lambda **kw: ns.state_log.append(kw)
            ns.record_ocr_hit = lambda **kw: ns.ocr_log.append(kw)
        return ns

    def test_target_missing_returns_false_without_click(self) -> None:
        actions = _import_actions()
        ctx = self._ctx(ocr=FakeOCR())
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.TextTarget(("確認",)),
                expect=actions.ExpectRoiChange(),
            )
        self.assertFalse(ok)
        self.assertEqual(ctx.input.clicks, [])
        self.assertEqual(ctx.wm.capture_calls, 0)

    def test_click_recaptures_then_retries_once_and_fails_with_trace(self) -> None:
        actions = _import_actions()
        ctx = self._ctx(
            ocr=FakeOCR(global_results=[("確認", 1.0, _bbox(700, 600))]),
            with_traces=True,
        )
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.TextTarget(("確認",)),
                expect=actions.ExpectRoiChange(),
            )
        self.assertFalse(ok, "expect 不滿足 → 重試一次後必須回 False")
        self.assertEqual(len(ctx.input.clicks), 2, "重試恰好一次（共 2 次點擊）")
        self.assertEqual(ctx.wm.capture_calls, 2, "每次點擊後必須重拍驗證")
        self.assertTrue(
            any(entry.get("source") == "click_verified_verify_failed" for entry in ctx.click_log),
            "click_trace 必須能看出 verify 失敗",
        )
        self.assertTrue(
            any(entry.get("verify") == "failed" for entry in ctx.state_log),
            "state_trace 必須能看出 verify 失敗",
        )

    def test_succeeds_when_roi_hash_changes_after_click(self) -> None:
        actions = _import_actions()
        changed = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx = self._ctx(
            ocr=FakeOCR(global_results=[("確認", 1.0, _bbox(700, 600))]),
            wm=FakeWindowManagerSequence([changed]),
        )
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.TextTarget(("確認",)),
                expect=actions.ExpectRoiChange(),
            )
        self.assertTrue(ok)
        self.assertEqual(len(ctx.input.clicks), 1)
        self.assertEqual(ctx.wm.capture_calls, 1)

    def test_succeeds_when_expected_state_signature_hits(self) -> None:
        actions = _import_actions()
        ctx = self._ctx(
            ocr=FakeOCR(global_results=[
                ("購買", 1.0, _bbox(620, 500)),
                ("潛能特飲", 1.0, _bbox(550, 240)),
                ("單價", 1.0, _bbox(570, 440)),
            ]),
        )
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.TextTarget(("購買",)),
                expect=actions.ExpectStateIn(states=("STATE_SHOP",)),
            )
        self.assertTrue(ok)
        self.assertEqual(len(ctx.input.clicks), 1)
        self.assertGreaterEqual(ctx.wm.capture_calls, 1)

    def test_point_target_allows_only_whitelisted_safe_points(self) -> None:
        actions = _import_actions()
        ctx = self._ctx()
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.PointTarget("tap_continue"),
                expect=actions.EXPECT_NONE,
            )
        self.assertTrue(ok)
        self.assertEqual(ctx.input.clicks, [(1126, 561)])

        with self.assertRaises(ValueError):
            actions.click_verified(
                ctx,
                actions.PointTarget("random_blind_point"),
                expect=actions.EXPECT_NONE,
            )
        self.assertEqual(len(ctx.input.clicks), 1, "非白名單座標絕不可被點擊")

    def test_degrades_to_single_unverified_click_without_window_manager(self) -> None:
        actions = _import_actions()
        ctx = self._ctx(
            ocr=FakeOCR(global_results=[("確認", 1.0, _bbox(700, 600))]),
            wm=None,
        )
        with patch.object(actions.time, "sleep", return_value=None):
            ok = actions.click_verified(
                ctx,
                actions.TextTarget(("確認",)),
                expect=actions.ExpectRoiChange(),
            )
        self.assertTrue(ok, "無法重拍（無 wm）時降級為單次點擊，不重試")
        self.assertEqual(len(ctx.input.clicks), 1)


if __name__ == "__main__":
    unittest.main()
