"""
Phase 1.1 簽名單一來源測試 (tests/test_signatures.py)

語料庫驗證：讀 tests/replays/labels.json + tests/replays/ocr_cache/，
逐張斷言「評分最高（priority 優先、再比分數）的 signature == 標籤狀態」。

注意：
  - 本檔不得初始化 EasyOCR；OCR 結果一律來自 ocr_cache（由
    diagnostics/run_detector_on_corpus.py 預先生成）。
  - unknown__* 語料（Phase 1.2）：classify 無命中（None）視為 STATE_UNKNOWN，
    必須與標籤一致 —— 34/34 全對，無豁免。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPLAYS_DIR = PROJECT_ROOT / "tests" / "replays"
FRAMES_DIR = REPLAYS_DIR / "frames"
CACHE_DIR = REPLAYS_DIR / "ocr_cache"


def _imread_unicode(path: Path):
    import cv2
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _load_labels() -> dict[str, str]:
    return json.loads((REPLAYS_DIR / "labels.json").read_text(encoding="utf-8"))


def _load_cache_items(file_name: str) -> list[tuple[str, float, tuple]]:
    payload = json.loads((CACHE_DIR / f"{file_name}.json").read_text(encoding="utf-8"))
    return [
        (
            item["text"],
            float(item["confidence"]),
            tuple((int(x), int(y)) for x, y in item["bbox"]),
        )
        for item in payload["items"]
    ]


class SignatureCorpusTests(unittest.TestCase):
    """L1 語料庫整批驗證：每張已標籤截圖的最高分 signature 必須等於標籤狀態。"""

    def test_every_labeled_frame_classifies_to_its_label(self) -> None:
        from vision.signatures import classify

        labels = _load_labels()
        self.assertGreaterEqual(len(labels), 30, "labels.json 應涵蓋全部語料")

        failures: list[str] = []
        checked = 0
        for file_name, label in labels.items():
            cache_path = CACHE_DIR / f"{file_name}.json"
            self.assertTrue(
                cache_path.exists(),
                f"缺少 OCR cache：{cache_path}（先跑 diagnostics/run_detector_on_corpus.py）",
            )
            items = _load_cache_items(file_name)
            frame = _imread_unicode(FRAMES_DIR / file_name)
            self.assertIsNotNone(frame, f"讀不到語料圖 {file_name}")

            state, score, signature = classify(items, frame=frame)
            resolved = state if state is not None else "STATE_UNKNOWN"
            checked += 1
            if resolved != label:
                sig_name = getattr(signature, "name", None)
                failures.append(
                    f"{file_name}: expected {label}, got {resolved} "
                    f"(signature={sig_name}, score={score:.2f})"
                )

        self.assertEqual(checked, len(labels), "全部語料（含 unknown__*）都必須驗證")

        self.assertEqual(
            failures,
            [],
            f"{len(failures)}/{checked} 張語料誤判：\n" + "\n".join(failures),
        )


class SignatureScoringUnitTests(unittest.TestCase):
    """直接對評分函式的單元測試（不依賴語料圖）。"""

    def test_negative_keyword_vetoes_signature(self) -> None:
        from vision.signatures import ScreenSignature, score_signature

        sig = ScreenSignature(
            name="t",
            state="STATE_X",
            priority=1,
            keywords_any=("拿走",),
            negative_keywords=("單價",),
        )
        self.assertGreaterEqual(score_signature(sig, ["拿走"]), 1.0)
        self.assertEqual(score_signature(sig, ["拿走", "單價 160"]), 0.0)

    def test_keywords_all_requires_every_group(self) -> None:
        from vision.signatures import ScreenSignature, score_signature

        sig = ScreenSignature(
            name="t",
            state="STATE_X",
            priority=1,
            keywords_all=(("購買", "购买"), ("單價", "特飲")),
        )
        self.assertEqual(score_signature(sig, ["購買"]), 0.0)
        self.assertGreaterEqual(score_signature(sig, ["購買", "單價"]), 1.0)
        self.assertGreaterEqual(score_signature(sig, ["购买", "特飲"]), 1.0)

    def test_classify_prefers_lower_priority_number(self) -> None:
        from vision.signatures import ScreenSignature, classify

        sigs = (
            ScreenSignature(name="late", state="STATE_B", priority=200, keywords_any=("甲",)),
            ScreenSignature(name="early", state="STATE_A", priority=10, keywords_any=("甲",)),
        )
        state, _score, sig = classify(["甲"], signatures=sigs)
        self.assertEqual(state, "STATE_A")
        self.assertEqual(sig.name, "early")

    def test_concert_skill_activate_screen_classifies_tap_continue_not_shop(self) -> None:
        from vision.signatures import classify

        # session 20260613_225933：bot 過選卡關後撞「啟動協奏技能!」音符畫面（置中
        # 單卡「流淌於夢的清晨」+ 底部「點擊空白處繼續」）。header 含「協奏技能」→ 原
        # shop_keywords(priority 140)誤判 STATE_SHOP → bot 跑 buy-all + ESC 離場全無效
        # → 30s stuck。應判 STATE_TAP_CONTINUE（handle_tap_continue 點空白推進）。
        concert_texts = [
            "啟動協奏技能!",
            "流淌於夢的清晨",
            "全隊風係傷害提升2.5%",
            "點擊空白處繼續",
        ]
        state, _score, _sig = classify(concert_texts)
        self.assertEqual(state, "STATE_TAP_CONTINUE")

        # 回歸：真商店（潛能特飲 + 控制列 +「相關協奏技能」描述）仍判 STATE_SHOP，
        # 不被新簽名搶走。
        shop_texts = ["潛能特飲", "相關協奏技能", "剩餘次數", "背包"]
        shop_state, _s2, _sig2 = classify(shop_texts)
        self.assertEqual(shop_state, "STATE_SHOP")

    def test_rapport_boost_screen_classifies_tap_continue(self) -> None:
        from vision.signatures import classify

        # session 20260614_173024：bot 推到最終房間→點「離開星塔」(conf 0.98)後,出現
        # 全新「默契提升」獎勵畫面(角色+右側獎勵圖示),無簽名 → 先被 potential_select_visual
        # 弱色錨誤判、再 STATE_UNKNOWN×4 → state_unknown_persistent 卡死。
        # 使用者證實:此畫面點空白推進 → 應判 STATE_TAP_CONTINUE(handle_tap_continue 點空白)。
        state, _score, _sig = classify(["默契提升"])
        self.assertEqual(state, "STATE_TAP_CONTINUE")

        # 回歸：真結算畫面(儲存紀錄/評分)仍判 STATE_RESULT,不被新簽名搶走。
        result_state, _s2, _sig2 = classify(["儲存紀錄", "未命名紀錄", "評分"])
        self.assertEqual(result_state, "STATE_RESULT")

    def test_result_records_screen_not_misjudged_as_potential_select(self) -> None:
        from vision.signatures import classify, has_bottom_teal_action_button

        # L3 20260614_220443:bot 點「離開星塔」→「確認」後出現結算/紀錄畫面(角色 +
        # 「潛能收集/收藏」「祕紋技能」分頁 + 底部彩色潛能格 + 右下「儲存紀錄」)。該畫面底部
        # 彩色格偶讓 potential_select_visual 的弱青色色錨命中(ratio 0.039 剛過 0.035 門檻),
        # 加「潛能收集」含「潛能」→ 被 potential_select_visual(priority 50)搶在 result_keywords
        # (110)前誤判 STATE_POTENTIAL_SELECT → 連續 reroll/取最佳卡皆找不到「拿走」鈕 → 卡死。
        # 應否決選卡視覺簽名 → 落到 result_keywords 判 STATE_RESULT(handle_result 點「儲存紀錄」)。

        # 合成一張「底部青色色錨會命中」的 frame,重現 live 的偶然命中條件(純文字 classify
        # 不帶 frame 時色錨永遠 False、測不出此 bug)。
        h, w = 720, 1280
        teal_frame = np.zeros((h, w, 3), dtype=np.uint8)
        teal_frame[int(h * 0.72):int(h * 0.92), int(w * 0.28):int(w * 0.82)] = (255, 255, 0)  # BGR 青
        self.assertTrue(
            has_bottom_teal_action_button(teal_frame),
            "測試前提:合成 frame 必須命中底部青色色錨,才能重現誤判",
        )

        # 結算/紀錄畫面實機 OCR 字(含「潛能收集」分頁 → 觸發 potential_select_visual 的「潛能」)。
        result_texts = ["未命名紀錄", "潛能收集", "祕紋技能", "評分", "儲存紀錄", "主位祕紋"]
        state, _score, sig = classify(result_texts, frame=teal_frame)
        self.assertEqual(
            state, "STATE_RESULT",
            f"結算畫面應判 STATE_RESULT,得 {state}(signature={getattr(sig, 'name', None)})",
        )

        # 回歸：真潛能選卡(純視覺簽名:潛能卡片詞 + 底部青色,無任何結算標記字)仍判
        # STATE_POTENTIAL_SELECT —— 否決字只該打結算畫面,不可誤傷正常選卡視覺判定。
        ps_state, _s2, ps_sig = classify(["潛能", "等級"], frame=teal_frame)
        self.assertEqual(ps_state, "STATE_POTENTIAL_SELECT")
        self.assertEqual(getattr(ps_sig, "name", None), "potential_select_visual")

    def test_discard_confirm_ticket_warning_classifies_discard_not_result(self) -> None:
        from vision.signatures import classify

        # L3 20260615_001511:完整一輪到結算(rating 23 不達標)→ 解鎖 → 點垃圾桶 → 跳「征途票根
        # 達上限」解散確認彈窗(使用者截圖:此版在票券積到上限才出,問句是「是否確認解散?」)。
        # 原 DISCARD_CONFIRM_TOKENS 只有「是否確定解散」(確定≠確認)→ 漏判 → 判 STATE_RESULT
        # (背景結算字)→ 又點垃圾桶(被彈窗蓋住=空白)→ 死循環 12 次 stuck。應判 STATE_DISCARD_CONFIRM。
        discard_texts = [
            "提醒",
            "本週可獲得的征途票根數量已達上限",
            "本次解散紀錄將無法獲得征途票根",
            "是否確認解散?",
            "今日不再提醒", "取消", "確認",
            # 彈窗蓋在結算畫面上,背景結算字仍被 OCR 讀到(priority 須贏 result_keywords=110)
            "儲存紀錄", "未命名紀錄", "評分", "潛能收集",
        ]
        state, _score, _sig = classify(discard_texts)
        self.assertEqual(state, "STATE_DISCARD_CONFIRM")

        # 回歸:純結算畫面(無解散彈窗)仍判 STATE_RESULT,不被新 token 誤搶。
        result_state, _s2, _sig2 = classify(["儲存紀錄", "未命名紀錄", "評分"])
        self.assertEqual(result_state, "STATE_RESULT")

    def test_classify_returns_none_when_nothing_hits(self) -> None:
        from vision.signatures import classify

        state, score, sig = classify(["完全無關的文字"], signatures=())
        self.assertIsNone(state)
        self.assertEqual(score, 0.0)
        self.assertIsNone(sig)

    def test_keyword_match_normalizes_spacing_and_case(self) -> None:
        from vision.signatures import ScreenSignature, score_signature

        sig = ScreenSignature(
            name="t",
            state="STATE_X",
            priority=1,
            keywords_any=("Reroll",),
        )
        self.assertGreaterEqual(score_signature(sig, ["RE ROLL"]), 1.0)

    def test_roi_filters_out_texts_outside_region(self) -> None:
        from vision.signatures import ScreenSignature, score_signature

        sig = ScreenSignature(
            name="t",
            state="STATE_X",
            priority=1,
            keywords_any=("拿走",),
            roi=(0.0, 0.5, 1.0, 1.0),  # 只看下半畫面
        )
        top_bbox = ((10, 10), (60, 10), (60, 30), (10, 30))
        bottom_bbox = ((10, 900), (60, 900), (60, 930), (10, 930))
        self.assertEqual(
            score_signature(sig, [("拿走", 0.9, top_bbox)], frame_size=(1920, 1080)),
            0.0,
        )
        self.assertGreaterEqual(
            score_signature(sig, [("拿走", 0.9, bottom_bbox)], frame_size=(1920, 1080)),
            1.0,
        )


class SignatureSingleSourceTests(unittest.TestCase):
    """結構性驗收：提示字常數只能存在於 vision/signatures.py。"""

    def test_state_detector_has_no_duplicate_hint_constants(self) -> None:
        source = (PROJECT_ROOT / "vision" / "state_detector.py").read_text(encoding="utf-8")
        for banned in ("EVENT_CHOICE_HINTS = ", "POTENTIAL_SELECT_HINTS = ",
                       "POTENTIAL_CARD_HINTS = ", "STATE_KEYWORDS: dict", "STATE_KEYWORDS = "):
            self.assertNotIn(banned, source, f"state_detector.py 不應再定義 {banned.strip(' =:')}")

    def test_core_states_has_no_duplicate_hint_literals(self) -> None:
        source = (PROJECT_ROOT / "core" / "states.py").read_text(encoding="utf-8")
        for banned in ("我獨愛", "命運之鏡", "好危險", "未收錄", "單價", "体力之音"):
            self.assertNotIn(
                banned, source,
                f"core/states.py 不應再內嵌畫面提示字「{banned}」（應 import vision.signatures）",
            )


if __name__ == "__main__":
    unittest.main()
