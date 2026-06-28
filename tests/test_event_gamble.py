"""機率/賭博類事件 — 偵測 + 「選錢多的」決策(使用者 2026-06-14 拍板,未來可調)。

語料 event_gamble__20260614_131730.png(1280x720,session 20260614_131730):
  「這是你..理性的決斷!」三選一:
    相信運氣 50%機率獲得200 / 50%機率失去100
    相信命運 30%機率獲得650 / 70%機率失去200   ← 錢最多(650)
    相信現實 獲得30(保底)
  原本提示字不在 EVENT_CHOICE_HINTS → 判 UNKNOWN ×4 卡死(state_unknown_persistent)。

斷言:
  1. classify 該真圖 → STATE_EVENT(偵測補上)。
  2. handle_event 對機率事件選「錢多的」= 相信命運(獲得650),非第一列。
  3. _event_gamble_gain 只計純金錢「獲得 N」,排除「獲得1個潛能」等非金錢。
  4. 命運之鏡式(機率獲得潛能/生命,非金錢)不被當金錢賭博 → 維持 E1。
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
_FRAME = "event_gamble__20260614_131730.png"


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _load_cache_items(name: str) -> list[tuple[str, float, tuple]]:
    data = json.loads((_OCR_CACHE / f"{name}.json").read_text(encoding="utf-8"))
    return [
        (it["text"], float(it["confidence"]), tuple((int(p[0]), int(p[1])) for p in it["bbox"]))
        for it in data["items"]
    ]


def _imread(path: Path):
    return cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)


class GambleEventTests(unittest.TestCase):
    def test_gamble_frame_classifies_as_event(self) -> None:
        from vision.signatures import classify
        items = _load_cache_items(_FRAME)
        frame = _imread(_FRAMES / _FRAME)
        state, _score, _sig = classify(items, frame=frame)
        self.assertEqual(state, "STATE_EVENT", "賭博事件應判 STATE_EVENT,不得 UNKNOWN")

    def test_event_gamble_gain_parses_only_probability_money(self) -> None:
        # 只計「N% 機率獲得 ⟨純金錢⟩」(賭博鑑別特徵)。
        self.assertEqual(states._event_gamble_gain("相信運氣 50%機率獲得200 50%機率失去100"), 200)
        self.assertEqual(states._event_gamble_gain("相信命運 30%機率獲得650 70%機率失去200"), 650)
        # 平白「獲得30」(無機率前綴,保底列)→ 非機率賭注,回 0(不影響挑最大,650 仍最高)。
        self.assertEqual(states._event_gamble_gain("相信現實 獲得30"), 0)
        # 機率獲得潛能/生命(非金錢)→ 0。
        self.assertEqual(states._event_gamble_gain("33%機率獲得1個潛能"), 0)
        self.assertEqual(states._event_gamble_gain("33%機率恢復20%生命值"), 0)
        # 成本列「消耗…獲得150」(無機率前綴)→ 0,不被當金錢賭博(維持 E1 拒成本)。
        self.assertEqual(states._event_gamble_gain("消耗5個隨機音符，獲得150💰"), 0)

    def test_handle_event_gamble_picks_most_money_option(self) -> None:
        # 三選一賭博 → 選「相信命運」(獲得650),非第一列「相信運氣」(200)。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
                ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
                ("50%機率失去100", 0.96, _bbox(1094, 330, 130, 24)),
                ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
                ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
                ("70%機率失去200", 0.76, _bbox(1092, 440, 132, 24)),
                ("相信現實", 0.82, _bbox(756, 506, 82, 26)),
                ("獲得30", 0.98, _bbox(1166, 550, 58, 24)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={},
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        # 「相信命運」那一列 y≈396–422;不得是「相信運氣」(288–312)或「相信現實」(506–532)。
        self.assertTrue(390 <= cy <= 430, f"應選錢多的『相信命運』(650),實點 y={cy}")


if __name__ == "__main__":
    unittest.main()
