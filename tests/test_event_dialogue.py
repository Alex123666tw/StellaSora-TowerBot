"""對話事件 NPC 台詞被當選項點 — 反覆點不推進卡死的回放修復測試。

語料 event_dialogue_lost__20260615_191325.png（1280x720,L3 session 20260615_191325
continue-run,external_watchdog_stuck STATE_EVENT）:
  綠髮女僕 NPC 對話事件,右側結構:
    NPC 台詞（非選項!,同一行被 OCR 切兩塊,含結尾省略號）:
      「我迷路了」(cx≈848,cy≈270) +「得循著聲音才知道怎麼走...」(cx≈1040,cy≈270)
    真選項列1（消耗音符,湊音符策略要拒）:
      「我幫你找找吧!」(cx≈821,cy≈355) | 消耗5個隨機音符 | 獲得150
    真選項列2（保底,無消耗）:
      「不好意思 幫不上忙」(cx≈756–934,cy≈465) | 獲得30

故障鏈（修前,commit 153d23c「事件選項 ROI 上緣 0.34→0.30」的副作用）:
  ROI 上移修好了「花錢買音符」事件,卻把這個對話事件 cy≈270 的 NPC 台詞也納入選項候選。
  _event_option_groups 後:
    group0「我迷路了 得循著聲音才知道怎麼走...」rank5 order0 —— 純 NPC 台詞,無成本/無獎勵明細
    group1「我幫你找找吧! 消耗5個隨機音苻 獲得150」—— OCR 把「符」誤判成「苻」(U+82FB),
           _event_has_note_cost 的「音符|之音」沒認到 → 漏判成花錢選項(未被拒)
    group2「不好意思 幫不上忙 獲得30」rank5 order2
  generic 激進 min((rank, order)) → 三組都 rank5 → order 最小的 group0(台詞)勝 →
  _pick_event_click_target 取 cx 最小 =「我迷路了」(848,270) → 點台詞,roi 不變,連點放棄卡死。

修後目標:
  1) 排除純 NPC 台詞組（既無成本明細、亦無獎勵明細）→ 不被當可選項。
  2) note-cost 認得 OCR 變體「音苻」→ group1（消耗音符）依湊音符策略被拒。
  → generic 激進在剩下唯一有效選項 group2 選「不好意思幫不上忙」(cy≈465)。
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
_FRAME = "event_dialogue_lost__20260615_191325.png"


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


# NPC 台詞行 cy≈270;真選項列2 cy≈465。
_DIALOGUE_CY_LO, _DIALOGUE_CY_HI = 255, 290
_OPTION2_CY_LO, _OPTION2_CY_HI = 450, 480
_OPTION2_CX_LO, _OPTION2_CX_HI = 756, 934


class EventDialogueClassifyTests(unittest.TestCase):
    def test_dialogue_frame_classifies_as_event(self) -> None:
        from vision.signatures import classify
        items = _load_cache_items(_FRAME)
        frame = _imread(_FRAMES / _FRAME)
        state, _score, _sig = classify(items, frame=frame)
        self.assertEqual(state, "STATE_EVENT", "對話事件應判 STATE_EVENT")


class EventDialogueClickTargetTests(unittest.TestCase):
    def test_does_not_click_npc_dialogue_line(self) -> None:
        """修前紅:bot 點到 NPC 台詞「我迷路了」(848,270),roi 不變卡死。
        修後:不可點在台詞行(cy≈255–290),且 matched_text 不含台詞片語/省略號。"""
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertTrue(ctx.input.clicks, "應有點擊（不可零點擊卡死）")
        cx, cy = ctx.input.clicks[0]
        matched = _text_at(cx, cy)

        self.assertFalse(
            _DIALOGUE_CY_LO <= cy <= _DIALOGUE_CY_HI,
            f"不可點到 NPC 台詞行(cy∈[{_DIALOGUE_CY_LO},{_DIALOGUE_CY_HI}]),實點 ({cx},{cy})「{matched}」",
        )
        for phrase in ("我迷路了", "得循著", "循著聲音", "..."):
            self.assertNotIn(
                phrase, matched,
                f"不可點到 NPC 台詞（含「{phrase}」）,實點「{matched}」@({cx},{cy})",
            )

    def test_picks_decline_option_not_spending_notes(self) -> None:
        """修後:排除台詞 + 拒消耗音符（含 OCR 變體「音苻」）→ 在剩下唯一有效選項
        選 group2「不好意思幫不上忙」(cx≈756–934,cy≈465),不選消耗音符的 group1。"""
        ctx = _make_ctx()
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_event(ctx)
        self.assertTrue(ctx.input.clicks, "應有點擊")
        cx, cy = ctx.input.clicks[0]
        matched = _text_at(cx, cy)
        self.assertTrue(
            _OPTION2_CY_LO <= cy <= _OPTION2_CY_HI,
            f"應選『不好意思幫不上忙』(cy≈465),實點 ({cx},{cy})「{matched}」",
        )
        self.assertTrue(
            _OPTION2_CX_LO <= cx <= _OPTION2_CX_HI,
            f"應點在選項列2 標題區(cx∈[{_OPTION2_CX_LO},{_OPTION2_CX_HI}]),實點 ({cx},{cy})「{matched}」",
        )


class EventNoteCostOcrVariantTests(unittest.TestCase):
    def test_note_cost_recognizes_ocr_glyph_variant(self) -> None:
        """OCR 常把「音符」的『符』(U+7B26)誤判成『苻』(U+82FB);消耗音符判斷要認得變體,
        否則湊音符策略漏拒花音符的選項（L3 20260615_191325 group1）。"""
        self.assertTrue(states._event_has_note_cost("消耗5個隨機音苻"))
        self.assertTrue(states._event_has_note_cost("消耗5個隨機音符"))  # 正字仍須認
        # 「獲得…音苻」是獎勵側,非消耗 → 不可誤判成消耗音符。
        self.assertFalse(states._event_has_note_cost("獲得10個隨機音苻"))


if __name__ == "__main__":
    unittest.main()
