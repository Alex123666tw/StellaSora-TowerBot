"""quiz 主觀題「猜猜看,星塔最喜歡哪個數字?」— bot 點到「選擇正確答案」提示反覆點卡死的回放修復測試。

語料 event_quiz_number__20260615_193731.png(1280x720,L3 session 20260615_193731,
卡組推到 49/78 後卡在此事件):
  問句「猜猜看...星塔最喜歡哪個數字?」(cx≈983, cy≈215)三選一,每列:
    選項標題(cx≈860,如「3? 因為總是如此做選擇」)| 「選擇正確答案」提示(cx≈1032)
    | 獎勵「將隨機獲得1個潛能!」(cx≈1164),提示+獎勵 cy 比標題低約 44px。
  正解 = 3(「3? 因為總是如此做選擇」,使用者拍板)。

故障鏈(修前):
  此主觀題不在 data/quiz_answers.json → _select_event_option 不走 quiz 分支 → 落 generic。
  答案文字(「3?…」cy≈298)與其獎勵(cy≈342)被 _group_option_rows 切成不同列、
  「選擇正確答案」提示(cy≈342)被 _event_option_groups 當成選項標題 → generic 選中此提示 →
  點 (1032,342)「選擇正確答案」不推進 → 反覆重點卡死(L3 重點 11 次後 external_watchdog_stuck)。

修法(純資料層):data/quiz_answers.json 新增 {"最喜歡哪個數字": "總是如此"} →
  走 quiz 分支(在 generic 前),題目關鍵字「最喜歡哪個數字」substring 命中問句,
  答案關鍵字「總是如此」substring 命中「3? 因為總是如此做選擇」(且不誤中 4?/5? 選項)→
  直接點正解 (cx≈860)。

本測試:
  紅 — 強制空題庫(states._quiz_db={"answers":{}})→ handle_event 點到「選擇正確答案」
       提示(cx≈1032)= 確認此為 bug(點不到正解、會卡死)。
  綠 — 清快取讓它重讀已填好的真 quiz_answers.json(states._quiz_db=None)→ handle_event
       點到「3? 因為總是如此做選擇」(cx≈860, matched_text 含「總是如此」)。
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
from tests.fakes import FakeInput, FakeOCR

_FRAMES = Path(__file__).resolve().parent / "replays" / "frames"
_OCR_CACHE = Path(__file__).resolve().parent / "replays" / "ocr_cache"
_FRAME = "event_quiz_number__20260615_193731.png"

# 「選擇正確答案」提示中心 cx≈1032(=卡死點);獎勵「將隨機獲得…」cx≈1164;選項標題 cx≈839–895。
_ANSWER_TEXT_FRAGMENT = "總是如此"   # 正解「3? 因為總是如此做選擇」的唯一識別子字串
_HINT_TEXT = "選擇正確答案"           # 修前被誤點的提示
_ANSWER_X_LO = 700                    # 選項標題中心區下界(答案文字在左)
_ANSWER_X_HI = 980                    # 選項標題中心區上界(提示/獎勵欄 cx>=982)


def _load_cache_items(name: str) -> list[tuple[str, float, tuple]]:
    data = json.loads((_OCR_CACHE / f"{name}.json").read_text(encoding="utf-8"))
    return [
        (it["text"], float(it["confidence"]), tuple((int(p[0]), int(p[1])) for p in it["bbox"]))
        for it in data["items"]
    ]


def _imread(path: Path):
    return cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        ocr=FakeOCR(global_results=_load_cache_items(_FRAME)),
        last_frame=_imread(_FRAMES / _FRAME),
        input=FakeInput(),
        frame_w=1280,
        frame_h=720,
        config={},
    )


def _text_at(cx: int, cy: int, tol: int = 30) -> str:
    """用點擊座標反查 cache 中最接近的 OCR 文字(debug/斷言訊息用)。"""
    best = ""
    best_d = 10 ** 9
    for text, _conf, bbox in _load_cache_items(_FRAME):
        bx = sum(p[0] for p in bbox) / 4
        by = sum(p[1] for p in bbox) / 4
        d = abs(bx - cx) + abs(by - cy)
        if d < best_d:
            best_d, best = d, text
    return best if best_d <= tol * 4 else f"<no-text@({cx},{cy})>"


class _QuizDbIsolation(unittest.TestCase):
    """切換 states._quiz_db 模組全域快取的測試共用基底:存檔→tearDown 還原,
    避免污染其他測試(尤其既有數學 quiz 用真 quiz_answers.json)。"""

    def setUp(self) -> None:
        self._saved_quiz_db = states._quiz_db

    def tearDown(self) -> None:
        states._quiz_db = self._saved_quiz_db


class EventQuizNumberClassifyTests(unittest.TestCase):
    def test_quiz_number_frame_classifies_as_event(self) -> None:
        from vision.signatures import classify
        items = _load_cache_items(_FRAME)
        frame = _imread(_FRAMES / _FRAME)
        state, _score, _sig = classify(items, frame=frame)
        self.assertEqual(state, "STATE_EVENT", "猜數字 quiz 事件應判 STATE_EVENT")


class EventQuizNumberRedTests(_QuizDbIsolation):
    def test_empty_quiz_db_clicks_hint_label_bug(self) -> None:
        """修前確認紅:題庫未含此題(強制空題庫)→ 落 generic → 點到「選擇正確答案」提示
        (cx≈1032)= 卡死座標,而非正解「3?…」(cx≈860)。"""
        states._quiz_db = {"answers": {}}
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertTrue(ctx.input.clicks, "應有點擊")
        cx, cy = ctx.input.clicks[0]
        matched = _text_at(cx, cy)
        # bug 證據:點到的是「選擇正確答案」提示(非答案),且座標落在提示欄(cx>=982)。
        self.assertEqual(
            matched, _HINT_TEXT,
            f"空題庫應誤點「{_HINT_TEXT}」提示(bug),實點 ({cx},{cy})「{matched}」",
        )
        self.assertGreaterEqual(
            cx, 982,
            f"空題庫應點到提示/獎勵欄(cx>=982,卡死點),實點 ({cx},{cy})「{matched}」",
        )
        self.assertNotIn(
            _ANSWER_TEXT_FRAGMENT, matched,
            f"空題庫不該點到正解「{_ANSWER_TEXT_FRAGMENT}」(那就不是 bug 了),實點「{matched}」",
        )


class EventQuizNumberGreenTests(_QuizDbIsolation):
    def test_real_quiz_db_clicks_correct_answer(self) -> None:
        """修後綠:清快取重讀已填好的真 quiz_answers.json → quiz 分支命中 →
        點到正解「3? 因為總是如此做選擇」(cx≈860, matched_text 含「總是如此」)。"""
        states._quiz_db = None   # 清快取 → _load_quiz_db 重讀真 json(含已新增的此題)
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "quiz 命中應恰點一次正解")
        cx, cy = ctx.input.clicks[0]
        matched = _text_at(cx, cy)
        self.assertIn(
            _ANSWER_TEXT_FRAGMENT, matched,
            f"應點到正解(含「{_ANSWER_TEXT_FRAGMENT}」),實點 ({cx},{cy})「{matched}」",
        )
        self.assertNotEqual(
            matched, _HINT_TEXT,
            f"不可點到「{_HINT_TEXT}」提示(卡死),實點 ({cx},{cy})「{matched}」",
        )
        self.assertTrue(
            _ANSWER_X_LO <= cx <= _ANSWER_X_HI,
            f"應點選項標題欄(cx∈[{_ANSWER_X_LO},{_ANSWER_X_HI}]),實點 ({cx},{cy})「{matched}」",
        )

    def test_real_quiz_db_explicit_patch_clicks_correct_answer(self) -> None:
        """同綠,改用顯式 patch 題庫(不依賴真 json 內容)→ 證明新增的這條 key/value 有效。"""
        states._quiz_db = {"answers": {"最喜歡哪個數字": "總是如此"}}
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_event(ctx)
        self.assertTrue(ctx.input.clicks)
        cx, cy = ctx.input.clicks[0]
        matched = _text_at(cx, cy)
        self.assertIn(_ANSWER_TEXT_FRAGMENT, matched, f"實點 ({cx},{cy})「{matched}」")


if __name__ == "__main__":
    unittest.main()
