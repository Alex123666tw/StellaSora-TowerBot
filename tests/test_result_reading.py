"""Phase 2.3 結算畫面感知 — 回放測試（GAME_MECHANICS F1/F1b/F2/F2b）。

語料 result__20260614_005348__last.png（1280x720,史上首次抵達結算畫面,
logs/session_failures/20260614_005348/last_frame.png 複製入庫）。OCR cache 勘查：
  評分六角徽章「27」conf 0.998（左上 x≈104,y≈80）;角色潛能總等級右側清單
  風影「29」/夏花「14」conf 0.96+（第三角色「13」OCR 漏讀 → 合計 best-effort）;
  「已鎖定」conf 0.835（左下,評分高自動上鎖）;「儲存紀錄」conf 0.836（右下）。

斷言重點：
  1. _read_result_rating 讀出評分 27（六角徽章,主達標依據）。
  2. _result_is_locked 判定此紀錄鎖定中（丟棄前須先解鎖,F2b）。
  3. _read_result_potential_total 合計右欄純數字（29+14=43,best-effort,不含漏讀的 13）。
  4. 真圖仍 classify 成 STATE_RESULT；解散確認彈窗 tokens 判 STATE_DISCARD_CONFIRM。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import cv2

import core.states as states
from tests.fakes import FakeInput, FakeWindowManagerSequence

_FRAMES = Path(__file__).resolve().parent / "replays" / "frames"
_OCR_CACHE = Path(__file__).resolve().parent / "replays" / "ocr_cache"
_FRAME = "result__20260614_005348__last.png"


def _imread(path: Path):
    buf = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _load_cache_items(name: str) -> list[tuple[str, float, tuple]]:
    data = json.loads((_OCR_CACHE / f"{name}.json").read_text(encoding="utf-8"))
    items: list[tuple[str, float, tuple]] = []
    for it in data["items"]:
        bbox = tuple((int(p[0]), int(p[1])) for p in it["bbox"])
        items.append((it["text"], float(it["confidence"]), bbox))
    return items


class CachedOCR:
    """以語料 OCR cache 重現 read_text/read_text_simple，含 ROI 裁切過濾（不跑 EasyOCR）。"""

    def __init__(self, items: list[tuple[str, float, tuple]]) -> None:
        self.items = list(items)

    def _filter(self, roi):
        if roi is None:
            return list(self.items)
        rx, ry, rw, rh = roi
        out = []
        for text, conf, bbox in self.items:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                out.append((text, conf, bbox))
        return out

    def read_text(self, img, roi=None):
        return self._filter(roi)

    def read_text_simple(self, img, roi=None):
        return [text for text, _conf, _bbox in self._filter(roi)]


def _make_ctx() -> SimpleNamespace:
    scene = _imread(_FRAMES / _FRAME)
    assert scene is not None, f"missing corpus frame {_FRAME}"
    h, w = scene.shape[:2]
    return SimpleNamespace(
        ocr=CachedOCR(_load_cache_items(_FRAME)),
        last_frame=scene,
        frame_w=w,
        frame_h=h,
    )


class ResultReadingTests(unittest.TestCase):
    def test_reads_rating_from_hexagon_badge(self) -> None:
        ctx = _make_ctx()
        self.assertEqual(states._read_result_rating(ctx), 27)

    def test_rating_roi_excludes_score_points_and_title(self) -> None:
        # 評分=27（六角徽章）,不得讀成分數 7965 或標題「未命名紀錄」。
        ctx = _make_ctx()
        value = states._read_result_rating(ctx)
        self.assertEqual(value, 27)
        self.assertNotEqual(value, 7965)

    def test_detects_locked_record(self) -> None:
        ctx = _make_ctx()
        self.assertTrue(states._result_is_locked(ctx), "此紀錄左下顯示『已鎖定』,應判鎖定中")

    def test_reads_potential_total_best_effort(self) -> None:
        # 右欄可讀的角色潛能總等級 29 + 14 = 43（第三角色 13 OCR 漏讀,best-effort）。
        ctx = _make_ctx()
        self.assertEqual(states._read_result_potential_total(ctx), 43)

    def test_frame_classifies_as_result(self) -> None:
        from vision.signatures import classify
        items = _load_cache_items(_FRAME)
        frame = _imread(_FRAMES / _FRAME)
        state, _score, _sig = classify(items, frame=frame)
        self.assertEqual(state, "STATE_RESULT")

    def test_discard_confirm_popup_classifies_over_result_and_tap_continue(self) -> None:
        # 解散確認彈窗（點垃圾桶後）：背景結算文字仍在 + 含 Space 提示,須優先判
        # STATE_DISCARD_CONFIRM（priority 13 < result_keywords 110、npc_dialogue 15）。
        from vision.signatures import classify
        texts = [
            "解散目前紀錄將獲得以下道具",
            "是否確定解散?",
            "★券x79",
            "Space 確認",
            "Esc 取消",
            "儲存紀錄",  # 背景結算殘留
            "評分",
        ]
        state, _score, _sig = classify(texts)
        self.assertEqual(state, "STATE_DISCARD_CONFIRM")


def _changed_frame() -> np.ndarray:
    return np.full((720, 1280, 3), 255, dtype=np.uint8)


def _make_handler_ctx(result_cfg: dict, **overrides) -> SimpleNamespace:
    scene = _imread(_FRAMES / _FRAME)
    h, w = scene.shape[:2]
    ctx = SimpleNamespace(
        ocr=CachedOCR(_load_cache_items(_FRAME)),
        last_frame=scene,
        frame_w=w,
        frame_h=h,
        input=FakeInput(),
        config={"result": result_cfg},
        run_count=0,
        success_count=0,
        max_runs=1,
        running=True,
        current_state="STATE_RESULT",
        # ExpectRoiChange / ExpectStateIn 重拍用：回傳與 last_frame 不同的純白 frame。
        wm=FakeWindowManagerSequence([_changed_frame(), _changed_frame()]),
        required_potentials_satisfied=lambda: True,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class HandleResultTests(unittest.TestCase):
    """Phase 2.3 重寫 handle_result：達標→儲存 / 不達標→丟棄(先解鎖→垃圾桶)。"""

    def test_locked_unmet_record_clicks_unlock_first(self) -> None:
        # 評分 27 < 門檻 30 → 不達標。紀錄「已鎖定」→ 須先點解鎖(F2b 鎖定陷阱),
        # 不可直接點垃圾桶。修復前 handle_result 點不存在的「下一步/結算」→ 0 有效點擊卡死。
        ctx = _make_handler_ctx({"rating_threshold": 30})
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_result(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "鎖定+不達標應只點『已鎖定』解鎖一次")
        # 「已鎖定」OCR bbox 中心 (132,662)。
        self.assertEqual(ctx.input.clicks[0], (132, 662))
        # 計數延後到整輪(含丟棄)跑完回大廳;結算當下只下決策、不計 run_count。
        self.assertEqual(ctx.run_count, 0, "結算當下不計(延後到回大廳)")
        self.assertTrue(getattr(ctx, "_result_outcome_pending"), "待回大廳計入該輪")
        self.assertFalse(getattr(ctx, "_result_keep"))

    def test_unlocked_unmet_record_clicks_trash(self) -> None:
        # 已解鎖(或解鎖已完成)+ 不達標 → 點垃圾桶(無文字 icon,白名單座標)。
        ctx = _make_handler_ctx(
            {"rating_threshold": 30},
            _result_accounted=True,
            _result_keep=False,
            _result_unlock_done=True,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_result(ctx)
        self.assertIsNone(result)
        # 垃圾桶白名單座標 (0.35, 0.908) → 1280x720 = (448, 653)。
        self.assertIn((448, 653), ctx.input.clicks, "不達標解鎖後應點垃圾桶")

    def test_met_record_saves(self) -> None:
        # 評分 27 >= 門檻 20 → 達標 → 點「儲存紀錄」;run/success 各 +1;達 max_runs → 停。
        ctx = _make_handler_ctx({"rating_threshold": 20})
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_result(ctx)
        self.assertIsNone(result)
        # 「儲存紀錄」OCR bbox 中心 (1138,654)。
        self.assertEqual(ctx.input.clicks[0], (1138, 654))
        # 計數延後:結算當下只點儲存、設達標待計,不在此停(停在回大廳時)。
        self.assertEqual(ctx.run_count, 0, "計數延後到回大廳")
        self.assertTrue(ctx._result_keep, "達標")
        self.assertTrue(ctx._result_outcome_pending)
        self.assertTrue(ctx.running, "結算當下不停;回大廳達 max_runs 才停")

    def test_accounting_happens_once_across_polls(self) -> None:
        # 同一結算畫面連續多拍(解鎖→垃圾桶)只計一次 run_count。
        ctx = _make_handler_ctx({"rating_threshold": 30})
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_result(ctx)   # 第一拍:解鎖
            states.handle_result(ctx)   # 第二拍:垃圾桶
        self.assertEqual(ctx.run_count, 0, "結算當下不計(延後到回大廳)")
        self.assertTrue(ctx._result_accounted, "決策只下一次")
        self.assertTrue(ctx._result_outcome_pending, "待回大廳計一次")

    def test_meets_target_falls_back_to_required_potentials_when_unreadable(self) -> None:
        # 評分讀不到(rating=0)且未設潛能門檻 → 退回 required_potentials_satisfied()
        # (保守,永不因讀數失敗誤判丟棄)。
        ctx = SimpleNamespace(
            config={"result": {"rating_threshold": 30}},
            required_potentials_satisfied=lambda: True,
        )
        self.assertTrue(states._result_meets_target(ctx, rating=0, potential_total=0))
        ctx.required_potentials_satisfied = lambda: False
        self.assertFalse(states._result_meets_target(ctx, rating=0, potential_total=0))

    def test_meets_target_potential_total_threshold_secondary(self) -> None:
        # 設了潛能總等級門檻(對標 78):評分未達但潛能達標 → 達標(OR);兩者皆未達 → 不達標。
        ctx = SimpleNamespace(config={"result": {"rating_threshold": 30, "potential_total_threshold": 50}})
        self.assertTrue(states._result_meets_target(ctx, rating=27, potential_total=56))   # 潛能 56>=50
        self.assertFalse(states._result_meets_target(ctx, rating=27, potential_total=43))  # 兩者皆未達

    def test_lobby_counts_pending_discard_round_and_stops(self) -> None:
        # 延後計數:不達標丟棄整輪跑完回大廳 → handle_lobby 計入(run+1,不算 success),
        # 達 max_runs → 停。修 session 20260614_140140 在丟棄中途(剛解鎖)就停的問題。
        ctx = SimpleNamespace(
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280, frame_h=720, input=FakeInput(),
            run_count=0, max_runs=1, success_count=0, running=True,
            _result_outcome_pending=True, _result_keep=False,
        )
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_lobby(ctx)
        self.assertEqual(ctx.run_count, 1, "回大廳才計入該輪")
        self.assertEqual(ctx.success_count, 0, "不達標(丟棄)不算 success")
        self.assertFalse(ctx.running, "達 max_runs(1) → 停")
        self.assertFalse(ctx._result_outcome_pending, "計入後清除")
        self.assertEqual(ctx.input.clicks, [], "達 max_runs 應直接停,不開新一輪")

    def test_lobby_counts_pending_saved_round_as_success(self) -> None:
        # 達標儲存整輪跑完回大廳 → 計入且算 success。
        ctx = SimpleNamespace(
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280, frame_h=720, input=FakeInput(),
            run_count=0, max_runs=1, success_count=0, running=True,
            _result_outcome_pending=True, _result_keep=True,
        )
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_lobby(ctx)
        self.assertEqual(ctx.run_count, 1)
        self.assertEqual(ctx.success_count, 1, "達標(儲存)算 success")
        self.assertFalse(ctx.running)


if __name__ == "__main__":
    unittest.main()
