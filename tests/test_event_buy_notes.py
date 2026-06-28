"""花錢買協奏音符事件 — bot 點到成本標籤「消耗140C」反覆點卡死的回放修復測試。

語料 event_buy_notes__20260615_183043.png（1280x720,L3 session 20260615_183043,
external_watchdog_stuck STATE_EVENT,commit d3e5928「修復無效」的同一卡點）:
  標題「旋律...如何影響著你的命運?」事件,右側 4 列「花錢買音符」選項
  （標題 y 比成本/獎勵高約 42px）:
    列1 y≈245  給我一些思考的靈感 | 消耗140C  | 獲得10個強攻之音
    列2 y≈354  給我一些思考的靈感 | 消耗140   | 獲得10個專注之音
    列3 y≈465  指引我的道路       | 消耗90    | 獲得10個隨機音符
    列4 y≈575  迴避它的影響       |（迴避=不花錢）| 獲得30
  左側 x<280 是協奏技能庫存面板（干擾,ROI 已排除）;右上「380」是當前金錢。

故障鏈（修前）:
  _choice_panel_roi y-start=244 + exclude_top_ratio=0.10（排除 cy<280）→ 列1 標題
  cy=245 被切掉 → 列1 只剩 [消耗140C, 獲得10個強攻之音];_event_option_groups 把
  「消耗140C」誤當標題（has_title 只排 _event_reward_detail_text,不排成本標籤）→ 殘缺
  列1 變合法選項組;generic 激進評分挑音符獎勵最佳+order 最小 = 殘缺列1 →
  _pick_event_click_target 排除成本/獎勵後 non_detail 空 → fallback 回全列點到成本
  「消耗140C」(cx≈1038) → 不推進 → 重點 3 次放棄 → 30s watchdog stuck。

修後目標（本子項 scope）: 點到一個**選項標題**（cx≈740–900,不含「消耗」「獲得」）能推進。
（依 target_notes 缺口選最缺音符是 Phase 3② 下一子項,本測試不驗。）
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
_FRAME = "event_buy_notes__20260615_183043.png"

# 成本標籤「消耗140C」中心 cx≈1038;獎勵「獲得10…」cx≈1163;選項標題 cx≈816–845。
_COST_X_MIN = 950   # >= 此 x 視為點到成本/獎勵欄（明細區,非標題）
_TITLE_X_LO = 740   # 選項標題中心區下界
_TITLE_X_HI = 900   # 選項標題中心區上界


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


class EventBuyNotesClassifyTests(unittest.TestCase):
    def test_buy_notes_frame_classifies_as_event(self) -> None:
        from vision.signatures import classify
        items = _load_cache_items(_FRAME)
        frame = _imread(_FRAMES / _FRAME)
        state, _score, _sig = classify(items, frame=frame)
        self.assertEqual(state, "STATE_EVENT", "花錢買音符事件應判 STATE_EVENT")


class EventBuyNotesClickTargetTests(unittest.TestCase):
    def test_handle_event_clicks_option_title_not_cost_label(self) -> None:
        """修後:點到選項標題（cx≈740–900,非「消耗/獲得」明細）→ 能推進。

        修前此測試紅:會點到成本「消耗140C」(cx≈1038) 或 matched_text 含「消耗」。
        """
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertTrue(ctx.input.clicks, "應有點擊（不可零點擊卡死）")
        cx, cy = ctx.input.clicks[0]

        # 取被點座標對應的 OCR 文字（trace 不易拿,直接用座標反查 cache）。
        matched = _text_at(cx, cy)

        self.assertLess(
            cx, _COST_X_MIN,
            f"不可點到成本/獎勵明細欄（cx>={_COST_X_MIN}）,實點 ({cx},{cy})「{matched}」",
        )
        self.assertNotIn("消耗", matched, f"不可點到成本標籤,實點「{matched}」@({cx},{cy})")
        self.assertNotIn("獲得", matched, f"不可點到獎勵標籤,實點「{matched}」@({cx},{cy})")
        self.assertTrue(
            _TITLE_X_LO <= cx <= _TITLE_X_HI,
            f"應點到選項標題（cx∈[{_TITLE_X_LO},{_TITLE_X_HI}]）,實點 ({cx},{cy})「{matched}」",
        )


def _text_at(cx: int, cy: int, tol: int = 30) -> str:
    """用點擊座標反查 cache 中最接近的 OCR 文字（debug/斷言訊息用）。"""
    best = ""
    best_d = 10 ** 9
    for text, _conf, bbox in _load_cache_items(_FRAME):
        bx = sum(p[0] for p in bbox) / 4
        by = sum(p[1] for p in bbox) / 4
        d = abs(bx - cx) + abs(by - cy)
        if d < best_d:
            best_d, best = d, text
    return best if best_d <= tol * 4 else f"<no-text@({cx},{cy})>"


if __name__ == "__main__":
    unittest.main()
