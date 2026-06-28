from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import cv2

import core.states as states
from core.decision_engine import DecisionEngine
from vision.matcher import TemplateMatcher
from tests.fakes import FakeDecisionEngine, FakeInput, FakeOCR, FakeWindowManager, FakeWindowManagerSequence, FakeMatcherSequence, FakeMatchResult


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


class StateReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        self._tmp_base = Path(tempfile.gettempdir()) / "stella_sora_state_replay_tests"
        self._tmp_base.mkdir(exist_ok=True)
        self._tmpdir_path: Path | None = None

    def _make_tmpdir(self) -> Path:
        self._tmpdir_path = self._tmp_base / f"fixture_{uuid.uuid4().hex}"
        self._tmpdir_path.mkdir(parents=True, exist_ok=True)
        return self._tmpdir_path

    def _write_engine_fixture(self, decision: dict) -> Path:
        tmpdir = self._tmpdir_path
        priority_path = tmpdir / "priority_list.json"
        priority_path.write_text(json.dumps({
            "potentials": [
                {"name": "Lucky", "aliases": ["Lucky"]},
                {"name": "OtherA", "aliases": ["OtherA"]},
                {"name": "OtherB", "aliases": ["OtherB"]},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        config_path = tmpdir / "config.yaml"
        config_path.write_text(
            json.dumps({
                "decision": decision,
                "ocr": {"priority_list_path": str(priority_path)},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return config_path

    def tearDown(self) -> None:
        if self._tmpdir_path is not None and self._tmpdir_path.exists():
            shutil.rmtree(self._tmpdir_path, ignore_errors=True)

    def test_handle_potential_select_clicks_card_then_take_button(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("攻擊強化", 0.9, _bbox(10, 10)), ("等級 1→3", 0.9, _bbox(10, 40))],
                    [("防禦強化", 0.9, _bbox(10, 10)), ("等級 1→2", 0.9, _bbox(10, 40))],
                    [("生命強化", 0.9, _bbox(10, 10)), ("等級 1→2", 0.9, _bbox(10, 40))],
                ],
                global_results=[("拿走", 1.0, _bbox(260, 850, 100, 40))],
            ),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            wm=FakeWindowManager(self.frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 2)
        self.assertEqual(ctx.input.clicks[1], (310, 870))
        self.assertEqual(ctx.wm.capture_calls, 1)

    def test_handle_potential_select_single_centered_card_takes_only_card(self) -> None:
        # 實機回放（logs/session_failures/20260613_201218，total_click_count=0）：
        # 免費強化帶出的「選擇一張潛能卡片強化吧!」= 置中單卡（1280x720）。
        # 標題含「強化」→ _detect_expected_card_count 回 2 → _selection_slot_layout
        # 用 2 槽（中心 0.33/0.67），置中卡（cx≈642）落在兩槽空隙 [601,678] →
        # 兩槽抓不到卡 → engine 判 reroll → 此畫面無 reroll 鈕 → 0 點擊 → watchdog 收屍。
        #
        # 真實 OCR 中心座標（讀 tests/replays/ocr_cache）：
        #   標題  cx≈646 cy≈49、卡名「決戰時刻」cx≈642 cy≈381、
        #   等級 cx≈651 cy≈412、「拿走」cx≈641 cy≈606，全部置中於 cx≈0.50w。
        # pending_card_count 刻意設 2（免費強化路徑誤設），證明單卡判定覆寫 hint。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # 重拍 hash 必變
        header_roi = states._selection_header_roi(SimpleNamespace(frame_w=1280, frame_h=720))
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                global_results=[
                    ("選擇一張潛能卡片強化吧!", 0.94, _bbox(487, 31, 318, 36)),   # cx≈646
                    ("決戰時刻", 0.99, _bbox(596, 366, 92, 30)),                 # cx≈642（卡名）
                    ("等級 3→4", 0.35, _bbox(590, 400, 122, 24)),               # cx≈651（含等級標記）
                    ("拿走", 0.51, _bbox(614, 592, 54, 28)),                     # cx≈641（拿走鈕）
                ],
                simple_results={tuple(header_roi): ["選擇一張潛能卡片強化吧!"]},
            ),
            last_frame=frame_before,
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            pending_card_count=2,
            wm=FakeWindowManagerSequence([frame_before, frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        # 核心斷言：修復前=0（紅，judge reroll 找不到鈕），修復後 ≥1（綠，拿下唯一卡）。
        self.assertGreaterEqual(
            len(ctx.input.clicks), 1,
            "置中單卡（選擇一張）被當成 2 卡 → 卡片落槽位空隙 → 0 點擊卡死",
        )
        click_x, _click_y = ctx.input.clicks[0]
        self.assertTrue(
            560 <= click_x <= 740,
            f"首次點擊應落在置中卡（cx≈640），實得 {click_x}",
        )
        # 單卡分支不進 reroll，pending_card_count 應清空。
        self.assertIsNone(ctx.pending_card_count)

    def test_detect_card_count_for_select_one_counts_actual_cards(self) -> None:
        # session 20260614_001041:「選擇一張潛能卡片強化吧!」的「一張」是「挑一張」,
        # 卡數可為 1~3。2188f88 一律當置中單卡 → 遇 2 卡(左右)時點中間空隙(640,302)
        # 卡死。修：數實際卡數（全寬掃等級標記、x 群集）→ 1 置中=1、2 左右=2。
        header_roi = tuple(states._selection_header_roi(SimpleNamespace(frame_w=1280, frame_h=720)))

        def make_ctx(level_bboxes):
            return SimpleNamespace(
                frame_w=1280, frame_h=720,
                last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
                pending_card_count=None,
                ocr=FakeOCR(
                    simple_results={header_roi: ["選擇一張潛能卡片強化吧!"]},
                    global_results=[("等級 4→5", 0.9, b) for b in level_bboxes],
                ),
            )

        # 1 卡置中（cx≈640）→ 1
        ctx_one = make_ctx([_bbox(600, 410, 80, 24)])
        self.assertEqual(states._detect_expected_card_count(ctx_one), 1)
        # 2 卡左右（cx≈430 / ≈740）→ 2（修復前盲回 1）
        ctx_two = make_ctx([_bbox(390, 410, 80, 24), _bbox(700, 410, 80, 24)])
        self.assertEqual(states._detect_expected_card_count(ctx_two), 2)

    def test_handle_lobby_clicks_fast_battle_button(self) -> None:
        frame_before = self.frame.copy()
        frame_after = np.full((1080, 1920, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("快速戰鬥", 1.0, _bbox(1180, 950, 180, 60)),
                ("出發", 1.0, _bbox(1540, 950, 180, 60)),
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            run_count=0,
            max_runs=1,
            # baseline = last_frame(frame_before)；點擊後重拍回 frame_after(!= baseline)
            # -> ExpectRoiChange 首次驗證即過 -> 單次點擊。
            wm=FakeWindowManagerSequence([frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_lobby(ctx)
        self.assertEqual(result, "STATE_FORMATION")
        # 找到「快速戰鬥」-> 點其 OCR 中心；frame 變化 -> ExpectRoiChange 首次驗證即過 -> 單次點擊。
        self.assertEqual(ctx.input.clicks, [(1270, 980)])

    def test_handle_lobby_skips_click_when_fast_battle_text_missing(self) -> None:
        # R3 紅->綠：OCR 找不到「快速戰鬥」時，舊碼 _click_text_or_fallback 會盲點
        # (0.70, 0.90)=(1344, 972)；遷移到 click_verified 後找不到 target -> 零點擊。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            run_count=0,
            max_runs=1,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_lobby(ctx)
        self.assertEqual(result, "STATE_FORMATION")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_potential_select_rerolls_via_q_key_when_engine_returns_none(self) -> None:
        # session 20260613_223142：reroll 鈕是右下角圓形 🔄 icon（無文字、旁標花費
        # 「40」），底部熱鍵列標「Q 更新」。原本走 REROLL_BUTTON_TOKENS 文字點擊永遠
        # 抓不到 icon 文字 → target_not_found → 卡死。新行為：送 Q 鍵（不點擊）。
        engine = SimpleNamespace(decide=lambda options: None)
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("OtherA", 0.9, _bbox(10, 10))],
                    [("OtherB", 0.9, _bbox(10, 10))],
                    [("OtherC", 0.9, _bbox(10, 10))],
                ],
                global_results=[],
            ),
            last_frame=self.frame.copy(),
            engine=engine,
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            reroll_count=0,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.keys, ['q'])
        self.assertEqual(ctx.input.clicks, [])
        # reroll 改記業務計數 → 外部 watchdog 看得到原地推進，不再 30s 誤殺連抽
        # （session 20260613_225241）。
        self.assertEqual(ctx.reroll_count, 1)

    def test_handle_potential_select_reroll_without_keyboard_skips_blind_click(self) -> None:
        # 後備路徑：舊環境無鍵盤能力（input 無 press_key）→ 退回文字點擊；reroll
        # 文字找不到（icon 無文字）→ R3 不盲點右下角（20260602_215757 卡死的盲點）。
        engine = SimpleNamespace(decide=lambda options: None)
        legacy_input = SimpleNamespace(clicks=[])
        legacy_input.click = lambda x, y, delay=0.05: legacy_input.clicks.append((x, y))
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("OtherA", 0.9, _bbox(10, 10))],
                    [("OtherB", 0.9, _bbox(10, 10))],
                    [("OtherC", 0.9, _bbox(10, 10))],
                ],
                global_results=[],
            ),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            engine=engine,
            input=legacy_input,
            frame_w=1280,
            frame_h=720,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(legacy_input.clicks, [])

    def test_handle_potential_select_prefers_short_title_text_over_description(self) -> None:
        self._make_tmpdir()
        engine = DecisionEngine(config_path=str(self._write_engine_fixture({
            "guaranteed": ["Lucky"],
            "required": [],
            "backup": [],
        })))
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [
                        ("Deals 50% damage for 2s", 0.9, _bbox(10, 200)),
                        ("Lucky", 0.9, _bbox(10, 650)),
                    ],
                    [("Long description text", 0.9, _bbox(10, 200)), ("OtherA", 0.9, _bbox(10, 650))],
                    [("Long description text", 0.9, _bbox(10, 200)), ("OtherB", 0.9, _bbox(10, 650))],
                ],
                global_results=[("拿走", 1.0, _bbox(260, 850, 100, 40))],
            ),
            last_frame=self.frame.copy(),
            engine=engine,
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 2)
        self.assertEqual(ctx.input.clicks[0], (320, 453))

    def test_handle_potential_select_retries_before_reroll_when_first_scan_is_unstable(self) -> None:
        self._make_tmpdir()
        engine = DecisionEngine(config_path=str(self._write_engine_fixture({
            "guaranteed": ["Lucky"],
            "required": [],
            "backup": [],
        })))
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("OtherA", 0.9, _bbox(10, 650))],
                    [("OtherB", 0.9, _bbox(10, 650))],
                    [("OtherC", 0.9, _bbox(10, 650))],
                    [("Lucky", 0.9, _bbox(10, 650))],
                    [("OtherA", 0.9, _bbox(10, 650))],
                    [("OtherB", 0.9, _bbox(10, 650))],
                ],
                global_results=[("拿走", 1.0, _bbox(260, 850, 100, 40))],
            ),
            last_frame=self.frame.copy(),
            engine=engine,
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            wm=FakeWindowManager(self.frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 2)
        self.assertEqual(ctx.input.clicks[0], (320, 453))

    def test_extract_slot_options_supports_two_card_layout_hint(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("攻擊強化", 0.9, _bbox(10, 650)), ("等級 1→3", 0.9, _bbox(10, 690))],
                    [("防禦強化", 0.9, _bbox(10, 650)), ("等級 1→2", 0.9, _bbox(10, 690))],
                ],
            ),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280,
            frame_h=720,
            pending_card_count=2,
        )
        options = states._extract_slot_options(ctx)
        self.assertEqual(len(options), 2)
        self.assertEqual([opt.position for opt in options], [(422, 302), (857, 302)])

    def test_card_level_parser_prefers_final_arrow_level_and_ignores_badge_target(self) -> None:
        self.assertEqual(states._parse_level(["推薦6級", "等級 1→3"]), 3)
        self.assertEqual(states._parse_level(["造成傷害 +15%"]), 1)
        self.assertFalse(states._has_level_marker(["推薦6級"]))

    def test_slot_recommendation_requires_badge_text_not_red_card_art(self) -> None:
        frame = self.frame.copy()
        roi = (100, 100, 300, 420)
        frame[100:260, 100:400] = (220, 80, 220)
        recommended, text = states._slot_recommendation_info(
            frame,
            roi,
            [("等級 3", 0.9, _bbox(120, 260, 80, 30))],
        )
        self.assertFalse(recommended)
        self.assertEqual(text, "")

    def test_slot_recommendation_detects_right_top_red_badge_color(self) -> None:
        frame = self.frame.copy()
        roi = (100, 100, 300, 420)
        frame[160:184, 286:356] = (20, 20, 220)
        recommended, text = states._slot_recommendation_info(
            frame,
            roi,
            [("等級 3", 0.9, _bbox(120, 260, 80, 30))],
        )
        self.assertTrue(recommended)
        self.assertEqual(text, "red_badge_color")

    def test_handle_potential_select_take_button_missing_clicks_card_only(self) -> None:
        # Phase 1.3 改寫：舊斷言為「OCR 找不到『拿走』仍盲點選中卡欄位 (1600, 896)」
        # —— R3 備援盲點行為。新安全行為：點卡後找不到拿走鈕就不點，留待下一輪。
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("攻擊強化", 0.9, _bbox(10, 10)), ("等級 1→3", 0.9, _bbox(10, 40))],
                    [("防禦強化", 0.9, _bbox(10, 10)), ("等級 1→2", 0.9, _bbox(10, 40))],
                    [("生命強化", 0.9, _bbox(10, 10)), ("等級 1→2", 0.9, _bbox(10, 40))],
                ],
                global_results=[],
            ),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=2),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [(1600, 453)])

    def test_handle_potential_select_increments_non_pink_card_counter_after_take(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("推薦6級", 0.9, _bbox(10, 10)), ("攻擊強化", 0.9, _bbox(10, 650)), ("等級 3", 0.9, _bbox(10, 690))],
                    [("防禦強化", 0.9, _bbox(10, 650)), ("等級 1", 0.9, _bbox(10, 690))],
                    [("生命強化", 0.9, _bbox(10, 650)), ("等級 1", 0.9, _bbox(10, 690))],
                ],
                global_results=[("拿走", 1.0, _bbox(260, 850, 100, 40))],
            ),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            wm=FakeWindowManager(self.frame),
            card_counter_enabled=True,
            card_counter_current_total=10,
            card_counter_target_total=31,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.card_counter_current_total, 13)

    def test_handle_potential_select_does_not_count_pink_card(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("推薦6級", 0.9, _bbox(10, 10)), ("粉色保底", 0.9, _bbox(10, 650))],
                    [("防禦強化", 0.9, _bbox(10, 650)), ("等級 1", 0.9, _bbox(10, 690))],
                    [("生命強化", 0.9, _bbox(10, 650)), ("等級 1", 0.9, _bbox(10, 690))],
                ],
                global_results=[("拿走", 1.0, _bbox(260, 850, 100, 40))],
            ),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            wm=FakeWindowManager(self.frame),
            card_counter_enabled=True,
            card_counter_current_total=10,
            card_counter_target_total=31,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_potential_select(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.card_counter_current_total, 10)

    def test_handle_note_acquired_updates_current_notes(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[("專注之音 2個 ▶ 5個", 1.0, _bbox(700, 400, 200, 40))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            current_notes={},
        )
        with patch.object(states, "_notes_id_to_name", return_value={"note_1": "專注之音"}):
            result = states.handle_note_acquired(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.current_notes["專注之音"], 5)
        self.assertEqual(len(ctx.input.clicks), 1)

    def test_handle_note_acquired_updates_multiple_notes_and_uses_safe_blank_point(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("專注之音 0個 ▶ 9個", 1.0, _bbox(700, 400, 220, 40)),
                ("火之音 3個 ▶ 6個", 1.0, _bbox(700, 520, 220, 40)),
            ]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            current_notes={},
        )
        with patch.object(states, "_notes_id_to_name", return_value={"note_1": "專注之音", "note_2": "火之音"}):
            result = states.handle_note_acquired(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.current_notes["專注之音"], 9)
        self.assertEqual(ctx.current_notes["火之音"], 6)
        self.assertEqual(ctx.input.clicks[0], (1689, 777))

    def test_handle_note_acquired_resyncs_via_top_row_icons_when_reliable(self) -> None:
        # GAME_MECHANICS D3 強化（best-effort）：「獲得音符」畫面頂列每個音符圖示
        # 旁標當前總量；若頂列圖示模板可靠命中且讀得到數字，覆蓋 current_notes 對應
        # 項（不只變化列），藉此重新同步全部音符總量（類似金錢固定 HUD 覆蓋）。
        # 變化列的「9個」仍是該音符的權威值，頂列不得覆蓋掉變化列已讀到的值。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[("專注之音 0個 ▶ 9個", 1.0, _bbox(700, 400, 220, 40))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=object(),
            frame_w=1920,
            frame_h=1080,
            current_notes={"幸運之音": 99},
        )
        # 頂列圖示命中：幸運之音(總量16)、強攻之音(總量23)、專注之音(總量7,但變化列權威=9)。
        top_row_matches = [
            {"note_name": "幸運之音", "center_x": 400, "center_y": 120, "width": 30, "height": 30, "rect": (385, 105, 415, 135), "confidence": 0.9},
            {"note_name": "強攻之音", "center_x": 520, "center_y": 120, "width": 30, "height": 30, "rect": (505, 105, 535, 135), "confidence": 0.9},
            {"note_name": "專注之音", "center_x": 640, "center_y": 120, "width": 30, "height": 30, "rect": (625, 105, 655, 135), "confidence": 0.9},
        ]
        qty_by_center = {(400, 120): 16, (520, 120): 23, (640, 120): 7}

        def fake_match(ctx_, roi, threshold=0.72):
            return top_row_matches

        def fake_read_number(ctx_, cx, cy, w, h):
            return qty_by_center.get((cx, cy), 0)

        with patch.object(states, "_notes_id_to_name", return_value={"note_1": "專注之音"}), \
             patch.object(states, "_match_note_templates", side_effect=fake_match), \
             patch.object(states, "_read_number_near_rect", side_effect=fake_read_number):
            result = states.handle_note_acquired(ctx)
        self.assertIsNone(result)
        # 變化列權威：專注之音 = 9（頂列的 7 不得覆蓋變化列）。
        self.assertEqual(ctx.current_notes["專注之音"], 9)
        # 頂列 best-effort 重新同步其他音符總量（覆蓋語意）。
        self.assertEqual(ctx.current_notes["幸運之音"], 16)
        self.assertEqual(ctx.current_notes["強攻之音"], 23)

    def test_handle_note_acquired_falls_back_to_change_row_when_top_row_unreliable(self) -> None:
        # 底線：頂列圖示模板不可靠（無命中 / 讀不到數字）時，退回現有變化列覆蓋
        # 路徑，絕不弄壞既有行為（也不得清掉已知音符）。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[("專注之音 0個 ▶ 9個", 1.0, _bbox(700, 400, 220, 40))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=object(),
            frame_w=1920,
            frame_h=1080,
            current_notes={"幸運之音": 99},
        )
        with patch.object(states, "_notes_id_to_name", return_value={"note_1": "專注之音"}), \
             patch.object(states, "_match_note_templates", return_value=[]):
            result = states.handle_note_acquired(ctx)
        self.assertIsNone(result)
        # 變化列覆蓋仍生效。
        self.assertEqual(ctx.current_notes["專注之音"], 9)
        # 頂列無命中 → 不動其他既有音符。
        self.assertEqual(ctx.current_notes["幸運之音"], 99)

    def test_handle_prepare_reads_target_and_current_notes(self) -> None:
        frame_before = self.frame.copy()
        frame_after = np.full((1080, 1920, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(
            ocr=FakeOCR(),
            matcher=None,
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            target_notes={},
            current_notes={},
            wm=FakeWindowManagerSequence([frame_before, frame_after]),
        )
        match_groups = [
            [
                {'note_name': 'NoteLuck', 'center_x': 20, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (10, 10, 30, 30), 'confidence': 0.95},
                {'note_name': 'NoteFocus', 'center_x': 100, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (90, 10, 110, 30), 'confidence': 0.95},
            ],
            [
                {'note_name': 'NoteLuck', 'center_x': 220, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (210, 10, 230, 30), 'confidence': 0.95},
                {'note_name': 'NoteFocus', 'center_x': 300, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (290, 10, 310, 30), 'confidence': 0.95},
            ],
            [
                {'note_name': 'NoteLuck', 'center_x': 420, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (410, 10, 430, 30), 'confidence': 0.95},
                {'note_name': 'NoteFocus', 'center_x': 500, 'center_y': 20, 'width': 20, 'height': 20, 'rect': (490, 10, 510, 30), 'confidence': 0.95},
            ],
            [
                {'note_name': 'NoteLuck', 'center_x': 1120, 'center_y': 260, 'width': 20, 'height': 20, 'rect': (1110, 250, 1130, 270), 'confidence': 0.95},
                {'note_name': 'NoteBurst', 'center_x': 1120, 'center_y': 332, 'width': 20, 'height': 20, 'rect': (1110, 322, 1130, 342), 'confidence': 0.95},
                {'note_name': 'NoteWater', 'center_x': 1120, 'center_y': 404, 'width': 20, 'height': 20, 'rect': (1110, 394, 1130, 414), 'confidence': 0.95},
            ],
        ]
        qty_map = {
            (20, 20): 15,
            (100, 20): 10,
            (220, 20): 15,
            (300, 20): 10,
            (420, 20): 15,
            (500, 20): 10,
            (1120, 260): 4,
            (1120, 332): 6,
            (1120, 404): 23,
        }
        with patch.object(states, '_match_note_templates', side_effect=match_groups),              patch.object(states, '_read_number_near_rect', side_effect=lambda _ctx, x, y, _w, _h: qty_map[(x, y)]),              patch.object(states.time, 'sleep', return_value=None):
            result = states.handle_prepare(ctx)
        self.assertEqual(result, 'STATE_FAST_BATTLE')
        self.assertEqual(ctx.target_notes, {'NoteLuck': 45, 'NoteFocus': 30})
        self.assertEqual(ctx.current_notes, {'NoteLuck': 4, 'NoteBurst': 6, 'NoteWater': 23})

    def test_prepare_target_notes_from_real_frame(self) -> None:
        """D2 啟動條件辨識(實機語料,先紅後綠):glyph+hue 從準備頁三張主位祕紋
        讀出每張需要的音符。語料 ground truth(人工看圖確認 20260614_192942):
          card1 空與花與詩: 風/絕招/強攻/專注
          card2 鹿鳴:       風/幸運/強攻
          card3 春日紀事:    風/絕招/幸運
        合集 = {強攻之音,絕招之音,風之音,專注之音,幸運之音}。
        舊 template-matching 對 16px 圖示失效(回 {}),本測試守住新辨識器。"""
        calib = Path(__file__).resolve().parents[1] / 'tests' / 'replays' / 'notes_calib' / 'prepare_current_20260614_192942.png'
        frame = cv2.imdecode(np.fromfile(str(calib), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(frame, 'calibration frame failed to load (CJK path?)')
        ctx = SimpleNamespace(
            ocr=FakeOCR(),
            matcher=None,
            last_frame=frame,
            frame_w=frame.shape[1],
            frame_h=frame.shape[0],
            target_notes={},
            current_notes={},
        )
        target = states._load_prepare_target_notes(ctx)
        self.assertEqual(
            set(target.keys()),
            {'強攻之音', '絕招之音', '風之音', '專注之音', '幸運之音'},
            f'recognised target notes mismatch: {target}',
        )
        # 每個音符需求數量為正(數量讀取失敗時退預設,不可掉 note)
        for name, qty in target.items():
            self.assertGreater(qty, 0, f'{name} qty must be positive, got {qty}')

    def test_prepare_target_notes_clamps_implausible_count(self) -> None:
        """數量 OCR 讀到爆量(最右圖示把鄰近「Lv 90」等讀進來)→ 退預設,不污染 target_notes。
        L3 20260614_213030 實證 強攻=430;clamp 後應回預設,identity 保留。"""
        ctx = SimpleNamespace(
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280, frame_h=720, ocr=FakeOCR(), matcher=None,
        )
        one = [{'note_name': '強攻之音', 'center_x': 100, 'center_y': 100,
                'width': 26, 'height': 26, 'rect': (87, 87, 113, 113), 'confidence': 0.9}]
        with patch.object(states, '_match_note_templates', side_effect=[one, [], []]), \
             patch.object(states, '_read_number_near_rect', return_value=430):
            target = states._load_prepare_target_notes(ctx)
        self.assertEqual(target, {'強攻之音': states._DEFAULT_ACTIVATION_COUNT})

    def test_handle_prepare_prefers_quick_battle_start_text_over_departure(self) -> None:
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("出發", 1.0, _bbox(900, 650, 90, 40)),
                ("開始戰鬥", 1.0, _bbox(1060, 650, 120, 40)),
            ]),
            matcher=None,
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            target_notes={},
            current_notes={},
            # baseline = last_frame(frame_before)；點擊後重拍回 frame_after(!= baseline) -> 單次點擊。
            wm=FakeWindowManagerSequence([frame_after]),
        )
        with patch.object(states, '_load_prepare_target_notes', return_value={}), \
             patch.object(states, '_load_prepare_current_notes', return_value={}), \
             patch.object(states.time, 'sleep', return_value=None):
            result = states.handle_prepare(ctx)
        self.assertEqual(result, 'STATE_FAST_BATTLE')
        # 點「開始戰鬥」的 OCR 中心而非「出發」；frame 變化 -> 單次點擊。
        self.assertEqual(ctx.input.clicks, [(1120, 670)])

    def test_template_matcher_returns_not_found_when_scene_smaller_than_template(self) -> None:
        scene = np.zeros((60, 60, 3), dtype=np.uint8)
        template = np.zeros((80, 80, 3), dtype=np.uint8)
        matcher = TemplateMatcher(template_dir=str(self._tmp_base / "missing_templates"))
        matcher._cache["oversized"] = template
        result = matcher.match(scene, 'oversized')
        self.assertFalse(result.found)
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.rect, ())

    def test_handle_event_clicks_topmost_right_side_option_instead_of_first_ocr_text(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(slot_results=[
                [
                    ("?????", 1.0, _bbox(120, 30, 260, 60)),
                    ("?????", 1.0, _bbox(120, 200, 260, 60)),
                    ("?????", 1.0, _bbox(120, 370, 260, 60)),
                ],
                [("?????????????", 1.0, _bbox(40, 20, 300, 50))],
            ]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
        )
        result = states.handle_event(ctx)
        self.assertIsNone(result)
        # y 由 427→384：事件選項 ROI 上緣由 0.34→0.30 上移（修「花錢買音符」事件第一列標題
        # 被切掉,L3 20260615_183043）。slot_results 的 bbox 是 ROI 相對座標,故絕對 y 隨
        # ROI origin 上移 Δ43（1080p）。x 與「選最上排右側選項」的選擇邏輯不變。
        self.assertEqual(ctx.input.clicks[0], (1210, 384))

    def test_handle_event_prefers_option_text_over_reward_badge(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(slot_results=[
                [
                    ("????????", 1.0, _bbox(180, 20, 260, 60)),
                    ("??5????????150", 1.0, _bbox(520, 26, 200, 48)),
                    ("???????", 1.0, _bbox(180, 180, 220, 60)),
                    ("??30", 1.0, _bbox(560, 186, 100, 42)),
                ],
                [("???????????????", 1.0, _bbox(40, 20, 360, 50))],
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_event(ctx)
        self.assertIsNone(result)
        # y 由 294→266：事件 ROI 上緣 0.34→0.30（修第一列標題被切,L3 20260615_183043）；
        # slot bbox 為 ROI 相對座標 → 絕對 y 隨 origin 上移 Δ28（720p）。選項>獎勵徽章邏輯不變。
        self.assertEqual(ctx.input.clicks[0], (950, 266))

    def test_handle_event_clicks_option_title_not_reward_detail(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("你也想踏入命運之鏡", 1.0, _bbox(844, 254, 202, 32)),
                ("魔鏡...拜託了!", 1.0, _bbox(756, 342, 130, 26)),
                ("33%機率獲得1個潛能", 1.0, _bbox(711, 387, 154, 20)),
                ("33%機率恢復20%生命值", 1.0, _bbox(875, 387, 178, 20)),
                ("33%機率損失30%生命值", 1.0, _bbox(1063, 387, 178, 20)),
                ("不了，好危險的樣子", 1.0, _bbox(756, 452, 178, 26)),
                ("獲得30", 1.0, _bbox(1167, 497, 70, 20)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks[0], (821, 355))

    def test_handle_event_without_option_text_does_not_click(self) -> None:
        # Phase 1.3 改寫：舊斷言為「OCR 找不到任何選項仍盲點固定列座標 (806, 439)」
        # —— R3 備援盲點行為（REPAIR_PLAN 1.3 紅測試原文要求點擊數為 0）。
        ctx = SimpleNamespace(
            ocr=FakeOCR(slot_results=[[], []]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_event_prefers_free_random_before_money_cost(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(slot_results=[
                [
                    ("隨機獲得潛能", 1.0, _bbox(180, 20, 260, 60)),
                    ("消耗50金幣，獲得潛能", 1.0, _bbox(180, 180, 300, 60)),
                ],
                [("事件問題", 1.0, _bbox(40, 20, 260, 50))],
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_event(ctx)
        self.assertIsNone(result)
        # y 由 294→266：事件 ROI 上緣 0.34→0.30（修第一列標題被切,L3 20260615_183043）；
        # slot bbox 為 ROI 相對座標 → 絕對 y 隨 origin 上移 Δ28（720p）。免費隨機>消耗金錢邏輯不變。
        self.assertEqual(ctx.input.clicks[0], (950, 266))

    def test_handle_event_rejects_note_cost_and_uses_last_safe_row(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(slot_results=[
                [
                    ("消耗5個隨機音符，獲得150💰", 1.0, _bbox(180, 20, 360, 60)),
                    ("不，還是算了。", 1.0, _bbox(180, 180, 220, 60)),
                    ("獲得30💰", 1.0, _bbox(520, 186, 120, 42)),
                ],
                [("這不是你真正喜歡的聲音，對吧？", 1.0, _bbox(40, 20, 420, 50))],
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_event(ctx)
        self.assertIsNone(result)
        # y 由 454→426：事件 ROI 上緣 0.34→0.30（修第一列標題被切,L3 20260615_183043）；
        # slot bbox 為 ROI 相對座標 → 絕對 y 隨 origin 上移 Δ28（720p）。
        # 「拒消耗音符列、改點安全列」的選擇邏輯不變（仍點『不，還是算了。』那列）。
        self.assertEqual(ctx.input.clicks[0], (930, 426))

    # ── 強化事件（一次性升級事件）回放 — 實機 bundle 20260613_191440 ──
    # 證據：logs/session_failures/20260613_191440/last_frame.png（已複製進
    # tests/replays/frames/event_upgrade_rare__20260613_191440.png，labels.json
    # 標 STATE_EVENT）。NPC「你還可以變得更強大…」+ 四選項：
    #   變強! / 變得更強! / 變成最強的存在吧!（稀有潛能，要選這個）/ 已經夠強了。
    # 兩個 bug：(1) 偵測飄移 STATE_EVENT<->STATE_POTENTIAL_SELECT（選項標題不在
    # EVENT_CHOICE_HINTS，畫面含「潛能」+ 底部青色行動按鈕 -> potential_select_visual
    # 誤命中）；(2) handle_event 把三個升級列都算成免費隨機(0,0) 平手挑 row 0 = 變強!。

    def _upgrade_event_drift_ocr(self) -> list[tuple[str, float, tuple]]:
        """飄移情境 OCR：高信心選項標題 + 殘留『潛能』字，低信心『隨機獲得…潛能』reward
        行漏讀（實機 conf ~0.59–0.63 常被丟）。重現 STATE_POTENTIAL_SELECT 誤判。"""
        return [
            ("480", 0.97, _bbox(1201, 23, 40, 20)),
            ("你還可以變得更強大...", 0.95, _bbox(760, 175, 220, 32)),
            ("變強!", 0.98, _bbox(757, 229, 52, 32)),
            ("變得更強!", 0.97, _bbox(762, 339, 80, 32)),
            ("變成最強的存在吧!", 0.97, _bbox(768, 449, 144, 32)),
            ("已經夠強了", 0.97, _bbox(757, 558, 100, 32)),
            ("潛能", 0.40, _bbox(1175, 277, 50, 20)),
            ("潛能", 0.40, _bbox(1175, 497, 50, 20)),
            ("Space", 0.95, _bbox(1015, 689, 40, 16)),
        ]

    def _upgrade_event_full_ocr(self) -> list[tuple[str, float, tuple]]:
        """完整選項列 OCR（PM 電腦控制 + OCR 實讀，中心座標見任務交接）。"""
        return [
            ("變強!", 0.98, _bbox(757, 229, 52, 32)),
            ("消耗120", 0.88, _bbox(1008, 277, 60, 20)),
            ("隨機獲得1個援護潛能", 0.63, _bbox(1077, 277, 180, 20)),
            ("變得更強!", 0.97, _bbox(762, 339, 80, 32)),
            ("消耗160", 0.89, _bbox(999, 387, 60, 20)),
            ("隨機獲得1個主控潛能", 0.60, _bbox(1077, 387, 180, 20)),
            ("變成最強的存在吧!", 0.97, _bbox(768, 449, 144, 32)),
            ("消耗200", 0.88, _bbox(1005, 497, 60, 20)),
            ("隨機獲得1個稀有潛能", 0.59, _bbox(1077, 497, 180, 20)),
            ("已經夠強了", 0.97, _bbox(757, 558, 100, 32)),
            ("獲得30", 0.64, _bbox(1165, 607, 60, 20)),
        ]

    def test_upgrade_event_classifies_as_event_not_potential_select(self) -> None:
        # 紅：選項標題不在 EVENT_CHOICE_HINTS -> event_choice(30) 不命中；真實 frame
        # 有底部青色行動按鈕 + 殘留「潛能」-> potential_select_visual(50) 命中 ->
        # classify == STATE_POTENTIAL_SELECT（飄移根因）。綴後 event_choice 須命中。
        from vision.signatures import classify

        frame_path = (
            Path(__file__).resolve().parent
            / "replays" / "frames" / "event_upgrade_rare__20260613_191440.png"
        )
        data = np.fromfile(str(frame_path), dtype=np.uint8)
        frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
        self.assertIsNotNone(frame, "讀不到強化事件語料圖")

        state, _score, sig = classify(self._upgrade_event_drift_ocr(), frame=frame)
        self.assertEqual(
            state, "STATE_EVENT",
            f"強化事件應判 STATE_EVENT，實得 {state}（signature={getattr(sig, 'name', None)}）",
        )

    def test_handle_event_picks_rare_potential_upgrade_row(self) -> None:
        # 紅：handle_event 把三個升級列都算 (0,0) 免費隨機平手 -> 挑 row 0「變強!」
        # （點到 (1008,287) 一帶的第一列）。修復後須點「變成最強的存在吧!」稀有列
        # 中心（約 (840,465)），絕不可點到「變強!」(783,245) 那列。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=self._upgrade_event_full_ocr()),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_event(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1)
        click_x, click_y = ctx.input.clicks[0]
        # 必須落在「變成最強的存在吧!」那一列（y≈449–481），不是第一列(變強! y≈229–261)
        self.assertTrue(
            440 <= click_y <= 490,
            f"應點稀有列（y≈465），實點 ({click_x},{click_y})",
        )
        # 點到該列的標題或其 reward（稀有潛能）皆可，但絕不可是第一列。
        self.assertGreater(click_y, 300, "不可點到第一列『變強!』(y≈245)")

    def test_do_upgrade_clicks_card_then_take_button(self) -> None:
        # Phase 1.3 改寫：舊斷言 clicks[1] == (633, 896) 是「拿走」OCR 文字落在
        # 選中卡欄位 ROI 之外時的 R3 備援盲點座標（rx=0.33, ry=0.83）。
        # 新安全行為：拿走鈕必須在選中卡欄位 ROI 內被 OCR 命中才點。
        ctx = SimpleNamespace(
            ocr=FakeOCR(
                slot_results=[
                    [("攻擊強化", 0.9, _bbox(10, 10)), ("等級 1→3", 0.9, _bbox(10, 40))],
                    [("防禦強化", 0.9, _bbox(10, 10)), ("等級 1→2", 0.9, _bbox(10, 40))],
                ],
                global_results=[("拿走", 1.0, _bbox(580, 840, 120, 50))],
            ),
            last_frame=self.frame.copy(),
            engine=FakeDecisionEngine(choose_index=0),
            input=FakeInput(),
            frame_w=1920,
            frame_h=1080,
            wm=FakeWindowManager(self.frame),
        )
        with patch.object(states.time, "sleep", return_value=None):
            states._do_upgrade(ctx, times=1)
        self.assertEqual(len(ctx.input.clicks), 2)
        self.assertEqual(ctx.input.clicks[1], (640, 865))
        self.assertEqual(len(ctx.engine.state.recorded), 1)
        self.assertEqual(ctx.wm.capture_calls, 1)

    def test_discount_keyword_helper_accepts_both_keywords(self) -> None:
        self.assertTrue(states._has_discount_keyword("超值優惠"))
        self.assertTrue(states._has_discount_keyword("折扣商品"))
        self.assertFalse(states._has_discount_keyword("普通商品"))

    def test_last_shop_choice_enters_shop(self) -> None:
        ctx = SimpleNamespace(
            shop_visit_count=2,
            current_floor=20,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                ("強化（免費）", 1.0, _bbox(220, 280, 240, 60)),
                ("不要了，直接上樓吧", 1.0, _bbox(220, 450, 260, 60)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertEqual(result, "STATE_SHOP")
        # Phase 2 修：商店三選一改用較寬、較左的 _shop_choice_panel_roi
        # （x-start 0.20 -> rx=256，舊共用 ROI 為 0.50 -> rx=640），slot_results 的
        # 選項中心隨 ROI 偏移左移 384px（640-256）。選的仍是同一個選項，只是座標更靠左。
        self.assertEqual(ctx.input.clicks[0], (596, 394))

    def test_shop_choice_goes_upstairs_after_shop_emptied_streak(self) -> None:
        # session 20260613_232705：買完真商店 + 拿 2 次免費強化後，SHOP_CHOICE 反覆選
        # 「去商店購物」重進已空商店 → buy-all 沒貨 → ESC → 無限迴圈（shop_done 信號在
        # 此交錯流程失效，empirically 讀成 False）。修：buy-all 清空商店累計
        # shop_emptied_streak，SHOP_CHOICE 見 streak>=1 直接上樓（不再重進空商店）。
        ctx = SimpleNamespace(
            shop_visit_count=2,        # visit_count=3 → upgrade_times=0（免費強化已用完）
            shop_done=False,           # 模擬 bug：shop_done 失效為 False
            shop_emptied_streak=1,     # buy-all 已回報「商店空」
            current_floor=20,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                ("不要了，直接上樓吧", 1.0, _bbox(220, 450, 260, 60)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        # enter 分支會回 "STATE_SHOP"；skip（直接上樓）分支回 None 且點一次。
        self.assertIsNone(result, "shop_emptied_streak>=1 應走直接上樓 skip 分支，不再重進空商店")
        self.assertEqual(len(ctx.input.clicks), 1)

    def test_shop_choice_shop_done_does_not_blind_click_enter_when_skip_missing(self) -> None:
        # session 20260614_001957（真根因,推翻 232705 的 shop_done 診斷）:shop_done=True 時
        # skip 分支找「不要了直接上樓」,但該選項沒被 OCR 命中 → _pick_option_from_rows
        # 盲 fallback 回頂部「去商店購物」→ 點 enter 重進空商店 → 無限迴圈。修:keywords
        # 給了卻全不命中應回 None（R3 不盲點頂部選項）,skip 找不到上樓鈕就不點、下輪重判。
        ctx = SimpleNamespace(
            shop_visit_count=3,        # visit=4 → upgrade_times=0（免費強化已用完）
            shop_done=True,
            shop_emptied_streak=1,
            current_floor=20,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                # 模擬該畫面/該拍沒讀到「不要了直接上樓」選項。
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertEqual(
            ctx.input.clicks, [],
            "skip 找不到『直接上樓』時不得盲 fallback 點頂部『去商店購物』重進空商店",
        )

    def test_leave_tower_confirm_classifies_over_tap_continue(self) -> None:
        # session 20260614_004610:點「離開星塔」後跳出確認彈窗「是否離開星塔?」(取消/確認),
        # 含「Space」提示 → 原被判 STATE_TAP_CONTINUE 去點空白(=取消)→ 退回 SHOP_CHOICE 迴圈。
        # 「是否離開星塔」應判 STATE_LEAVE_TOWER_CONFIRM(priority 優先於 npc_dialogue=15)。
        from vision.signatures import classify
        texts = ["提醒", "目前尚有未使用的輝光幣", "是否離開星塔", "Space 確認", "Esc 取消"]
        state, _score, _sig = classify(texts)
        self.assertEqual(state, "STATE_LEAVE_TOWER_CONFIRM")

    def test_handle_leave_tower_confirm_clicks_confirm_not_blank(self) -> None:
        # 確認彈窗要點「確認」離塔,不可點空白(空白=取消 → 迴圈,session 20260614_004610)。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # 點確認後畫面必變
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("確認", 0.97, _bbox(720, 460, 90, 40)),
                ("取消", 0.97, _bbox(380, 460, 90, 40)),
                ("是否離開星塔", 0.95, _bbox(470, 300, 320, 36)),
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_after, frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_leave_tower_confirm(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "應點『確認』離塔")
        # 點的是確認鈕(右下,x≈765),非取消(左,x≈425)。
        self.assertGreater(ctx.input.clicks[0][0], 600, "應點右側『確認』而非左側『取消』")

    def test_handle_discard_confirm_clicks_confirm_not_cancel(self) -> None:
        # Phase 2.3:結算不達標 → 點垃圾桶 → 「解散目前紀錄...是否確定解散?」確認彈窗
        # (取消/確認)。複製 leave_tower_confirm 模式:點「確認」解散該紀錄,不可點取消。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # 點確認後畫面必變
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("確認", 0.97, _bbox(720, 460, 90, 40)),
                ("取消", 0.97, _bbox(380, 460, 90, 40)),
                ("是否確定解散", 0.95, _bbox(470, 300, 320, 36)),
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_after, frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_discard_confirm(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "應點『確認』解散")
        self.assertGreater(ctx.input.clicks[0][0], 600, "應點右側『確認』而非左側『取消』")

    def test_handle_discard_confirm_falls_back_to_space_key(self) -> None:
        # 後備:找不到「確認」文字 → 按 Space(彈窗標 Space 確認;input 有 press_key)。
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[("是否確定解散", 0.95, _bbox(470, 300, 320, 36))]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_discard_confirm(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "找不到確認文字不得盲點")
        self.assertEqual(ctx.input.keys, ['space'])

    def test_handle_discard_confirm_clicks_bottom_button_not_body_confirm_word(self) -> None:
        # 先紅後綠 — L3 20260616_190456 卡死根因（票券版解散確認彈窗）。
        # 此彈窗內文第三行為「是否確認解散?」，**含「確認」二字**且 y 在彈窗中段
        # （真實 OCR center=(636,363)）；底部按鈕「確認」在 y=507。EasyOCR 由上而下
        # 回傳 → 內文「是否確認解散?」排在底部按鈕「確認」之前。原 handle_discard_confirm
        # 用 TextTarget(CONFIRM_BUTTON_TOKENS) 無 ROI 限制 → _resolve_target 取**第一個**
        # 含「確認」的文字 = 內文(636,363) → 點內文無效 → ExpectRoiChange 永不觸發 →
        # 連續 12 次無進度 state_stuck_no_progress。
        # 修復後須點**底部按鈕**「確認」(y≈507)，絕不可點內文(y≈363)。
        # 座標取自真實 OCR dump（diagnostics 對 last_frame.png 實跑）。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # 點底部確認後畫面必變
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                # 內文塊（彈窗中段 y≤382），第三行含「確認」二字 —— 必須排在按鈕之前以重現 bug。
                ("本週可獲得的征途票根數量已達上限", 0.99, _bbox(448, 276, 384, 32)),  # center (640,292)
                ("本次解散紀錄將無法獲得征途票根", 0.99, _bbox(460, 311, 362, 32)),    # center (641,327)
                ("是否確認解散?", 0.98, _bbox(552, 344, 169, 38)),                    # center (636,363) ← 誤點目標
                ("今日不再提醒", 0.99, _bbox(604, 410, 110, 24)),                     # center (659,422) 勾選列
                # 底部按鈕列（y≈507）。
                ("取消", 0.99, _bbox(472, 494, 54, 28)),                             # center (499,508)
                ("確認", 0.99, _bbox(754, 494, 52, 26)),                             # center (780,507) ← 正解
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_after, frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_discard_confirm(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "應點底部『確認』一次")
        click_x, click_y = ctx.input.clicks[0]
        # 核心斷言：必須落在底部按鈕列（y≈507），不可點內文「是否確認解散?」(y≈363)。
        self.assertGreater(
            click_y, 446,
            f"應點底部按鈕『確認』(y≈507)，不可點內文『是否確認解散?』(y≈363)，實點 ({click_x},{click_y})",
        )
        # 底部按鈕「確認」在右側(x≈780)，非左側「取消」(x≈499)。
        self.assertGreater(click_x, 600, "底部『確認』在右側(x≈780)，非左側『取消』")

    def test_handle_discard_confirm_real_frame_clicks_bottom_button(self) -> None:
        # 真 frame + 真 OCR（cache）端到端回放：載入 L3 20260616_190456 的實機截圖與
        # 其真實 OCR dump（tests/replays/frames + ocr_cache），驅動 handle_discard_confirm，
        # 斷言點到底部按鈕「確認」(y≈507) 而非內文「是否確認解散?」(y≈363)。
        frame_path = (
            Path(__file__).resolve().parent
            / "replays" / "frames" / "discard_confirm_ticket__20260616_190456.png"
        )
        frame = cv2.imdecode(np.fromfile(str(frame_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(frame, "讀不到解散確認語料圖")

        cache_path = (
            Path(__file__).resolve().parent
            / "replays" / "ocr_cache" / "discard_confirm_ticket__20260616_190456.png.json"
        )
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        cache_items = [
            (item["text"], float(item["confidence"]),
             tuple((int(x), int(y)) for x, y in item["bbox"]))
            for item in payload["items"]
        ]
        h, w = frame.shape[:2]
        frame_after = np.full((h, w, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=cache_items),
            last_frame=frame,
            input=FakeInput(),
            frame_w=w,
            frame_h=h,
            wm=FakeWindowManagerSequence([frame_after, frame_after]),
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_discard_confirm(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "真 frame 回放應點底部『確認』一次")
        click_x, click_y = ctx.input.clicks[0]
        # 內文「是否確認解散?」center=(636,363)；底部「確認」center=(780,507)。
        self.assertGreater(
            click_y, int(h * 0.62),
            f"真 frame 應點底部按鈕『確認』(y≈507)，不可點內文(y≈363)，實點 ({click_x},{click_y})",
        )
        self.assertGreater(click_x, int(w * 0.5), "底部『確認』在右側")

    def test_shop_choice_final_room_leaves_tower_via_exit_option(self) -> None:
        # session 20260614_003320:bot 史上首次推進到「最終房間」商店(去商店逛逛/強化/
        # 離開星塔)。離場鈕是「離開星塔」(離塔=完成本輪),不在舊 SKIP tokens → 卡死。
        # 加入後 skip 分支應點「離開星塔」離塔(回大廳/結算)。
        ctx = SimpleNamespace(
            shop_visit_count=3,        # visit=4 → upgrade_times=0
            shop_done=True,
            shop_emptied_streak=1,
            current_floor=20,
            current_money=200,
            ocr=FakeOCR(slot_results=[[
                ("最終房間商店大拍賣！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店逛逛", 1.0, _bbox(220, 120, 240, 50)),
                ("強化 (540)", 1.0, _bbox(220, 250, 240, 50)),
                ("離開星塔", 1.0, _bbox(220, 400, 240, 50)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        # 加入「離開星塔」前:SKIP 全不命中 → f1fa515 回 None → 不點(卡死)。
        # 加入後:skip 分支命中「離開星塔」→ 點它離塔(只有 SKIP token 會被 skip 分支選,
        # 故必為離開星塔,非頂部去商店逛逛)。
        self.assertEqual(len(ctx.input.clicks), 1, "最終房間應點『離開星塔』離塔,不得卡死不點")

    def test_first_shop_choice_prefers_free_upgrade_option(self) -> None:
        ctx = SimpleNamespace(
            shop_visit_count=0,
            current_floor=1,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                ("強化（免費）", 1.0, _bbox(220, 280, 240, 60)),
                ("不要了，直接上樓吧", 1.0, _bbox(220, 450, 260, 60)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.shop_visit_count, 1)
        # Phase 2 修：商店三選一 ROI 左移（rx 640 -> 256），slot 選項中心隨之 -384px。
        self.assertEqual(ctx.input.clicks[0], (596, 554))

    def test_second_shop_choice_still_prefers_upgrade_option(self) -> None:
        ctx = SimpleNamespace(
            shop_visit_count=1,
            current_floor=8,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                ("強化（免費）", 1.0, _bbox(220, 280, 240, 60)),
                ("不要了，直接上樓吧", 1.0, _bbox(220, 450, 260, 60)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.shop_visit_count, 2)
        # Phase 2 修：商店三選一 ROI 左移（rx 640 -> 256），slot 選項中心隨之 -384px。
        self.assertEqual(ctx.input.clicks[0], (596, 554))

    def test_handle_shop_choice_hands_potential_screen_back_to_potential_select(self) -> None:
        ctx = SimpleNamespace(
            shop_visit_count=0,
            current_floor=8,
            current_money=0,
            ocr=FakeOCR(global_results=[
                ("隊伍等級提升至3級", 1.0, _bbox(760, 100, 260, 40)),
                ("未收錄", 1.0, _bbox(1380, 170, 100, 30)),
                ("等級 3", 1.0, _bbox(940, 410, 80, 30)),
                ("拿走", 1.0, _bbox(900, 920, 120, 50)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertEqual(result, "STATE_POTENTIAL_SELECT")
        self.assertEqual(ctx.input.clicks, [])
        self.assertEqual(ctx.shop_visit_count, 0)

    def test_handle_shop_choice_hands_visual_potential_screen_back_to_potential_select(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[585:635, 720:910] = (220, 190, 20)
        ctx = SimpleNamespace(
            current_state="STATE_SHOP_CHOICE",
            shop_visit_count=0,
            current_floor=8,
            current_money=0,
            ocr=FakeOCR(global_results=[
                ("隊伍等級提升至3級", 1.0, _bbox(760, 100, 260, 40)),
                ("等級 3", 1.0, _bbox(940, 410, 80, 30)),
                ("強化", 1.0, _bbox(940, 450, 80, 30)),
            ]),
            last_frame=frame,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200):
            result = states.handle_shop_choice(ctx)
        self.assertEqual(result, "STATE_POTENTIAL_SELECT")
        self.assertEqual(ctx.current_state, "STATE_POTENTIAL_SELECT")
        self.assertEqual(ctx.input.clicks, [])
        self.assertEqual(ctx.shop_visit_count, 0)

    def test_handle_potential_select_hands_shop_purchase_modal_back_to_shop(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=[
                ("購買", 1.0, _bbox(362, 178, 48, 28)),
                ("潛能特飲", 1.0, _bbox(550, 238, 80, 24)),
                ("單價", 1.0, _bbox(571, 441, 40, 20)),
                ("購買", 1.0, _bbox(630, 506, 52, 26)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_potential_select(ctx)
        self.assertEqual(result, "STATE_SHOP")
        self.assertEqual(ctx.current_state, "STATE_SHOP")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_potential_select_hands_shop_purchase_modal_without_buy_text_back_to_shop(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=[
                ("潛能特飲", 1.0, _bbox(550, 238, 80, 24)),
                ("可獲得新潛能或提升潛能等級", 1.0, _bbox(547, 279, 206, 20)),
                ("單價", 1.0, _bbox(571, 441, 40, 20)),
                ("Space", 1.0, _bbox(553, 511, 40, 18)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_potential_select(ctx)
        self.assertEqual(result, "STATE_SHOP")
        self.assertEqual(ctx.current_state, "STATE_SHOP")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_potential_select_hands_shop_screen_back_to_shop(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=[
                ("潛能特飲", 1.0, _bbox(671, 277, 62, 20)),
                ("體力之音*5", 1.0, _bbox(663, 475, 78, 20)),
                ("專注之音*5", 1.0, _bbox(811, 475, 80, 20)),
                ("剩餘次數2", 1.0, _bbox(1104, 620, 82, 24)),
                ("背包", 1.0, _bbox(1037, 687, 28, 18)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_potential_select(ctx)
        self.assertEqual(result, "STATE_SHOP")
        self.assertEqual(ctx.current_state, "STATE_SHOP")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_potential_select_hands_mirror_event_back_to_event(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=[
                ("你也想踏入命運之鏡", 1.0, _bbox(844, 254, 202, 32)),
                ("魔鏡...拜託了!", 1.0, _bbox(756, 342, 130, 26)),
                ("33%機率獲得1個潛能", 1.0, _bbox(711, 387, 154, 20)),
                ("不了，好危險的樣子", 1.0, _bbox(756, 452, 178, 26)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_potential_select(ctx)
        self.assertEqual(result, "STATE_EVENT")
        self.assertEqual(ctx.current_state, "STATE_EVENT")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_potential_select_hands_life_wager_event_back_to_event(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_POTENTIAL_SELECT",
            ocr=FakeOCR(global_results=[
                ("生命力也可以成為籌碼嗎", 1.0, _bbox(858, 254, 242, 32)),
                ("那還是分我一些吧", 1.0, _bbox(760, 342, 170, 26)),
                ("恢復20%生命值", 1.0, _bbox(1125, 388, 120, 20)),
                ("試試也不是不行", 1.0, _bbox(760, 452, 178, 26)),
                ("消耗30%生命值，隨機獲得1個潛能", 1.0, _bbox(995, 497, 250, 20)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
        )
        result = states.handle_potential_select(ctx)
        self.assertEqual(result, "STATE_EVENT")
        self.assertEqual(ctx.current_state, "STATE_EVENT")
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_shop_confirms_purchase_modal_and_counts_pending_card(self) -> None:
        ctx = SimpleNamespace(
            current_state="STATE_SHOP",
            ocr=FakeOCR(global_results=[
                ("購買", 1.0, _bbox(362, 178, 48, 28)),
                ("潛能特飲", 1.0, _bbox(550, 238, 80, 24)),
                ("單價", 1.0, _bbox(571, 441, 40, 20)),
                ("購買", 1.0, _bbox(630, 506, 52, 26)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            pending_shop_card_level=3,
            pending_shop_card_text="流速紊亂 等級3",
            pending_shop_card_slot_key=states._shop_slot_key(630, 420),
            shop_purchased_slots=set(),
            card_counter_enabled=True,
            card_counter_current_total=6,
            card_counter_target_total=78,
        )
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [(656, 519)])
        self.assertEqual(ctx.card_counter_current_total, 9)
        self.assertIsNone(ctx.pending_shop_card_level)
        self.assertIsNone(ctx.pending_shop_card_text)
        self.assertIsNone(ctx.pending_shop_card_slot_key)
        self.assertIn(states._shop_slot_key(630, 420), ctx.shop_purchased_slots)

    def test_select_shop_card_skips_already_purchased_slot(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("潛能特飲 等級1", 1.0, _bbox(650, 275, 120, 32)),
                ("流速紊亂 等級3", 1.0, _bbox(850, 275, 120, 32)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280,
            frame_h=720,
            shop_purchased_slots=set(),
        )
        ctx.shop_purchased_slots.add(states._shop_slot_key(710, 291, ctx))
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, level, text = selected
        self.assertEqual((cx, cy), (910, 291))
        self.assertEqual(level, 3)
        self.assertIn("流速紊亂", text)

    def test_select_shop_card_grouped_fallback_skips_only_purchased_slot(self) -> None:
        ctx = SimpleNamespace(
            ocr=FakeOCR(global_results=[
                ("等級", 1.0, _bbox(650, 275, 60, 24)),
                ("6", 1.0, _bbox(716, 275, 16, 24)),
                ("風蝕環劫", 1.0, _bbox(650, 325, 90, 28)),
                ("120", 1.0, _bbox(650, 375, 52, 24)),
                ("等級", 1.0, _bbox(850, 275, 60, 24)),
                ("3", 1.0, _bbox(916, 275, 16, 24)),
                ("流速紊亂", 1.0, _bbox(850, 325, 90, 28)),
                ("120", 1.0, _bbox(850, 375, 52, 24)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280,
            frame_h=720,
            shop_purchased_slots=set(),
        )
        ctx.shop_purchased_slots.add(states._shop_slot_key(680, 339, ctx))
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, level, text = selected
        self.assertEqual((cx, cy), (895, 339))
        self.assertEqual(level, 3)
        self.assertIn("流速紊亂", text)

    def test_handle_shop_marks_pending_slot_unavailable_when_purchase_modal_never_appears(self) -> None:
        pending_key = "pending-slot"
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[
                ("潛能特飲 等級1", 1.0, _bbox(650, 275, 120, 32)),
            ]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1280,
            frame_h=720,
            card_counter_enabled=True,
            card_counter_current_total=20,
            card_counter_target_total=31,
            pending_shop_card_level=1,
            pending_shop_card_text="潛能特飲 等級1",
            pending_shop_card_slot_key=pending_key,
            shop_purchased_slots=set(),
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states, "_settle_and_refresh", return_value=False), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [])
        self.assertIn(pending_key, ctx.shop_purchased_slots)
        self.assertIsNone(ctx.pending_shop_card_level)
        self.assertIsNone(ctx.pending_shop_card_text)
        self.assertIsNone(ctx.pending_shop_card_slot_key)

    def test_handle_shop_refreshes_via_q_key_when_under_limit(self) -> None:
        # 2.4(使用者實機確認 2026-06-14):商店刷新貨架走快捷鍵 Q（同選卡 reroll），不是
        # 找「刷新」文字點擊（刷新鈕多半無文字 icon）。真經濟卡片達標（current>=target,
        # cards_only）+ 無音符 → handle_shop 不買任何東西 → 走到刷新 gate;
        # max_shop_refresh=1 且 refresh_count=0 → 按 Q 刷新、refresh_count++、清本店已購格位。
        # 修前（文字點擊）FakeOCR 無「刷新」→ 抓不到 → keys 不含 'q'（紅）;修後按 Q（綠）。
        ctx = SimpleNamespace(
            shop_buy_strategy='cards_only',
            card_counter_current_total=78,
            card_counter_target_total=78,
            current_money=500,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 1}},
            frame_w=1280,
            frame_h=720,
            shop_purchased_slots={"slot-a", "slot-b"},
        )
        with patch.object(states, "_read_money_via_icon", return_value=500), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.keys, ['q'])           # 按 Q 刷新（非文字點擊）
        self.assertEqual(ctx.input.clicks, [])            # 刷新不產生點擊
        self.assertEqual(ctx.shop_refresh_count, 1)       # 計數 ++（已在 progress_counters → 不被誤殺）
        self.assertEqual(ctx.shop_purchased_slots, set()) # 清本店已購 → 可重買新貨

    def test_handle_shop_no_refresh_when_limit_reached(self) -> None:
        # 2.4:刷新次數達上限（refresh_count>=max_shop_refresh）→ 不再按 Q，改設 shop_done +
        # 走 ESC 離場上樓（不無限刷新）。回歸:確認上限 gate 生效。
        ctx = SimpleNamespace(
            shop_buy_strategy='cards_only',
            card_counter_current_total=78,
            card_counter_target_total=78,
            current_money=500,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=1,
            config={"bot": {"max_shop_refresh": 1}},
            frame_w=1280,
            frame_h=720,
            shop_purchased_slots=set(),
        )
        with patch.object(states, "_read_money_via_icon", return_value=500), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertNotIn('q', ctx.input.keys)        # 達上限不再刷新
        self.assertEqual(ctx.input.keys, ['esc'])    # 改走 ESC 離場上樓
        self.assertTrue(ctx.shop_done)               # 設 shop_done → SHOP_CHOICE 選上樓
        self.assertEqual(ctx.shop_refresh_count, 1)  # 次數不變

    def test_shop_choice_skip_uses_bottom_option_instead_of_prompt(self) -> None:
        ctx = SimpleNamespace(
            shop_visit_count=2,
            current_floor=10,
            current_money=0,
            ocr=FakeOCR(slot_results=[[
                ("遇到了星塔商店，去購物吧！", 1.0, _bbox(80, 10, 280, 40)),
                ("去商店購物", 1.0, _bbox(220, 120, 240, 60)),
                ("強化（免費）", 1.0, _bbox(220, 280, 240, 60)),
                ("不要了，直接上樓吧", 1.0, _bbox(220, 450, 260, 60)),
            ]]),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=100),              patch.object(states, "_should_enter_shop", return_value=False):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        # Phase 2 修：商店三選一 ROI 左移（rx 640 -> 256），slot 選項中心隨之 -384px。
        self.assertEqual(ctx.input.clicks[0], (606, 724))

    def test_shop_choice_finds_left_aligned_options_outside_old_roi(self) -> None:
        # 實機回放（logs/session_failures/20260613_195227，total_click_count=0）：
        # 商店三選一的選項文字偏左（cx≈458–551），落在修復前的選項搜尋 ROI
        # （_choice_panel_roi x-start=0.50 → x:[640..1216]）左邊外面 →
        # _select_shop_choice_option 找不到 → handle_shop_choice 回 None → R3 不點
        # → 0 點擊 → 外部 watchdog 30s 收屍。
        #
        # 用 global_results（FakeOCR 會依 roi 中心過濾，slot_results 不會）餵實機
        # 真 OCR 證實的四個選項中心座標。修復前：選項全被 ROI 濾掉 → 0 點擊（紅）；
        # 修復後（放寬 ROI x-start）：找得到「強化（免費）」→ 至少 1 次點擊，
        # 且點擊 x 落在某個真選項中心（cx 458–551）。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # hash 必變 → ExpectRoiChange 過
        ctx = SimpleNamespace(
            current_state="STATE_SHOP_CHOICE",
            shop_visit_count=0,   # → visit_count=1 → _shop_upgrade_times=2 → 走 upgrade 分支
            current_floor=5,
            current_money=0,
            ocr=FakeOCR(global_results=[
                ("遇到了星塔商店", 0.99, _bbox(510, 196, 160, 32)),   # 標題列（cy≈212，在選項 ROI 上緣外）
                ("去購物吧!", 0.98, _bbox(680, 196, 108, 32)),
                ("去商店購物", 0.90, _bbox(426, 284, 102, 28)),       # cx≈477
                ("強化（免費）", 0.45, _bbox(426, 394, 136, 30)),     # cx≈494 ← upgrade 分支會挑這個
                ("不要了", 0.99, _bbox(426, 504, 64, 26)),            # cx≈458（最左）
                ("直接上樓吧", 0.99, _bbox(500, 504, 102, 26)),       # cx≈551
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_before, frame_after]),
            config={"bot": {}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=0), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        # 核心斷言：修復前這裡是 0（紅），修復後 ≥1。
        self.assertGreaterEqual(
            len(ctx.input.clicks), 1,
            "偏左的商店三選一選項落在搜尋 ROI 外時，handler 找不到選項 → 0 點擊卡死",
        )
        click_x, _click_y = ctx.input.clicks[0]
        self.assertTrue(
            458 <= click_x <= 551,
            f"點擊應落在某個真選項中心（cx 458–551），實得 {click_x}",
        )

    def test_handle_shop_clicks_discount_without_blind_confirm(self) -> None:
        # Phase 1.3 改寫：舊測試以 patch _click_text_or_fallback 斷言「點完優惠後
        # 無條件補點『確認』備援座標」—— R3 盲點行為（當前 frame 上根本沒有確認字樣）。
        # 新安全行為：優惠商品照點（OCR 證據座標）；「確認」改為重拍後驗證式點擊，
        # 畫面上找不到該文字時不點。
        money_values = iter([100, 100, 100])
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[("今日優惠", 1.0, _bbox(200, 300, 100, 40))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
        )
        with patch.object(states, "_read_money_via_icon", side_effect=lambda *args, **kwargs: next(money_values)), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [(250, 320)])

    def test_handle_shop_buys_all_cards_before_notes_when_counter_unmet(self) -> None:
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={"火之音": 10},
            current_notes={"火之音": 5},
            ocr=FakeOCR(global_results=[
                ("火之音x5 72", 1.0, _bbox(300, 320, 160, 48)),
                ("等級 6 風蝕環劫 80", 1.0, _bbox(620, 420, 180, 48)),   # 卡價 80 <= 餘額 100(買得起)
            ]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
            card_counter_enabled=True,
            card_counter_current_total=20,
            card_counter_target_total=31,
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.card_counter_current_total, 20)
        self.assertEqual(ctx.current_notes["火之音"], 5)
        self.assertGreaterEqual(len(ctx.input.clicks), 1)
        self.assertEqual(ctx.pending_shop_card_level, 6)
        self.assertIn("風蝕環劫", ctx.pending_shop_card_text)
        self.assertTrue(ctx.pending_shop_card_slot_key)

    def test_handle_shop_hands_event_choice_screen_back_to_event(self) -> None:
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[
                ("音樂，也是治療的方法。", 1.0, _bbox(700, 250, 240, 40)),
                ("我獨愛這些。", 1.0, _bbox(720, 340, 180, 40)),
                ("獲得5個幸運之音", 1.0, _bbox(1100, 380, 180, 40)),
            ]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
            card_counter_enabled=True,
            card_counter_current_total=20,
            card_counter_target_total=31,
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertEqual(result, "STATE_EVENT")
        self.assertEqual(ctx.input.clicks, [])
        self.assertEqual(ctx.card_counter_current_total, 20)

    def test_handle_shop_does_not_treat_price_only_number_as_card(self) -> None:
        # Phase 1.3 改寫：舊測試末行 assertTrue(helper_calls) 斷言「離開商店」走
        # _click_text_or_fallback 備援盲點路徑。新安全行為：畫面上找不到
        # 「離開/返回」文字時不點擊（零點擊已由原斷言涵蓋）。
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[("120", 1.0, _bbox(620, 420, 70, 36))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
            card_counter_enabled=True,
            card_counter_current_total=20,
            card_counter_target_total=31,
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.card_counter_current_total, 20)
        self.assertEqual(ctx.input.clicks, [])

    def test_handle_shop_returns_to_discount_logic_after_card_counter_met(self) -> None:
        # Phase 1.3 改寫：舊測試以 patch _click_text_or_fallback 斷言補點「確認」
        # 備援盲點（同 discount 測試的理由）。新安全行為只保證優惠商品被點擊。
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=[
                ("等級 6 風蝕環劫 120", 1.0, _bbox(620, 420, 180, 48)),
                ("今日優惠", 1.0, _bbox(200, 300, 100, 40)),
            ]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
            card_counter_enabled=True,
            card_counter_current_total=31,
            card_counter_target_total=31,
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.card_counter_current_total, 31)
        self.assertEqual(ctx.input.clicks, [(250, 320)])

    def test_handle_shop_buying_gap_note_does_not_accumulate_current_notes(self) -> None:
        # GAME_MECHANICS D3（2026-06-14 拍板）：音符總數一律由「獲得音符」畫面
        # （STATE_NOTE_ACQUIRED）覆蓋讀取，shop 端不可累加。買音符後遊戲一定會
        # 跳 STATE_NOTE_ACQUIRED 覆蓋新總量（L3 log 證實），shop 再 += 會重複計。
        # 新行為：音符商品照點（OCR 證據座標）以觸發購買，但 current_notes 不動。
        ctx = SimpleNamespace(
            current_money=100,
            current_floor=19,
            target_notes={"火之音": 10},
            current_notes={"火之音": 5},
            ocr=FakeOCR(global_results=[("火之音x5 72", 1.0, _bbox(300, 420, 160, 48))]),
            last_frame=self.frame.copy(),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1920,
            frame_h=1080,
        )
        with patch.object(states, "_read_money_via_icon", return_value=100), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        # shop 不再累加：總量維持原值，等買後的 STATE_NOTE_ACQUIRED 畫面覆蓋。
        self.assertEqual(ctx.current_notes["火之音"], 5)
        self.assertEqual(ctx.input.clicks, [(380, 444)])

    # ── 測試模式 shop_buy_all（買全部：特飲 + 音符，去重，買完離開）──
    # 實機商店版面真實座標（1280x720，goods ROI ≈ x[320..1242] y[72..590]）。

    def _shop_buy_all_ctx(self, goods: list[tuple], **overrides) -> SimpleNamespace:
        ctx = SimpleNamespace(
            current_state="STATE_SHOP",
            current_money=999,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=list(goods)),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1280,
            frame_h=720,
            card_counter_enabled=False,   # continue-run 強制關閉（真因）
            card_counter_current_total=0,
            card_counter_target_total=0,
            pending_shop_card_level=None,
            pending_shop_card_text=None,
            pending_shop_card_slot_key=None,
            shop_purchased_slots=set(),
            shop_buy_all=True,
        )
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def test_shop_buy_all_selects_drinks_and_notes_skipping_purchased(self) -> None:
        # _select_shop_good_to_buy_any 必須同時把「潛能特飲」與「音符」視為可買，
        # 且永遠跳過已在 shop_purchased_slots 的格位。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),   # Row0 特飲 cx≈671 cy≈287
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),       # Row1 音符 cx≈702 cy≈485
        ]
        ctx = self._shop_buy_all_ctx(goods)
        drink_slot = states._shop_slot_key(671, 287, ctx)
        note_slot = states._shop_slot_key(702, 485, ctx)
        self.assertNotEqual(drink_slot, note_slot)

        # 第一格未購：兩格皆可選，且選到的是真貨格（非 UI）。
        first = states._select_shop_good_to_buy_any(ctx)
        self.assertIsNotNone(first)

        # 標記特飲格已購 → 下一次必選音符格（證明音符也買、且去重生效）。
        ctx.shop_purchased_slots.add(drink_slot)
        second = states._select_shop_good_to_buy_any(ctx)
        self.assertIsNotNone(second)
        cx2, cy2, _level2, text2 = second
        self.assertEqual(states._shop_slot_key(cx2, cy2, ctx), note_slot)
        self.assertIn("之音", text2)

        # 兩格皆已購 → 無可買 → 回 None。
        ctx.shop_purchased_slots.add(note_slot)
        self.assertIsNone(states._select_shop_good_to_buy_any(ctx))

    def test_handle_shop_buy_all_clicks_distinct_slots_and_never_repeats(self) -> None:
        # 逐格買、不重買：handle_shop 在 shop_buy_all=True 下，依序點不同 slot，
        # 已購格永不再被點；特飲與音符都會被選到。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),   # drink A cx≈671
            ("流速紊亂 等級3", 1.0, _bbox(781, 271, 80, 32)),   # drink B cx≈821
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),       # note  cx≈702
        ]
        ctx = self._shop_buy_all_ctx(goods)
        slot_a = states._shop_slot_key(671, 287, ctx)
        slot_b = states._shop_slot_key(821, 287, ctx)
        slot_note = states._shop_slot_key(702, 485, ctx)
        all_slots = {slot_a, slot_b, slot_note}
        self.assertEqual(len(all_slots), 3)

        clicked_slots: list[str] = []
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            for _ in range(3):
                pre_clicks = len(ctx.input.clicks)
                result = states.handle_shop(ctx)
                self.assertIsNone(result)
                self.assertEqual(len(ctx.input.clicks), pre_clicks + 1,
                                 "每輪 shop_buy_all 應點正好一格")
                # 取剛點的 slot（pending_shop_card_slot_key 由買卡分支設定）。
                slot = ctx.pending_shop_card_slot_key
                self.assertIsNotNone(slot)
                self.assertNotIn(slot, clicked_slots, "已點過的 slot 不得重點")
                clicked_slots.append(slot)
                # 模擬購買彈窗 confirm 成功：把該 slot 記進已購集、清 pending。
                ctx.shop_purchased_slots.add(slot)
                states._clear_pending_shop_card(ctx)

        self.assertEqual(set(clicked_slots), all_slots,
                         "特飲與音符三格都應各被點一次（買全部）")

    def test_leave_shop_presses_esc(self) -> None:
        # 遊戲離開商店是按 ESC 鍵（使用者實機確認）。_leave_shop 應送 ESC，不點文字。
        ctx = self._shop_buy_all_ctx([])
        result = states._leave_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.keys, ['esc'],
                         "離開商店應按 ESC 鍵（不是點文字）")
        self.assertEqual(ctx.input.clicks, [],
                         "ESC 離場不應產生任何點擊（不灌水 click_count）")

    def test_leave_shop_falls_back_to_text_without_esc_capability(self) -> None:
        # 後備：舊環境 input 無 press_esc → 退回文字離場，且不爆。
        class _NoEscInput:
            def __init__(self) -> None:
                self.clicks: list[tuple[int, int]] = []

            def click(self, x: int, y: int, delay: float = 0.05) -> None:
                self.clicks.append((x, y))

        goods = [("坦回", 0.21, _bbox(1099, 686, 40, 20))]   # 右下返回鈕（OCR 糊字）
        ctx = self._shop_buy_all_ctx(goods, input=_NoEscInput())
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx.wm = FakeWindowManagerSequence([ctx.last_frame, frame_after])
        with patch.object(states.time, "sleep", return_value=None):
            result = states._leave_shop(ctx)
        self.assertIsNone(result)
        # 無 ESC 能力 → 退回點離開/返回文字（含「坦回」變體）。
        self.assertGreaterEqual(len(ctx.input.clicks), 1,
                                "無 ESC 能力時應退回點離開/返回文字")

    def test_handle_shop_buy_all_leaves_via_esc_when_everything_purchased(self) -> None:
        # 全買完 → _select_shop_good_to_buy_any 回 None → _leave_shop 被呼叫 →
        # 按 ESC 離場（遊戲設計，使用者實機確認），不點文字。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),
        ]
        ctx = self._shop_buy_all_ctx(goods)
        # 先把兩個真貨格標為已購。
        ctx.shop_purchased_slots.add(states._shop_slot_key(671, 287, ctx))
        ctx.shop_purchased_slots.add(states._shop_slot_key(702, 485, ctx))
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx.wm = FakeWindowManagerSequence([ctx.last_frame, frame_after])

        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        # 應按 ESC 離場，不點任何文字。
        self.assertEqual(ctx.input.keys, ['esc'],
                         "全買完後應按 ESC 離開商店")
        self.assertEqual(ctx.input.clicks, [],
                         "ESC 離場不應產生任何點擊")

    # ── shop_done 信號驅動上樓（修「無限重進空商店」迴圈）──
    # 實機 session 20260613_221637：商店買完（八格全售完）→ ESC → SHOP_CHOICE →
    # _should_enter_shop(floor, money, upgrade_price=0) 永遠回 True → 又選「去商店購物」
    # → 重進空商店 → … 燒到 600s。修法：buy-all 沒東西可買時設 ctx.shop_done=True，
    # handle_shop_choice 見 shop_done=True 直接選「不要了直接上樓」。

    def _shop_choice_shop_done_ctx(self, **overrides) -> SimpleNamespace:
        # 三選項皆落在 _shop_choice_panel_roi（x:[256..1216] y:[244..604]）內、
        # 且在 exclude_top_ratio=0.10 下緣（cy>280）之下，確保都被掃到。
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)  # hash 必變 → ExpectRoiChange 過
        ctx = SimpleNamespace(
            current_state="STATE_SHOP_CHOICE",
            shop_done=True,
            shop_visit_count=2,   # → visit_count=3 → _shop_upgrade_times=0 → 不走免費強化分支
            current_floor=10,
            current_money=0,
            ocr=FakeOCR(global_results=[
                ("去商店購物", 0.95, _bbox(440, 296, 100, 28)),       # cx≈490 cy≈310
                ("強化（免費）", 0.95, _bbox(440, 406, 120, 28)),     # cx≈500 cy≈420
                ("不要了，直接上樓吧", 0.99, _bbox(440, 516, 180, 28)),  # cx≈530 cy≈530
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            matcher=None,
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_before, frame_after]),
            config={"bot": {}},
        )
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def test_shop_choice_shop_done_goes_upstairs_not_into_shop(self) -> None:
        ctx = self._shop_choice_shop_done_ctx()
        with patch.object(states, "_read_money_via_icon", return_value=0), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        # shop_done=True → 不再進商店（不回 STATE_SHOP），點「不要了直接上樓」那一列。
        self.assertNotEqual(result, "STATE_SHOP",
                            "shop_done=True 時不應再選『去商店購物』進商店")
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "應點『不要了直接上樓』")
        # 點到的應是「不要了，直接上樓吧」那一列（cy≈530），不是「去商店購物」（cy≈310）。
        _click_x, click_y = ctx.input.clicks[0]
        self.assertGreater(click_y, 470,
                           f"應點『不要了直接上樓』列（cy≈530），實得 cy={click_y}（疑似誤點上方『去商店購物』）")
        # 點擊 verified 成功（離開 SHOP_CHOICE）→ shop_done 重置為 False，讓下一層商店重新開始。
        self.assertFalse(ctx.shop_done, "點上樓 verified 成功後 shop_done 應重置為 False")

    def test_shop_choice_shop_done_keeps_flag_when_click_not_verified(self) -> None:
        # 點「不要了直接上樓」verified 失敗（畫面沒變）→ 保持 shop_done=True，
        # 下一輪重試上樓，不 fallback 去重進商店。
        frame_same = np.zeros((720, 1280, 3), dtype=np.uint8)
        ctx = self._shop_choice_shop_done_ctx(
            last_frame=frame_same,
            wm=FakeWindowManagerSequence([frame_same, frame_same]),  # hash 不變 → ExpectRoiChange 失敗
        )
        with patch.object(states, "_read_money_via_icon", return_value=0), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertNotEqual(result, "STATE_SHOP")
        self.assertTrue(ctx.shop_done,
                        "上樓 verified 失敗時 shop_done 應保持 True（下一輪重試，不重進商店）")

    def test_handle_shop_buy_all_sets_shop_done_when_nothing_left(self) -> None:
        # buy-all 所有 slot 已購（_select_shop_good_to_buy_any 回 None）→ 設 shop_done=True，
        # 且走 _leave_shop（按 ESC）。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),
        ]
        ctx = self._shop_buy_all_ctx(goods, shop_done=False)
        ctx.shop_purchased_slots.add(states._shop_slot_key(671, 287, ctx))
        ctx.shop_purchased_slots.add(states._shop_slot_key(702, 485, ctx))
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx.wm = FakeWindowManagerSequence([ctx.last_frame, frame_after])
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertTrue(ctx.shop_done, "沒東西可買 → shop_done 應設為 True")
        self.assertEqual(ctx.input.keys, ['esc'], "沒東西可買後應按 ESC 離開商店")

    def test_handle_shop_buy_all_keeps_shop_done_false_when_goods_available(self) -> None:
        # buy-all 有未購商品 → 買它、shop_done 維持/設為 False（商店還有貨）。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),
        ]
        ctx = self._shop_buy_all_ctx(goods, shop_done=True)  # 先污染成 True，證明有貨時被改回 False
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(len(ctx.input.clicks), 1, "有貨應點一格買")
        self.assertFalse(ctx.shop_done, "商店還有貨時 shop_done 應為 False")

    # ── Part 1：升級機（強化）可調（config shop.upgrade）──

    def test_shop_upgrade_times_reads_config_times_by_visit(self) -> None:
        # config 提供 times_by_visit → 依設定回傳；超出設定的造訪次數 = 0。
        cfg = {"shop": {"upgrade": {"enabled": True, "times_by_visit": {1: 1, 2: 4, 3: 2}}}}
        ctx = SimpleNamespace(config=cfg)
        self.assertEqual(states._shop_upgrade_times(ctx, 1), 1)
        self.assertEqual(states._shop_upgrade_times(ctx, 2), 4)
        self.assertEqual(states._shop_upgrade_times(ctx, 3), 2)
        self.assertEqual(states._shop_upgrade_times(ctx, 4), 0)

    def test_shop_upgrade_times_falls_back_to_default_when_no_config(self) -> None:
        # 無 shop.upgrade.times_by_visit → 退回 {1:2, 2:3} 預設。
        ctx = SimpleNamespace(config={"bot": {}})
        self.assertEqual(states._shop_upgrade_times(ctx, 1), 2)
        self.assertEqual(states._shop_upgrade_times(ctx, 2), 3)
        self.assertEqual(states._shop_upgrade_times(ctx, 3), 0)

    def test_shop_upgrade_disabled_returns_zero(self) -> None:
        # shop.upgrade.enabled=False → 一律 0（不強化）。
        cfg = {"shop": {"upgrade": {"enabled": False, "times_by_visit": {1: 2}}}}
        ctx = SimpleNamespace(config=cfg)
        self.assertEqual(states._shop_upgrade_times(ctx, 1), 0)

    def test_parse_upgrade_price_extracts_number(self) -> None:
        # 「強化 (120C)」→ 120；「強化（150）」→ 150。
        self.assertEqual(states._parse_upgrade_price("強化 (120C)"), 120)
        self.assertEqual(states._parse_upgrade_price("強化（150）"), 150)

    def test_parse_upgrade_price_free_is_zero(self) -> None:
        # 「強化（免費6）」/「強化（免費）」→ 免費 = 0（一律強化）。
        self.assertEqual(states._parse_upgrade_price("強化（免費6）"), 0)
        self.assertEqual(states._parse_upgrade_price("強化（免費）"), 0)

    def test_parse_upgrade_price_no_info_returns_none(self) -> None:
        # 純「強化」無價格資訊 → None（呼叫端視為未知→照常強化）。
        self.assertIsNone(states._parse_upgrade_price("強化"))

    def _shop_choice_upgrade_ctx(self, upgrade_text: str, **overrides) -> SimpleNamespace:
        frame_before = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame_after = np.full((720, 1280, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(
            current_state="STATE_SHOP_CHOICE",
            shop_visit_count=0,   # → visit_count=1 → upgrade_times>0
            current_floor=5,
            current_money=0,
            shop_done=False,
            shop_emptied_streak=0,
            ocr=FakeOCR(global_results=[
                ("去商店購物", 0.95, _bbox(440, 296, 100, 28)),       # cy≈310
                (upgrade_text, 0.95, _bbox(440, 406, 160, 28)),       # cy≈420 ← 強化列
                ("不要了，直接上樓吧", 0.99, _bbox(440, 516, 180, 28)),  # cy≈530
            ]),
            last_frame=frame_before,
            input=FakeInput(),
            matcher=None,
            frame_w=1280,
            frame_h=720,
            wm=FakeWindowManagerSequence([frame_before, frame_after]),
            config={"shop": {"upgrade": {"enabled": True, "times_by_visit": {1: 2},
                                         "price_ceiling": 540}}},
        )
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def test_shop_choice_upgrades_when_price_below_ceiling(self) -> None:
        # 強化單價 120 < ceiling 540 → 點「強化」列（cy≈420）。
        ctx = self._shop_choice_upgrade_ctx("強化 (120C)")
        with patch.object(states, "_read_money_via_icon", return_value=200), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "120<540 應點強化")
        _click_x, click_y = ctx.input.clicks[0]
        self.assertTrue(390 <= click_y <= 450,
                        f"應點『強化』列（cy≈420），實得 cy={click_y}")

    def test_shop_choice_skips_upgrade_when_price_at_or_above_ceiling(self) -> None:
        # 強化單價 600 >= ceiling 540 → 不強化，往下走（去商店/上樓），不點強化列。
        ctx = self._shop_choice_upgrade_ctx("強化 (600C)")
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        # 不該點到強化列（cy≈420）。應改去商店購物（cy≈310）或上樓。
        for _cx, cy in ctx.input.clicks:
            self.assertFalse(390 <= cy <= 450,
                             f"600>=540 不該點強化列，實得 cy={cy}")
        self.assertIsNone(getattr(ctx, "pending_card_count", None),
                          "略過強化時不該設 pending_card_count=2")

    def test_shop_choice_upgrades_when_free_even_if_ceiling_low(self) -> None:
        # 免費一律強化（即使 ceiling 設很低）。
        ctx = self._shop_choice_upgrade_ctx(
            "強化（免費6）",
            config={"shop": {"upgrade": {"enabled": True, "times_by_visit": {1: 2},
                                         "price_ceiling": 1}}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=0), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "免費應一律強化")
        _click_x, click_y = ctx.input.clicks[0]
        self.assertTrue(390 <= click_y <= 450,
                        f"免費應點『強化』列（cy≈420），實得 cy={click_y}")

    def test_shop_choice_order_shop_first_enters_shop_before_upgrade(self) -> None:
        # order=shop_first：第一次遇商店先「去商店購物」（即使 upgrade_times>0）。
        # 進商店走 ExpectStateIn(STATE_SHOP) 驗證 → frame_after 需 classify 成 STATE_SHOP，
        # 故 OCR 含 shop 簽名字（潛能特飲 + 剩餘次,置於選項 ROI 上緣外 cy<244 不干擾選項挑選）。
        ctx = self._shop_choice_upgrade_ctx(
            "強化（免費）",
            ocr=FakeOCR(global_results=[
                ("潛能特飲", 0.95, _bbox(440, 90, 100, 28)),         # shop 簽名（ROI 外）
                ("剩餘次數 3", 0.95, _bbox(640, 90, 120, 28)),       # shop 簽名（ROI 外）
                ("去商店購物", 0.95, _bbox(440, 296, 100, 28)),       # cy≈310
                ("強化（免費）", 0.95, _bbox(440, 406, 160, 28)),     # cy≈420
                ("不要了，直接上樓吧", 0.99, _bbox(440, 516, 180, 28)),  # cy≈530
            ]),
            config={"shop": {
                "upgrade": {"enabled": True, "times_by_visit": {1: 2}, "price_ceiling": 540},
                "order": "shop_first",
            }},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        # shop_first：尚未逛過商店 → 先選「去商店購物」進商店（回 STATE_SHOP）。
        self.assertEqual(result, "STATE_SHOP",
                         "order=shop_first 第一次應先進商店再強化")
        _click_x, click_y = ctx.input.clicks[0]
        self.assertTrue(290 <= click_y <= 340,
                        f"應點『去商店購物』列（cy≈310），實得 cy={click_y}")

    def test_shop_choice_order_shop_first_upgrades_after_shop_done(self) -> None:
        # order=shop_first：商店逛完（shop_done=True）後回 SHOP_CHOICE → 才做強化。
        ctx = self._shop_choice_upgrade_ctx(
            "強化（免費）",
            shop_done=True,
            shop_emptied_streak=1,
            config={"shop": {
                "upgrade": {"enabled": True, "times_by_visit": {1: 2}, "price_ceiling": 540},
                "order": "shop_first",
            }},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "逛完商店後應做強化")
        _click_x, click_y = ctx.input.clicks[0]
        self.assertTrue(390 <= click_y <= 450,
                        f"shop_first 逛完後應點『強化』列（cy≈420），實得 cy={click_y}")

    def test_shop_choice_default_order_is_upgrade_first(self) -> None:
        # 無 order 設定 → 預設 upgrade_first：第一次先強化（不先進商店）。
        ctx = self._shop_choice_upgrade_ctx(
            "強化（免費）",
            config={"shop": {"upgrade": {"enabled": True, "times_by_visit": {1: 2},
                                         "price_ceiling": 540}}},
        )
        with patch.object(states, "_read_money_via_icon", return_value=200), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop_choice(ctx)
        self.assertIsNone(result)
        _click_x, click_y = ctx.input.clicks[0]
        self.assertTrue(390 <= click_y <= 450,
                        f"預設 upgrade_first 應先點『強化』列（cy≈420），實得 cy={click_y}")

    # ── Part 2：商店買法「真經濟」（config shop.buy.strategy）──

    def _strategy_shop_ctx(self, goods: list[tuple], strategy: str, **overrides) -> SimpleNamespace:
        ctx = SimpleNamespace(
            current_state="STATE_SHOP",
            current_money=999,
            current_floor=19,
            target_notes={},
            current_notes={},
            ocr=FakeOCR(global_results=list(goods)),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            input=FakeInput(),
            matcher=None,
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            frame_w=1280,
            frame_h=720,
            card_counter_enabled=True,
            card_counter_current_total=0,
            card_counter_target_total=78,
            pending_shop_card_level=None,
            pending_shop_card_text=None,
            pending_shop_card_slot_key=None,
            shop_purchased_slots=set(),
            shop_buy_strategy=strategy,
        )
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def test_strategy_cards_then_notes_buys_card_before_target(self) -> None:
        # cards_then_notes 且未達 78 → 買卡片（特飲），不買音符。
        goods = [
            ("等級 6 風蝕環劫 120", 1.0, _bbox(620, 271, 180, 48)),   # 卡片 cy≈295
            ("專注之音x5 72", 1.0, _bbox(380, 469, 160, 48)),         # 音符 cy≈493
        ]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20)
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.pending_shop_card_level, 6, "未達標應買卡片")
        self.assertIn("風蝕環劫", ctx.pending_shop_card_text)

    def test_strategy_cards_then_notes_buys_gap_note_after_target_met(self) -> None:
        # cards_then_notes 且已達 78 → 改買協奏缺口音符。
        goods = [("專注之音x5 72", 1.0, _bbox(380, 420, 160, 48))]
        ctx = self._strategy_shop_ctx(
            goods, "cards_then_notes",
            card_counter_current_total=78,
            target_notes={"專注之音": 10},
            current_notes={"專注之音": 5},
        )
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        # 達標 → 不買卡片（pending 卡片不應被設）。
        self.assertIsNone(ctx.pending_shop_card_slot_key, "達標後不應再買卡片")
        # 應點到缺口音符（cy≈444）。
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "達標後應買缺口音符")
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(420 <= cy <= 470, f"應點缺口音符列，實得 cy={cy}")
        # D3：shop 端不累加音符總量。
        self.assertEqual(ctx.current_notes["專注之音"], 5)

    def test_gap_note_slot_deduped_no_rebuy_loop(self) -> None:
        """買缺口音符要去重:本店此格已買過 → 不重買 → 離場(不卡死)。
        L3 20260614_213030 實證:買缺口音符無去重 → 連點同張「強攻之音*15」12 次
        → state_stuck_no_progress。買卡片那段早有去重,此段漏。"""
        goods = [("強攻之音*15 90", 1.0, _bbox(780, 461, 140, 48))]  # center≈(850,485)
        ctx = self._strategy_shop_ctx(
            goods, "cards_then_notes",
            card_counter_current_total=78,
            target_notes={"強攻之音": 30},
            current_notes={"強攻之音": 0},
        )
        ctx.shop_purchased_slots.add(states._shop_slot_key(850, 485, ctx))  # 預標已購
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "缺口音符格已購 → 不應重點(防卡死)")
        self.assertEqual(ctx.input.keys, ['esc'], "無新可買 → ESC 離場")
        self.assertTrue(getattr(ctx, "shop_done", False), "本店無可買 → shop_done 應設")

    def test_strategy_cards_only_never_buys_notes(self) -> None:
        # cards_only：達標後不買音符（即使有缺口），離場。
        goods = [("專注之音x5 72", 1.0, _bbox(380, 420, 160, 48))]
        ctx = self._strategy_shop_ctx(
            goods, "cards_only",
            card_counter_current_total=78,
            target_notes={"專注之音": 10},
            current_notes={"專注之音": 5},
        )
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "cards_only 達標後不買音符")
        self.assertEqual(ctx.input.keys, ['esc'], "cards_only 無可買 → ESC 離場")

    def test_strategy_notes_only_never_buys_cards(self) -> None:
        # notes_only：即使卡片未達 78，也不買卡片，只買缺口音符。
        goods = [
            ("等級 6 風蝕環劫 120", 1.0, _bbox(620, 271, 180, 48)),   # 卡片
            ("專注之音x5 72", 1.0, _bbox(380, 420, 160, 48)),         # 音符 cy≈444
        ]
        ctx = self._strategy_shop_ctx(
            goods, "notes_only",
            card_counter_current_total=20,
            target_notes={"專注之音": 10},
            current_notes={"專注之音": 5},
        )
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertIsNone(ctx.pending_shop_card_slot_key, "notes_only 不買卡片")
        self.assertGreaterEqual(len(ctx.input.clicks), 1, "notes_only 應買缺口音符")
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(420 <= cy <= 470, f"應點缺口音符列，實得 cy={cy}")

    def test_strategy_all_uses_buy_all_path(self) -> None:
        # strategy=all → 走 _handle_shop_buy_all（買全部：特飲 + 音符，去重）。
        goods = [
            ("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32)),
            ("專注之音x5", 1.0, _bbox(662, 469, 80, 32)),
        ]
        ctx = self._strategy_shop_ctx(goods, "all",
                                      card_counter_current_total=0,
                                      card_counter_target_total=0)
        with patch.object(states, "_handle_shop_buy_all", return_value=None) as buy_all, \
             patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        buy_all.assert_called_once()

    def test_shop_buy_all_flag_still_routes_to_buy_all(self) -> None:
        # 向後相容：舊 shop_buy_all=True 旗標仍走 buy-all 路徑。
        goods = [("潛能特飲 等級1", 1.0, _bbox(631, 271, 80, 32))]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes", shop_buy_all=True)
        with patch.object(states, "_handle_shop_buy_all", return_value=None) as buy_all, \
             patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        buy_all.assert_called_once()

    # ── 真經濟商店「本店無可買」emptied 信號（修無限重進迴圈,L3 20260614_162359）──
    # 真經濟 handle_shop 走 _leave_shop 卻從不設 shop_done/shop_emptied_streak →
    # handle_shop_choice 永遠 shop_done=False emptied=0 → _try_enter_shop 重進空商店
    # → visit_count 無限累加。buy-all 路徑 _handle_shop_buy_all 早有此信號,真經濟漏。

    def test_real_economy_shop_signals_emptied_when_nothing_buyable(self) -> None:
        # cards_then_notes 未達標(想買卡)但唯一卡片格已購 → 無可買 → 離場時應設
        # shop_done=True + emptied_streak>=1（讓 SHOP_CHOICE 上樓,不重進空商店）。
        goods = [("潛能特飲 等級1 200", 1.0, _bbox(640, 271, 140, 36))]   # center≈(710,289)
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20)
        ctx.shop_purchased_slots.add(states._shop_slot_key(710, 289, ctx))  # 預標已購
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "全已購 → 不點任何卡")
        self.assertTrue(getattr(ctx, "shop_done", False),
                        "本店無可買 → shop_done 應設 True")
        self.assertGreaterEqual(int(getattr(ctx, "shop_emptied_streak", 0) or 0), 1,
                                "本店無可買 → emptied_streak 應 >=1")
        self.assertEqual(ctx.input.keys, ['esc'], "無可買 → ESC 離場")

    def test_real_economy_shop_keeps_active_signal_while_buying(self) -> None:
        # 有可買卡片 → 買它,且不可誤設 shop_done（商店還有貨,不應觸發上樓)。
        # 先污染 shop_done=True/emptied=2,證明買得到貨時被改回 active。
        goods = [("潛能特飲 等級1 120", 1.0, _bbox(640, 271, 140, 36))]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20,
                                      shop_done=True, shop_emptied_streak=2)
        with patch.object(states, "_read_money_via_icon", return_value=999), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertIsNotNone(ctx.pending_shop_card_slot_key, "有貨應買卡")
        self.assertFalse(getattr(ctx, "shop_done", True),
                         "買得到貨時 shop_done 應為 False")
        self.assertEqual(int(getattr(ctx, "shop_emptied_streak", 9)), 0,
                         "買得到貨時 emptied_streak 應歸 0")

    # ── 真經濟「買得起才點」affordability 過濾（L3 20260614_162359:錢剩130還空點貴卡）──
    # 點卡前讀卡片單價 vs current_money,買不起的不點(讓真正無可買→emptied→上樓)。
    # 保守設計:價格糊字/天文數字 → 視為未知 → 照點(讓購買 modal 把關),零回歸。

    def test_select_shop_card_skips_unaffordable_and_picks_affordable(self) -> None:
        # 錢=130:貴卡(200)在前須被跳過,便宜卡(90)在後應被選。
        goods = [
            ("潛能特飲 200", 1.0, _bbox(360, 271, 120, 36)),   # center≈(420,289) 貴格
            ("潛能特飲 90", 1.0, _bbox(900, 271, 120, 36)),    # center≈(960,289) 便宜格
        ]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20,
                                      current_money=130)
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected)
        cx, cy, _level, _text = selected
        self.assertEqual(states._shop_slot_key(cx, cy, ctx),
                         states._shop_slot_key(960, 289, ctx),
                         "應跳過買不起的 200、選買得起的 90")

    def test_select_shop_card_buys_when_price_unknown_or_garbled(self) -> None:
        # 價格 OCR 黏連成天文數字(298100,>上限)→ 不可信 → 視為未知 → 照買(modal 把關)。
        goods = [("潛能特飲 298100", 1.0, _bbox(640, 271, 140, 36))]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20,
                                      current_money=130)
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected, "價格不可信時不應跳過(改由購買 modal 把關)")

    def test_select_shop_card_affordability_disabled_buys_expensive(self) -> None:
        # config shop.buy.affordability=False → 不過濾價格,照舊買(可關閉防誤殺)。
        goods = [("潛能特飲 400", 1.0, _bbox(640, 271, 140, 36))]
        ctx = self._strategy_shop_ctx(
            goods, "cards_then_notes", card_counter_current_total=20,
            current_money=130,
            config={"bot": {"max_shop_refresh": 0},
                    "shop": {"buy": {"affordability": False}}})
        selected = states._select_shop_card_to_buy(ctx)
        self.assertIsNotNone(selected, "affordability=False → 不過濾價格,照舊買")

    def test_real_economy_broke_shop_signals_emptied_via_affordability(self) -> None:
        # L3 20260614_162359 完整重現:錢=130,潛能特飲卡全買不起(160/200/400),
        # cards_then_notes 未達標 → 真的無可買 → 不空點任何卡 + 設 emptied + ESC 離場。
        goods = [
            ("潛能特飲 160", 1.0, _bbox(360, 271, 120, 36)),
            ("潛能特飲 200", 1.0, _bbox(640, 271, 120, 36)),
            ("潛能特飲 400", 1.0, _bbox(900, 271, 120, 36)),
        ]
        ctx = self._strategy_shop_ctx(goods, "cards_then_notes",
                                      card_counter_current_total=20,
                                      current_money=130)
        with patch.object(states, "_read_money_via_icon", return_value=130), \
             patch.object(states.time, "sleep", return_value=None):
            result = states.handle_shop(ctx)
        self.assertIsNone(result)
        self.assertEqual(ctx.input.clicks, [], "全買不起 → 不空點任何卡")
        self.assertTrue(getattr(ctx, "shop_done", False),
                        "全買不起=本店無可買 → shop_done 應 True")
        self.assertGreaterEqual(int(getattr(ctx, "shop_emptied_streak", 0) or 0), 1)
        self.assertEqual(ctx.input.keys, ['esc'], "無可買 → ESC 離場")

    def _make_settlement_ctx(self, potentials_ok: bool, notes_ok: bool) -> SimpleNamespace:
        # 風險 #1+#6:結算只在達標時計 success;達標判定走 ctx 的兩個 satisfied 方法。
        return SimpleNamespace(
            ocr=FakeOCR(global_results=[]),
            input=FakeInput(),
            last_frame=self.frame.copy(),
            frame_w=1920,
            frame_h=1080,
            run_count=0,
            success_count=0,
            max_runs=1,
            running=True,
            required_potentials_satisfied=lambda: potentials_ok,
            current_notes_satisfied=lambda: notes_ok,
        )

    def test_settlement_counts_success_only_when_targets_met(self) -> None:
        ctx = self._make_settlement_ctx(potentials_ok=True, notes_ok=True)
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_settlement(ctx)
        self.assertEqual(ctx.run_count, 1)
        self.assertEqual(ctx.success_count, 1)

    def test_settlement_no_success_when_targets_unmet(self) -> None:
        # 模擬續跑空 target_notes:潛能達標但音符未達標 -> 不算 success。
        ctx = self._make_settlement_ctx(potentials_ok=True, notes_ok=False)
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_settlement(ctx)
        self.assertEqual(ctx.run_count, 1)
        self.assertEqual(ctx.success_count, 0)

        # 反向:潛能未達標但音符達標 -> 同樣不算 success。
        ctx2 = self._make_settlement_ctx(potentials_ok=False, notes_ok=True)
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_settlement(ctx2)
        self.assertEqual(ctx2.run_count, 1)
        self.assertEqual(ctx2.success_count, 0)

    def test_settlement_stops_running_when_max_runs_reached(self) -> None:
        ctx = self._make_settlement_ctx(potentials_ok=False, notes_ok=False)
        with patch.object(states.time, "sleep", return_value=None):
            result = states.handle_settlement(ctx)
        self.assertFalse(ctx.running)
        self.assertIsNone(result)

    def test_settlement_skips_click_when_return_text_missing(self) -> None:
        # R3 紅->綠：OCR 找不到「返回/確認」時，舊碼 _click_text_or_fallback 會盲點
        # (0.50, 0.90)=(960, 972)；遷移到 click_verified 後找不到 target -> 零點擊。
        # 注意：run_count/success_count 邏輯不受影響（target 未命中 -> 直接 return False）。
        ctx = self._make_settlement_ctx(potentials_ok=True, notes_ok=True)
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_settlement(ctx)
        self.assertEqual(ctx.input.clicks, [])
        # 結算計數仍照常推進（click 與否互不影響）。
        self.assertEqual(ctx.run_count, 1)
        self.assertEqual(ctx.success_count, 1)
