from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from tests.fakes import FakeOCR
from vision.state_detector import DetectionResult, StateDetector, STATE_UNKNOWN

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OCR_CACHE_DIR = PROJECT_ROOT / "tests" / "replays" / "ocr_cache"


def _cache_texts(file_name: str) -> list[str]:
    payload = json.loads((OCR_CACHE_DIR / f"{file_name}.json").read_text(encoding="utf-8"))
    return [item["text"] for item in payload["items"]]


class StateDetectorTests(unittest.TestCase):
    """StateDetector v2：detect() 回傳 DetectionResult(state, confidence, evidence)。"""

    def test_event_choice_text_beats_shop_like_rewards(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["音樂，也是治療的方法。", "我獨愛這些。", "獲得5個幸運之音", "獲得5個專注之音"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_SHOP")
        self.assertEqual(result.state, "STATE_EVENT")

    def test_mirror_event_choice_beats_potential_visual(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["你也想踏入命運之鏡，是嗎？", "魔鏡...拜託了!", "33%機率獲得1個潛能", "不了，好危險的樣子"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[585:635, 720:910] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, "STATE_EVENT")

    def test_life_wager_event_choice_beats_potential_visual(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["生命力也可以成為籌碼嗎？", "那還是分我一些吧。", "試試也不是不行...", "消耗30%生命值，隨機獲得1個潛能"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[585:635, 720:910] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, "STATE_EVENT")

    def test_potential_select_text_beats_shop_choice_keywords(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["隊伍等級提升至3級", "未收錄", "等級 3", "拿走", "更新"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_SHOP_CHOICE")
        self.assertEqual(result.state, "STATE_POTENTIAL_SELECT")

    def test_potential_select_visual_beats_shop_choice_keywords(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["隊伍等級提升至3級", "等級 3", "強化"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[585:635, 720:910] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_SHOP_CHOICE")
        self.assertEqual(result.state, "STATE_POTENTIAL_SELECT")

    def test_detect_shop_from_real_shop_keywords(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["優惠", "潛能特飲", "刷新次數 2"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_SHOP_CHOICE")
        self.assertEqual(result.state, "STATE_SHOP")

    def test_shop_screen_text_beats_potential_visual(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["潛能特飲", "體力之音*5", "專注之音*5", "剩餘次數2", "背包", "Space 查看"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[585:635, 720:910] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, "STATE_SHOP")

    def test_shop_purchase_modal_beats_potential_visual(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["購買", "潛能特飲", "可獲得新潛能或提升潛能等級", "單價", "Space", "購買"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[500:550, 530:750] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, "STATE_SHOP")

    def test_shop_purchase_modal_without_buy_text_beats_potential_visual(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["潛能特飲", "可獲得新潛能或提升潛能等級", "單價", "Space"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[500:550, 530:750] = (220, 190, 20)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, "STATE_SHOP")

    def test_unknown_ide_noise_frame_returns_state_unknown(self) -> None:
        """R2 紅測試：IDE 視窗雜訊圖（unknown__20260531_195529__last）不得誤判。

        該圖 OCR 含「快速戰鬥」等跨畫面通用詞，v1 平表會誤判為 STATE_LOBBY
        或維持原狀態；v2 必須回 STATE_UNKNOWN。
        """
        texts = _cache_texts("unknown__20260531_195529__last.png")
        detector = StateDetector(FakeOCR(simple_results={None: texts}))
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertEqual(result.state, STATE_UNKNOWN)
        self.assertEqual(result.confidence, 0.0)

    def test_departure_text_alone_does_not_enter_lobby(self) -> None:
        """單獨一個「出發」不足以判定大廳；v2 下回 STATE_UNKNOWN（而非誤入 LOBBY）。"""
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["出發"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_HOME")
        self.assertNotEqual(result.state, "STATE_LOBBY")
        self.assertEqual(result.state, STATE_UNKNOWN)

    def test_lobby_detected_when_two_lobby_keywords_present(self) -> None:
        """大廳簽名 min_score=1.10：至少兩個關鍵字（語料庫大廳圖皆滿足）。"""
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["星塔探索", "難度 2", "快速戰鬥", "出發"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_HOME")
        self.assertEqual(result.state, "STATE_LOBBY")

    def test_detect_returns_detection_result_with_confidence_and_evidence(self) -> None:
        detector = StateDetector(
            FakeOCR(simple_results={
                None: ["購買", "潛能特飲", "單價"],
            })
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_POTENTIAL_SELECT")
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(result.state, "STATE_SHOP")
        self.assertGreaterEqual(result.confidence, 1.0)
        # evidence 含命中簽名與關鍵字，可直接進 state_trace
        self.assertIn("signature:shop_purchase_modal_buy", result.evidence)
        self.assertIn("keyword:購買", result.evidence)
        for item in result.evidence:
            self.assertIsInstance(item, str)

    def test_v1_mode_keeps_current_state_when_nothing_hits(self) -> None:
        """config vision.detector: v1 對照模式 = 舊「未命中維持原狀態」行為。"""
        detector = StateDetector(
            FakeOCR(simple_results={None: ["完全無關的雜訊文字"]}),
            mode="v1",
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_SHOP")
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(result.state, "STATE_SHOP")
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.evidence, ())

    def test_v2_mode_returns_unknown_on_ocr_exception(self) -> None:
        class _BrokenOCR:
            def read_text_simple(self, img, roi=None):
                raise RuntimeError("ocr boom")

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = StateDetector(_BrokenOCR()).detect(frame, current_state="STATE_SHOP")
        self.assertEqual(result.state, STATE_UNKNOWN)
        self.assertIn("ocr_exception", result.evidence)
        # v1 對照：OCR 失敗沿用舊行為（保持當前狀態）
        result_v1 = StateDetector(_BrokenOCR(), mode="v1").detect(frame, current_state="STATE_SHOP")
        self.assertEqual(result_v1.state, "STATE_SHOP")

    def test_unknown_detector_mode_falls_back_to_v2(self) -> None:
        detector = StateDetector(FakeOCR(simple_results={None: ["雜訊"]}), mode="v99")
        self.assertEqual(detector.mode, "v2")
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector.detect(frame, current_state="STATE_HOME")
        self.assertEqual(result.state, STATE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
