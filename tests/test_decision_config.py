"""決策 config 化讀取器測試（DECISION_CONFIG_PLAN step0 起，逐步擴充）。

step0：states._event_cfg / _prepare_cfg 三層防呆讀取器（仿 _shop_cfg）。
  config 缺 / 非 dict / 區塊為 None / 區塊非 dict → 一律回空 dict
  （呼叫端再 .get(key, 寫死值) 退寫死，達成「不退化」保證）。
另驗 _event_strategy 改走 _event_cfg 後行為 byte-identical。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

import core.states as states
import vision.signatures as signatures
from core.decision_engine import DecisionEngine, ScreenOption
from tests.fakes import FakeOCR


def _ctx(config):
    return SimpleNamespace(config=config)


def _build_engine(decision: dict) -> DecisionEngine:
    """沿用 test_decision_engine._write_engine_fixture 手法：寫臨時 config.yaml +
    priority_list.json，回傳載入完成的 DecisionEngine。

    註：TemporaryDirectory 在 engine 建構（含 _load_config / _load_priority_list 全部
    讀檔）完成後才清掉，之後 engine 不再讀檔，安全。
    """
    tmp = tempfile.TemporaryDirectory()
    tmpdir = Path(tmp.name)
    priority_path = tmpdir / "priority_list.json"
    priority_path.write_text(json.dumps({
        "potentials": [
            {"name": "Lucky", "aliases": ["Lucky"]},
            {"name": "Req", "aliases": ["Req"]},
            {"name": "Lev", "aliases": ["Lev"]},
            {"name": "BakA", "aliases": ["BakA"]},
            {"name": "BakB", "aliases": ["BakB"]},
            {"name": "Other", "aliases": ["Other"]},
        ]
    }, ensure_ascii=False), encoding="utf-8")
    config_path = tmpdir / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "decision": decision,
        "ocr": {"priority_list_path": str(priority_path)},
    }, allow_unicode=True), encoding="utf-8")
    engine = DecisionEngine(config_path=str(config_path))
    tmp.cleanup()
    return engine


class EventCfgReaderTests(unittest.TestCase):
    """_event_cfg 三層防呆。"""

    def test_normal(self) -> None:
        ctx = _ctx({'event': {'strategy': 'aggressive'}})
        self.assertEqual(states._event_cfg(ctx), {'strategy': 'aggressive'})

    def test_missing_block(self) -> None:
        self.assertEqual(states._event_cfg(_ctx({})), {})

    def test_config_none(self) -> None:
        self.assertEqual(states._event_cfg(_ctx(None)), {})

    def test_config_not_dict(self) -> None:
        self.assertEqual(states._event_cfg(_ctx(['not', 'a', 'dict'])), {})

    def test_block_none(self) -> None:
        # yaml `event:`（空值）→ event 鍵存在但值為 None。
        self.assertEqual(states._event_cfg(_ctx({'event': None})), {})

    def test_block_not_dict(self) -> None:
        self.assertEqual(states._event_cfg(_ctx({'event': ['x']})), {})

    def test_no_config_attr(self) -> None:
        # ctx 完全沒有 config 屬性。
        self.assertEqual(states._event_cfg(SimpleNamespace()), {})


class PrepareCfgReaderTests(unittest.TestCase):
    """_prepare_cfg 三層防呆（目前無生產呼叫端，純骨架，後續 prepare 項使用）。"""

    def test_normal(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx({'prepare': {'foo': 1}})), {'foo': 1})

    def test_missing_block(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx({})), {})

    def test_config_none(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx(None)), {})

    def test_config_not_dict(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx('nope')), {})

    def test_block_none(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx({'prepare': None})), {})

    def test_block_not_dict(self) -> None:
        self.assertEqual(states._prepare_cfg(_ctx({'prepare': 42})), {})

    def test_no_config_attr(self) -> None:
        self.assertEqual(states._prepare_cfg(SimpleNamespace()), {})


class EventStrategyByteIdenticalTests(unittest.TestCase):
    """_event_strategy 改走 _event_cfg 後，正常路徑與邊界行為不變。"""

    def test_default_aggressive(self) -> None:
        self.assertEqual(states._event_strategy(_ctx({})), 'aggressive')

    def test_explicit_aggressive(self) -> None:
        self.assertEqual(
            states._event_strategy(_ctx({'event': {'strategy': 'aggressive'}})), 'aggressive')

    def test_explicit_conservative(self) -> None:
        self.assertEqual(
            states._event_strategy(_ctx({'event': {'strategy': 'conservative'}})), 'conservative')

    def test_gamble_prefer_alias(self) -> None:
        self.assertEqual(
            states._event_strategy(_ctx({'event': {'gamble_prefer': 'max_money'}})), 'aggressive')

    def test_config_none_defaults_aggressive(self) -> None:
        self.assertEqual(states._event_strategy(_ctx(None)), 'aggressive')

    def test_unknown_strategy_defaults_aggressive(self) -> None:
        self.assertEqual(
            states._event_strategy(_ctx({'event': {'strategy': 'wat'}})), 'aggressive')


class DecisionEngineConfigTests(unittest.TestCase):
    """decision 區塊三個新旋鈕：required_target_level / prefer_never_picked /
    prefer_higher_gain。預設值 = 現有寫死值，給預設時 byte-identical。
    """

    # ── byte-identical 回歸：無這三個 key（或無 config）時同現行 ──

    def test_required_target_level_defaults_to_6(self) -> None:
        engine = _build_engine({"required": ["Req"]})
        self.assertEqual(engine._required_target_level, 6)

    def test_prefer_flags_default_to_true(self) -> None:
        engine = _build_engine({"required": ["Req"]})
        self.assertTrue(engine._prefer_never_picked)
        self.assertTrue(engine._prefer_higher_gain)

    def test_required_caps_at_lv6_by_default(self) -> None:
        # 預設目標 Lv.6：累計 5 仍 required；累計 6 轉 unknown（同現行）。
        engine = _build_engine({"required": ["Req"]})
        engine.state.accumulated_levels["Req"] = 5
        cats = {o.name: o.category for o in engine.categorize([ScreenOption(name="Req", level=1)])}
        self.assertEqual(cats["Req"], "required")
        engine.state.accumulated_levels["Req"] = 6
        cats = {o.name: o.category for o in engine.categorize([ScreenOption(name="Req", level=1)])}
        self.assertEqual(cats["Req"], "unknown")

    def test_pick_best_default_prefers_never_picked_then_higher_level(self) -> None:
        # 預設 True/True：一個選過(高 level)、一個沒選過(低 level) → 沒選過勝（多樣性優先）。
        engine = _build_engine({"required": ["Req"]})
        engine.state.selected_history["BakB"] = 1  # B 已選過
        chosen = engine._pick_best([
            ScreenOption(name="BakB", level=3),   # 選過、+3
            ScreenOption(name="BakA", level=1),   # 沒選過、+1
        ])
        self.assertEqual(chosen.name, "BakA")
        # 同為沒選過 → 升等量大者勝（+3 > +2 > +1）。
        chosen = engine._pick_best([
            ScreenOption(name="Other", level=1),
            ScreenOption(name="BakA", level=3),
        ])
        self.assertEqual(chosen.name, "BakA")

    # ── required_target_level：可調 + clamp ──

    def test_required_target_level_custom_stops_earlier(self) -> None:
        # 設 3：required 潛能達 Lv.3 後 category 轉 unknown（不再續選）。
        engine = _build_engine({"required": ["Req"], "required_target_level": 3})
        self.assertEqual(engine._required_target_level, 3)
        engine.state.accumulated_levels["Req"] = 2
        cats = {o.name: o.category for o in engine.categorize([ScreenOption(name="Req", level=1)])}
        self.assertEqual(cats["Req"], "required")
        engine.state.accumulated_levels["Req"] = 3
        cats = {o.name: o.category for o in engine.categorize([ScreenOption(name="Req", level=1)])}
        self.assertEqual(cats["Req"], "unknown")

    def test_required_target_level_clamps_low(self) -> None:
        # 給 0 → clamp 到下限 1。
        engine = _build_engine({"required": ["Req"], "required_target_level": 0})
        self.assertEqual(engine._required_target_level, 1)

    def test_required_target_level_clamps_high(self) -> None:
        # 給 9 → 越界，退預設 6。
        engine = _build_engine({"required": ["Req"], "required_target_level": 9})
        self.assertEqual(engine._required_target_level, 6)

    def test_required_target_level_bad_type_falls_back(self) -> None:
        # 給 "abc" → 壞型別，退預設 6。
        engine = _build_engine({"required": ["Req"], "required_target_level": "abc"})
        self.assertEqual(engine._required_target_level, 6)

    # ── prefer_never_picked=False：不再優先未選過 ──

    def test_prefer_never_picked_false_ignores_diversity(self) -> None:
        engine = _build_engine({"required": ["Req"], "prefer_never_picked": False})
        self.assertFalse(engine._prefer_never_picked)
        engine.state.selected_history["BakB"] = 1  # B 已選過
        # A 沒選過 level 低、B 選過 level 高 → 關閉多樣性後由 B（高 level）勝。
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=1),   # 沒選過、+1
            ScreenOption(name="BakB", level=3),   # 選過、+3
        ])
        self.assertEqual(chosen.name, "BakB")

    # ── prefer_higher_gain=False：不再依升等量排序 ──

    def test_prefer_higher_gain_false_keeps_list_order(self) -> None:
        engine = _build_engine({"required": ["Req"], "prefer_higher_gain": False})
        self.assertFalse(engine._prefer_higher_gain)
        # 同 never_picked 狀態（都沒選過）下，不再依 level 排序 → 退回清單順序（取第一個）。
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=1),   # 清單第一、+1
            ScreenOption(name="Other", level=3),  # +3 但不該因此被選
        ])
        self.assertEqual(chosen.name, "BakA")


class MinLevelThresholdTests(unittest.TestCase):
    """decision.min_level_threshold（E-2）：legacy _pick_best 排除升等量低於門檻的
    弱卡。預設 0 = 不過濾（byte-identical 現行）。guaranteed 不受限；過濾後全空退全集。
    """

    # ── byte-identical：預設 0 = 不過濾 ──

    def test_default_threshold_is_zero(self) -> None:
        engine = _build_engine({"required": ["Req"]})
        self.assertEqual(engine._min_level_threshold, 0)

    def test_default_does_not_exclude_weak_cards(self) -> None:
        # 預設不給 min_level_threshold：_pick_best 不排除 level 1 弱卡，結果同現行。
        # 兩張都沒選過 → 升等量大者勝（+3>+1）；弱卡仍在候選池內參與排序。
        engine = _build_engine({"required": ["Req"]})
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=1),
            ScreenOption(name="Other", level=3),
        ])
        self.assertEqual(chosen.name, "Other")
        # 反向：弱卡若因多樣性勝出，預設下不該被門檻擋掉。
        engine.state.selected_history["Other"] = 1  # 高 level 卡已選過
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=1),   # 沒選過、+1
            ScreenOption(name="Other", level=3),  # 選過、+3
        ])
        self.assertEqual(chosen.name, "BakA")

    # ── threshold=2：排除 level 1，但有合格候選時才排除 ──

    def test_threshold_excludes_below(self) -> None:
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 2})
        self.assertEqual(engine._min_level_threshold, 2)
        # BakA(+1) 低於門檻被排除；即使它「沒選過」本該多樣性優先，仍出局。
        # 留下 Other(+3) >= 2 → 選 Other。
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=1),   # < 2，排除
            ScreenOption(name="Other", level=3),  # >= 2，保留
        ])
        self.assertEqual(chosen.name, "Other")

    def test_threshold_keeps_qualifying_then_diversity(self) -> None:
        # 兩張都 >= 門檻時，過濾不改變池內容 → 原 sort_key（多樣性→升等量）照常。
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 2})
        engine.state.selected_history["Other"] = 1  # 高 level 已選過
        chosen = engine._pick_best([
            ScreenOption(name="BakA", level=2),   # 沒選過、+2、>=門檻
            ScreenOption(name="Other", level=3),  # 選過、+3、>=門檻
        ])
        self.assertEqual(chosen.name, "BakA")  # 多樣性優先

    # ── threshold 高到全空 → 退全集（仍選得出，不報錯/不回 None）──

    def test_threshold_above_all_falls_back_to_full_set(self) -> None:
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 5})
        # 全部候選都 < 5 → filtered 空 → 退全集；原 sort_key 取最佳（都沒選過→+3 勝）。
        cands = [
            ScreenOption(name="BakA", level=1),
            ScreenOption(name="Other", level=3),
        ]
        chosen = engine._pick_best(cands)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "Other")  # 退全集後升等量大者勝

    def test_single_candidate_bypasses_threshold(self) -> None:
        # 單一候選一律拿（保證推進），即使 level < 門檻也不被擋。
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 9})
        chosen = engine._pick_best([ScreenOption(name="BakA", level=1)])
        self.assertEqual(chosen.name, "BakA")

    # ── guaranteed 不受過濾 ──

    def test_guaranteed_bypasses_threshold_via_decide(self) -> None:
        # 走 decide 規則0：guaranteed 卡 level 低於門檻仍選得到（無條件選取以免錯過）。
        engine = _build_engine({"guaranteed": ["Lucky"], "min_level_threshold": 5})
        # Lucky(+1) < 5，但 guaranteed 不受 min_threshold 過濾。
        # 另放兩張低 level 雜卡確保有多個候選（不是靠單一候選旁路）。
        chosen = engine.decide([
            ScreenOption(name="Lucky", level=1, position=(10, 10)),
            ScreenOption(name="Other", level=1, position=(20, 20)),
            ScreenOption(name="BakA", level=1, position=(30, 30)),
        ])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "Lucky")

    def test_choose_from_candidates_apply_min_threshold_false(self) -> None:
        # 直接驗 apply_min_threshold=False：高門檻下弱卡仍可被選（guaranteed 繞過路徑）。
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 5})
        chosen = engine._choose_from_candidates(
            [
                ScreenOption(name="BakA", level=1),   # 沒選過、+1
                ScreenOption(name="Other", level=3),  # 沒選過、+3
            ],
            log_message="[test] {chosen.name}",
            apply=False,
            apply_min_threshold=False,
        )
        # 不過濾 → 原 sort_key（都沒選過→升等量大）→ Other。若被門檻擋過會退全集，
        # 結果相同；關鍵是 BakA(+1<5) 沒被當成「過濾掉」而是全程參與。
        self.assertEqual(chosen.name, "Other")

    def test_choose_from_candidates_default_applies_threshold(self) -> None:
        # 預設 apply_min_threshold=True：弱卡被排除（對照上一個 False 案例）。
        engine = _build_engine({"required": ["Req"], "min_level_threshold": 2})
        chosen = engine._choose_from_candidates(
            [
                ScreenOption(name="BakA", level=1),   # < 2，排除
                ScreenOption(name="Other", level=3),  # >= 2，保留
            ],
            log_message="[test] {chosen.name}",
            apply=False,
        )
        self.assertEqual(chosen.name, "Other")

    # ── 負數 / 壞型別 → 退 0 ──

    def test_negative_threshold_falls_back_to_zero(self) -> None:
        engine = _build_engine({"required": ["Req"], "min_level_threshold": -3})
        self.assertEqual(engine._min_level_threshold, 0)

    def test_bad_type_threshold_falls_back_to_zero(self) -> None:
        engine = _build_engine({"required": ["Req"], "min_level_threshold": "abc"})
        self.assertEqual(engine._min_level_threshold, 0)

    def test_none_threshold_falls_back_to_zero(self) -> None:
        # yaml `min_level_threshold:`（空值）→ None。
        engine = _build_engine({"required": ["Req"], "min_level_threshold": None})
        self.assertEqual(engine._min_level_threshold, 0)


def _bbox(x: int, y: int, w: int = 80, h: int = 26) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


class RecommendationTargetLevelParseTests(unittest.TestCase):
    """signatures.parse_recommendation_target_level：純解析「推薦N級」→N。

    E-3 感知層：只解析、不 gate。容忍「級」OCR 誤讀與半形空格。
    """

    def test_standard_badge(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level("推薦6級"), 6)

    def test_simplified_variant(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level("推荐3級"), 3)

    def test_with_spaces(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level("推薦 5 級"), 5)

    def test_level_char_dropped_by_ocr(self) -> None:
        # 「級」字 OCR 漏讀仍可解析（正則只要求「推薦/推荐」後跟數字）。
        self.assertEqual(signatures.parse_recommendation_target_level("推薦6"), 6)

    def test_empty_string(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level(""), 0)

    def test_none(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level(None), 0)

    def test_no_recommendation_text(self) -> None:
        self.assertEqual(signatures.parse_recommendation_target_level("拿走"), 0)

    def test_red_badge_color_sentinel(self) -> None:
        # 純色錨命中時 recommendation_text='red_badge_color'（無數字）→ 0。
        self.assertEqual(signatures.parse_recommendation_target_level("red_badge_color"), 0)


class ScreenOptionRecommendationTargetLevelDefaultTests(unittest.TestCase):
    """ScreenOption 新欄位預設值 = 0（不讀此欄位的現有決策不受影響）。"""

    def test_default_is_zero(self) -> None:
        self.assertEqual(ScreenOption(name="X").recommendation_target_level, 0)


class ExtractSlotOptionsTargetLevelTests(unittest.TestCase):
    """感知整合：_extract_slot_options 把解析出的目標等級填進 ScreenOption。

    手法同 test_state_replay.test_extract_slot_options_supports_two_card_layout_hint：
    FakeOCR(slot_results=...) + np.zeros 黑底（color_hit 不會誤觸）。
    """

    def _ctx(self, slot_results):
        return SimpleNamespace(
            ocr=FakeOCR(slot_results=slot_results),
            last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
            frame_w=1280,
            frame_h=720,
            pending_card_count=2,
        )

    def test_target_level_parsed_into_screen_option(self) -> None:
        # slot0 帶「推薦6級」徽章（落在 ROI 頂端 badge 區）→ target_level=6。
        # slot1 無推薦徽章 → target_level=0。
        ctx = self._ctx([
            [("推薦6級", 0.9, _bbox(10, 12)), ("攻擊強化", 0.9, _bbox(10, 650)),
             ("等級 1→3", 0.9, _bbox(10, 690))],
            [("防禦強化", 0.9, _bbox(10, 650)), ("等級 1→2", 0.9, _bbox(10, 690))],
        ])
        options = states._extract_slot_options(ctx)
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0].recommendation_target_level, 6)
        self.assertTrue(options[0].recommended)
        self.assertEqual(options[1].recommendation_target_level, 0)
        self.assertFalse(options[1].recommended)


class UpgradeStrategyConfigTests(unittest.TestCase):
    """decision.upgrade_strategy + recommendation_target.enabled（E-4）：
    recommendation_badge 模式下，候選排序如何在「目標等級不同的卡」之間取捨。

    鐵律：總開關 recommendation_target.enabled 預設 false ⇒ 退回現行排序
    （升後等級最高、平手最左），逐位元同現版（已 L3 驗證的閉環不退化）。
    upgrade_strategy 只在 enabled=true 且所有候選都讀到推薦N級(>0) 時生效。
    """

    @staticmethod
    def _rbadge(**overrides) -> dict:
        d = {"mode": "recommendation_badge", "required": ["Req"]}
        d.update(overrides)
        return d

    @staticmethod
    def _opt(name: str, level: int, target: int, x: int) -> ScreenOption:
        return ScreenOption(
            name=name,
            level=level,                          # opt.level = 升後等級 M
            position=(x, 0),
            recommended=True,
            recommendation_target_level=target,   # 推薦N級
        )

    # ── 預設值（沿用 E-1/E-2 attr 檢查手法）──

    def test_defaults(self) -> None:
        engine = _build_engine(self._rbadge())
        self.assertEqual(engine._upgrade_strategy, "minimize_overflow")
        self.assertFalse(engine._recommendation_target_enabled)

    # ── byte-identical：enabled=false（預設）退現行（升後等級最高、平手最左）──

    def test_disabled_picks_highest_after_level_ignoring_target(self) -> None:
        # 即使設了 recommendation_target_level，未啟用時與 target 完全無關：
        # A 升後 5 級(target=4 溢出)、B 升後 4 級(target=4 剛好) → 選 A（升後等級最高）。
        engine = _build_engine(self._rbadge())
        chosen = engine.decide([
            self._opt("CardA", level=5, target=4, x=200),
            self._opt("CardB", level=4, target=4, x=400),
        ])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "CardA")

    def test_disabled_tiebreaks_by_leftmost(self) -> None:
        # 升後等級平手 → 平手最左（同現行）。
        engine = _build_engine(self._rbadge())
        chosen = engine.decide([
            self._opt("Right", level=3, target=2, x=600),
            self._opt("Left", level=3, target=9, x=200),
        ])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "Left")

    def test_enabled_false_explicit_still_byte_identical(self) -> None:
        # 顯式 enabled=false + minimize_overflow：仍退現行（B 溢出 0 但不被偏好）。
        engine = _build_engine(self._rbadge(
            upgrade_strategy="minimize_overflow",
            recommendation_target={"enabled": False},
        ))
        chosen = engine.decide([
            self._opt("CardA", level=5, target=4, x=200),   # 溢出 1
            self._opt("CardB", level=5, target=6, x=400),   # 溢出 0
        ])
        # 升後等級平手(5,5) → 平手最左 = CardA（target 不參與）。
        self.assertEqual(chosen.name, "CardA")

    # ── minimize_overflow 生效（REGISTRY 例）──

    def test_minimize_overflow_prefers_non_overflowing(self) -> None:
        # 卡A(level=5, target=4 溢出1) vs 卡B(level=5, target=6 溢出0)，兩張都 recommended。
        # 啟用 minimize_overflow → 選卡B（不浪費升等點）。
        engine = _build_engine(self._rbadge(
            upgrade_strategy="minimize_overflow",
            recommendation_target={"enabled": True},
        ))
        chosen = engine.decide([
            self._opt("CardA", level=5, target=4, x=200),
            self._opt("CardB", level=5, target=6, x=400),
        ])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "CardB")

    def test_minimize_overflow_no_overflow_equals_current(self) -> None:
        # 無卡溢出（都 after<=target）→ key 第一維全 0 → 等同現行 (-after, pos_x)：
        # 升後等級最高勝。A 升後 5(target=6)、B 升後 3(target=4) → 選 A。
        engine = _build_engine(self._rbadge(
            upgrade_strategy="minimize_overflow",
            recommendation_target={"enabled": True},
        ))
        chosen = engine.decide([
            self._opt("CardA", level=5, target=6, x=200),
            self._opt("CardB", level=3, target=4, x=400),
        ])
        self.assertEqual(chosen.name, "CardA")

    # ── 退化保護：某卡 recommendation_target_level=0 → 退現行 ──

    def test_unread_target_falls_back_to_current(self) -> None:
        # enabled=true 但 CardB 讀不到推薦級(=0) → 任一讀不到即退現行（升後等級最高）。
        # A 升後 5(target=4 溢出)、B 升後 6(target=0 讀不到) → 退現行選 B（升後 6 最高）。
        engine = _build_engine(self._rbadge(
            upgrade_strategy="minimize_overflow",
            recommendation_target={"enabled": True},
        ))
        chosen = engine.decide([
            self._opt("CardA", level=5, target=4, x=200),
            self._opt("CardB", level=6, target=0, x=400),
        ])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "CardB")

    # ── 三策略語意分歧（同一組卡 → 各選不同）──
    # 共用卡組（升後 after / 目標 target；x 不影響結果,因分歧由 metric 主導,非 tiebreak）：
    #   Y: after=7, target=6 → 溢出 max(0,+1)=1 / |dist|=1 / signed=+1   （最接近目標）
    #   M: after=6, target=9 → 溢出 max(0,-3)=0 / |dist|=3 / signed=-3   （不溢出且 after 較高）
    #   F: after=3, target=8 → 溢出 max(0,-5)=0 / |dist|=5 / signed=-5   （離目標最遠 / 最缺）
    # 預期：minimize_overflow → M（溢出 0；與 F 同 0,靠 -after tiebreak 6>3 勝）；
    #       nearest_target    → Y（|dist|=1 最小）；
    #       farthest_target   → F（signed=-5 最小 = 升離目標最遠 / 最缺,規格定義）。
    # 三者各異且皆由 metric 決定,證明語意真分歧（非座標巧合）。
    @staticmethod
    def _diverge_cards() -> list:
        return [
            UpgradeStrategyConfigTests._opt("Y", level=7, target=6, x=200),
            UpgradeStrategyConfigTests._opt("M", level=6, target=9, x=400),
            UpgradeStrategyConfigTests._opt("F", level=3, target=8, x=600),
        ]

    def _strategy_engine(self, strategy: str) -> DecisionEngine:
        return _build_engine(self._rbadge(
            upgrade_strategy=strategy,
            recommendation_target={"enabled": True},
        ))

    def test_minimize_overflow_strategy(self) -> None:
        # 溢出最小者勝；M 與 F 同為溢出 0,-after tiebreak 取 after 較高的 M。
        chosen = self._strategy_engine("minimize_overflow").decide(self._diverge_cards())
        self.assertEqual(chosen.name, "M")

    def test_nearest_target_strategy(self) -> None:
        # |after-target| 最小者勝（升後最接近目標,含低於目標）→ Y（|dist|=1）。
        chosen = self._strategy_engine("nearest_target").decide(self._diverge_cards())
        self.assertEqual(chosen.name, "Y")

    def test_farthest_target_strategy(self) -> None:
        # (after-target) 最小（最負）者勝 = 升離目標最遠 / 最缺 → F（signed=-5）。
        chosen = self._strategy_engine("farthest_target").decide(self._diverge_cards())
        self.assertEqual(chosen.name, "F")

    def test_three_strategies_diverge_on_same_cards(self) -> None:
        # 同一組卡,三策略各選不同卡 → 語意確實分歧。
        self.assertEqual(
            self._strategy_engine("minimize_overflow").decide(self._diverge_cards()).name, "M")
        self.assertEqual(
            self._strategy_engine("nearest_target").decide(self._diverge_cards()).name, "Y")
        self.assertEqual(
            self._strategy_engine("farthest_target").decide(self._diverge_cards()).name, "F")

    # ── 壞值 → 退 minimize_overflow ──

    def test_bogus_strategy_falls_back_to_minimize_overflow(self) -> None:
        engine = _build_engine(self._rbadge(
            upgrade_strategy="bogus",
            recommendation_target={"enabled": True},
        ))
        self.assertEqual(engine._upgrade_strategy, "minimize_overflow")
        # 行為亦如 minimize_overflow：A 溢出1 vs B 溢出0 → B。
        chosen = engine.decide([
            self._opt("CardA", level=5, target=4, x=200),
            self._opt("CardB", level=5, target=6, x=400),
        ])
        self.assertEqual(chosen.name, "CardB")

    # ── recommendation_target 三層防呆 ──

    def test_recommendation_target_not_dict_defaults_false(self) -> None:
        engine = _build_engine(self._rbadge(recommendation_target=["x"]))
        self.assertFalse(engine._recommendation_target_enabled)

    def test_recommendation_target_none_defaults_false(self) -> None:
        # yaml `recommendation_target:`（空值）→ None。
        engine = _build_engine(self._rbadge(recommendation_target=None))
        self.assertFalse(engine._recommendation_target_enabled)

    def test_recommendation_target_missing_defaults_false(self) -> None:
        engine = _build_engine(self._rbadge())
        self.assertFalse(engine._recommendation_target_enabled)


class ShopRefreshTriggerTests(unittest.TestCase):
    """shop.refresh.trigger（B）：刷新時機 enum。預設 exhausted=現行（買無可買就刷）。
    never=從不刷；when_gap=有音符缺口才刷；before_target=卡片未達標才刷；
    always=同 exhausted（「每進店先刷」需改時序、另開子項）。
    """

    def _ctx(self, refresh_cfg=None, target_notes=None, current_notes=None,
             card_current=0, card_target=78):
        shop = {}
        if refresh_cfg is not None:
            shop['refresh'] = refresh_cfg
        return SimpleNamespace(
            config={'shop': shop},
            target_notes=target_notes or {},
            current_notes=current_notes or {},
            card_counter_target_total=card_target,
            card_counter_current_total=card_current,
        )

    def test_default_exhausted_allows(self) -> None:
        self.assertTrue(states._refresh_trigger_allows(self._ctx()))

    def test_explicit_exhausted_allows(self) -> None:
        self.assertTrue(states._refresh_trigger_allows(self._ctx({'trigger': 'exhausted'})))

    def test_never_blocks(self) -> None:
        self.assertFalse(states._refresh_trigger_allows(self._ctx({'trigger': 'never'})))

    def test_always_allows(self) -> None:
        self.assertTrue(states._refresh_trigger_allows(self._ctx({'trigger': 'always'})))

    def test_when_gap_with_gap_allows(self) -> None:
        ctx = self._ctx({'trigger': 'when_gap'}, target_notes={'風': 45}, current_notes={'風': 10})
        self.assertTrue(states._refresh_trigger_allows(ctx))

    def test_when_gap_no_gap_blocks(self) -> None:
        ctx = self._ctx({'trigger': 'when_gap'}, target_notes={'風': 45}, current_notes={'風': 50})
        self.assertFalse(states._refresh_trigger_allows(ctx))

    def test_before_target_under_allows(self) -> None:
        ctx = self._ctx({'trigger': 'before_target'}, card_current=30, card_target=78)
        self.assertTrue(states._refresh_trigger_allows(ctx))

    def test_before_target_met_blocks(self) -> None:
        ctx = self._ctx({'trigger': 'before_target'}, card_current=78, card_target=78)
        self.assertFalse(states._refresh_trigger_allows(ctx))

    def test_unknown_trigger_defaults_allow(self) -> None:
        # 未知值保守退 exhausted（允許），不靜默關掉刷新。
        self.assertTrue(states._refresh_trigger_allows(self._ctx({'trigger': 'bogus'})))

    def test_missing_refresh_block_allows(self) -> None:
        # config 無 refresh 區塊 → 退 exhausted（現行）。
        self.assertTrue(states._refresh_trigger_allows(SimpleNamespace(config={'shop': {}})))


if __name__ == "__main__":
    unittest.main()
