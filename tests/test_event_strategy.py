"""事件策略 config（保守 vs 激進，預設激進）+ 升級機 NPC + quiz 數學題庫
   覆蓋測試（使用者 2026-06-14 拍板）。

語意（GAME_MECHANICS E1）：
  - aggressive（激進，預設）= 追最高報酬、接受風險：在所有「非消耗音符」選項中挑
    報酬最好的（接受消耗金錢、接受機率損失）。報酬高低：稀有潛能 > 潛能 >
    普通潛能 > 音符 > 金錢。機率純金錢賭注沿用 _event_gamble_gain（選錢多）。
  - conservative（保守）= 絕不冒損失：排除「機率損失/失去金錢」「機率損失/失去
    生命」「消耗金錢」「消耗音符」的選項，在剩下（無下行、保證/免費）選項中挑
    報酬最好的。
  - quiz 命中 與 upgrade-event-rare（稀有潛能）規則仍最優先，不受 strategy 影響。

覆蓋場景：
  (a) 升級機：aggressive→積極出手 / conservative→還是算了。
  (b) quiz「二的十次方」：64 / 1,024 / 65,536 → 選 1,024（不分 strategy）。
  (d) 傾聽事件：aggressive→認真傾聽（保證 5 音符）/ conservative→隨意傾聽（免費無下行）。
  + 回歸：激進預設下賭博「選錢多」、命運之鏡式仍走 E1。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import core.states as states
from tests.fakes import FakeInput, FakeOCR


def _bbox(x: int, y: int, w: int = 40, h: int = 20) -> tuple:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _make_ctx(global_results, strategy: str | None = None, extra_event: dict | None = None):
    event_cfg: dict = {}
    if strategy is not None:
        event_cfg['strategy'] = strategy
    if extra_event:
        event_cfg.update(extra_event)
    return SimpleNamespace(
        ocr=FakeOCR(global_results=global_results),
        last_frame=np.zeros((720, 1280, 3), dtype=np.uint8),
        input=FakeInput(),
        frame_w=1280,
        frame_h=720,
        config={'event': event_cfg} if event_cfg else {},
    )


class UpgradeMachineEventTests(unittest.TestCase):
    """升級機 NPC：想用你的運氣獲得一些好處嗎? 三選一。"""

    def _ocr(self):
        # 選項標題列在左（cx≈760，落在 choice ROI x>=640）、獎勵明細在右。
        return [
            ("謹慎出手吧。", 0.98, _bbox(756, 300, 110, 26)),
            ("消耗100", 0.90, _bbox(1000, 300, 60, 20)),
            ("隨機獲得1個普通潛能", 0.62, _bbox(1075, 300, 180, 20)),
            ("積極出手吧!", 0.98, _bbox(756, 410, 110, 26)),
            ("消耗120", 0.90, _bbox(1000, 410, 60, 20)),
            ("隨機獲得1個潛能", 0.60, _bbox(1075, 410, 150, 20)),
            ("還是算了。", 0.98, _bbox(756, 520, 100, 26)),
            ("獲得30", 0.95, _bbox(1166, 520, 58, 20)),
        ]

    def test_aggressive_picks_active_for_potential(self) -> None:
        # 激進：積極出手（消耗120 → 潛能，報酬最好），不是普通潛能/金錢。
        ctx = _make_ctx(self._ocr(), strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(400 <= cy <= 430, f"激進應點『積極出手』(y≈410)，實點 y={cy}")

    def test_default_is_aggressive(self) -> None:
        # 無 strategy → 預設激進 → 積極出手。
        ctx = _make_ctx(self._ocr())
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(400 <= cy <= 430, f"預設(激進)應點『積極出手』(y≈410)，實點 y={cy}")

    def test_conservative_picks_no_cost_option(self) -> None:
        # 保守：排除兩個「消耗金錢」選項 → 只剩「還是算了」(獲得30，無下行)。
        ctx = _make_ctx(self._ocr(), strategy='conservative')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(510 <= cy <= 540, f"保守應點『還是算了』(y≈520)，實點 y={cy}")

    def test_classifies_as_event(self) -> None:
        from vision.signatures import classify
        items = [
            ("想用你的運氣獲得一些好處嗎?", 0.95, _bbox(700, 140, 360, 30)),
            ("積極出手吧!", 0.98, _bbox(756, 410, 110, 26)),
        ]
        state, _score, _sig = classify(items, frame=np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertEqual(state, "STATE_EVENT", "升級機事件應判 STATE_EVENT")


class QuizMathEventTests(unittest.TestCase):
    """quiz 數學題庫：二的十次方 → 1,024。"""

    def _ocr(self):
        # 問句落在 question ROI（cx≈900 in [691..1126], cy≈180 in [129..230]）。
        # 選項落在 choice ROI。
        return [
            ("二的十次方是多少?", 0.95, _bbox(820, 175, 200, 30)),
            ("64", 0.98, _bbox(900, 300, 60, 26)),
            ("1,024", 0.97, _bbox(900, 410, 80, 26)),
            ("65,536", 0.96, _bbox(900, 520, 100, 26)),
        ]

    def test_aggressive_quiz_picks_1024(self) -> None:
        ctx = _make_ctx(self._ocr(), strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(400 <= cy <= 430, f"quiz 應選 1,024 (y≈410)，實點 y={cy}")

    def test_conservative_quiz_picks_1024(self) -> None:
        # quiz 不分 strategy，保守也要選對答案。
        ctx = _make_ctx(self._ocr(), strategy='conservative')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(400 <= cy <= 430, f"quiz(保守) 應選 1,024 (y≈410)，實點 y={cy}")


class ListenEventTests(unittest.TestCase):
    """傾聽事件：聽完這段音樂...你可能會有意外的收穫。"""

    def _ocr(self):
        return [
            ("認真傾聽。", 0.98, _bbox(756, 320, 100, 26)),
            ("消耗50", 0.90, _bbox(1000, 320, 60, 20)),
            ("獲得5個隨機音符", 0.62, _bbox(1075, 320, 160, 20)),
            ("隨意傾聽。", 0.98, _bbox(756, 470, 100, 26)),
            ("50%機率恢復30%生命值", 0.60, _bbox(940, 470, 200, 20)),
            ("50%機率獲得5個隨機音符", 0.60, _bbox(940, 510, 220, 20)),
        ]

    def test_aggressive_picks_serious_listen(self) -> None:
        # 激進：認真傾聽（保證 5 音符，接受消耗 50 金錢）。
        # 注意：報酬是音符，但此選項**消耗的是金錢**而非音符 → 不被「拒消耗音符」排除。
        ctx = _make_ctx(self._ocr(), strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(310 <= cy <= 340, f"激進應點『認真傾聽』(y≈320)，實點 y={cy}")

    def test_conservative_picks_casual_listen(self) -> None:
        # 保守：認真傾聽含「消耗金錢」→ 排除；隨意傾聽免費無損失下行 → 選它。
        ctx = _make_ctx(self._ocr(), strategy='conservative')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(460 <= cy <= 490, f"保守應點『隨意傾聽』(y≈470)，實點 y={cy}")


class StrategyRegressionTests(unittest.TestCase):
    """回歸：激進預設下既有事件行為相容。"""

    def test_aggressive_gamble_still_picks_most_money(self) -> None:
        # 賭博三選一 → 激進仍選錢多的「相信命運」(650)。
        ctx = _make_ctx([
            ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
            ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
            ("50%機率失去100", 0.96, _bbox(1094, 330, 130, 24)),
            ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
            ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
            ("70%機率失去200", 0.76, _bbox(1092, 440, 132, 24)),
            ("相信現實", 0.82, _bbox(756, 506, 82, 26)),
            ("獲得30", 0.98, _bbox(1166, 550, 58, 24)),
        ], strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(390 <= cy <= 430, f"激進賭博應選『相信命運』(650)，實點 y={cy}")

    def test_conservative_gamble_avoids_loss(self) -> None:
        # 保守：兩個賭注皆含「機率失去金錢」→ 排除；只剩保底「相信現實」(獲得30)。
        ctx = _make_ctx([
            ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
            ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
            ("50%機率失去100", 0.96, _bbox(1094, 330, 130, 24)),
            ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
            ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
            ("70%機率失去200", 0.76, _bbox(1092, 440, 132, 24)),
            ("相信現實", 0.82, _bbox(756, 506, 82, 26)),
            ("獲得30", 0.98, _bbox(1166, 550, 58, 24)),
        ], strategy='conservative')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(500 <= cy <= 540, f"保守賭博應選保底『相信現實』(獲得30)，實點 y={cy}")

    def test_gamble_prefer_alias_back_compat(self) -> None:
        # 舊 key event.gamble_prefer=max_money 仍應觸發激進「選錢多」行為。
        ctx = _make_ctx([
            ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
            ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
            ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
            ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
        ], strategy=None, extra_event={'gamble_prefer': 'max_money'})
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(390 <= cy <= 430, f"舊 gamble_prefer alias 應選『相信命運』(650)，實點 y={cy}")


class EventRewardRankTests(unittest.TestCase):
    """報酬分級：稀有潛能 > 潛能 > 普通潛能 > 音符 > 金錢。"""

    def test_finer_reward_rank_ordering(self) -> None:
        rank = states._event_reward_rank
        self.assertLess(rank("隨機獲得1個稀有潛能"), rank("隨機獲得1個潛能"))
        self.assertLess(rank("隨機獲得1個潛能"), rank("隨機獲得1個普通潛能"))
        self.assertLess(rank("隨機獲得1個普通潛能"), rank("獲得5個隨機音符"))
        self.assertLess(rank("獲得5個隨機音符"), rank("獲得30金幣"))

    def test_money_loss_and_hp_loss_detectors(self) -> None:
        self.assertTrue(states._event_has_money_loss("50%機率失去100金幣"))
        self.assertTrue(states._event_has_money_loss("70%機率損失200💰"))
        self.assertFalse(states._event_has_money_loss("50%機率獲得200金幣"))
        self.assertTrue(states._event_has_hp_loss("33%機率損失30%生命值"))
        self.assertTrue(states._event_has_hp_loss("失去生命"))
        self.assertFalse(states._event_has_hp_loss("恢復30%生命值"))


class BalancedStrategyTests(unittest.TestCase):
    """balanced 中間檔（使用者拍板，A-1）：拒一切機率損失下行（失去金錢/生命），
    但接受確定性消耗金錢換確定報酬。= conservative 排除清單去掉「確定消耗金錢」那條。
    """

    def _listen_ocr(self):
        # 認真傾聽：消耗50金錢→5音符（確定消耗）；隨意傾聽：免費無下行。
        return [
            ("認真傾聽。", 0.98, _bbox(756, 320, 100, 26)),
            ("消耗50", 0.90, _bbox(1000, 320, 60, 20)),
            ("獲得5個隨機音符", 0.62, _bbox(1075, 320, 160, 20)),
            ("隨意傾聽。", 0.98, _bbox(756, 470, 100, 26)),
            ("50%機率恢復30%生命值", 0.60, _bbox(940, 470, 200, 20)),
            ("50%機率獲得5個隨機音符", 0.60, _bbox(940, 510, 220, 20)),
        ]

    def test_balanced_accepts_definite_money_cost(self) -> None:
        # balanced 接受確定消耗金錢 → 選「認真傾聽」(同 aggressive，異於 conservative 的隨意傾聽)。
        ctx = _make_ctx(self._listen_ocr(), strategy='balanced')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(310 <= cy <= 340, f"balanced 應接受確定消耗、點『認真傾聽』(y≈320)，實點 y={cy}")

    def _gamble_ocr(self):
        return [
            ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
            ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
            ("50%機率失去100", 0.96, _bbox(1094, 330, 130, 24)),
            ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
            ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
            ("70%機率失去200", 0.76, _bbox(1092, 440, 132, 24)),
            ("相信現實", 0.82, _bbox(756, 506, 82, 26)),
            ("獲得30", 0.98, _bbox(1166, 550, 58, 24)),
        ]

    def test_balanced_rejects_probabilistic_loss(self) -> None:
        # balanced：兩賭注皆含「機率失去金錢」→ 拒（同 conservative）→ 選保底「相信現實」。
        ctx = _make_ctx(self._gamble_ocr(), strategy='balanced')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(500 <= cy <= 540, f"balanced 應拒機率損失、選保底『相信現實』(y≈506)，實點 y={cy}")

    def test_balanced_refuses_note_cost(self) -> None:
        # balanced 仍拒消耗音符（音符留協奏）→ 選免費保底「穩紮穩打」。
        ctx = _make_ctx([
            ("傾力一搏。", 0.98, _bbox(756, 320, 100, 26)),
            ("消耗5個隨機音符", 0.90, _bbox(1000, 320, 140, 20)),
            ("獲得1個稀有潛能", 0.62, _bbox(1075, 320, 160, 20)),
            ("穩紮穩打。", 0.98, _bbox(756, 470, 100, 26)),
            ("獲得30", 0.95, _bbox(1166, 470, 58, 20)),
        ], strategy='balanced')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(460 <= cy <= 490, f"balanced 應拒消耗音符、選『穩紮穩打』(y≈470)，實點 y={cy}")

    def test_event_strategy_accepts_balanced(self) -> None:
        self.assertEqual(
            states._event_strategy(SimpleNamespace(config={'event': {'strategy': 'balanced'}})),
            'balanced',
        )


class EventKnobReaderTests(unittest.TestCase):
    """step6/A-2 三旋鈕讀取器：refuse_note_cost / aggressive_gamble_mode /
    same_option_repeat_limit。預設 = 現行寫死值（True / True / 3）。
    """

    def _ctx(self, **event_cfg):
        return SimpleNamespace(config={'event': event_cfg} if event_cfg else {})

    # refuse_note_cost ----------------------------------------------------
    def test_refuse_note_cost_default_true(self) -> None:
        self.assertTrue(states._refuse_note_cost(self._ctx()))

    def test_refuse_note_cost_set_false(self) -> None:
        self.assertFalse(states._refuse_note_cost(self._ctx(refuse_note_cost=False)))

    # aggressive_gamble_mode ---------------------------------------------
    def test_aggressive_gamble_mode_default_true(self) -> None:
        self.assertTrue(states._aggressive_gamble_mode(self._ctx()))

    def test_aggressive_gamble_mode_set_false(self) -> None:
        self.assertFalse(states._aggressive_gamble_mode(self._ctx(aggressive_gamble_mode=False)))

    # same_option_repeat_limit -------------------------------------------
    def test_same_option_repeat_limit_default_3(self) -> None:
        self.assertEqual(states._same_option_repeat_limit(self._ctx()), 3)

    def test_same_option_repeat_limit_set_5(self) -> None:
        self.assertEqual(states._same_option_repeat_limit(self._ctx(same_option_repeat_limit=5)), 5)

    def test_same_option_repeat_limit_bad_value_falls_back_3(self) -> None:
        self.assertEqual(states._same_option_repeat_limit(self._ctx(same_option_repeat_limit='x')), 3)

    def test_same_option_repeat_limit_below_one_falls_back_3(self) -> None:
        self.assertEqual(states._same_option_repeat_limit(self._ctx(same_option_repeat_limit=0)), 3)


class EventKnobBehaviorTests(unittest.TestCase):
    """三旋鈕的行為翻轉（gate 點實際生效）。"""

    def _note_cost_ocr(self):
        # 傾力一搏：消耗5個隨機音符 → 稀有潛能（rank 最好）；穩紮穩打：免費獲得30。
        return [
            ("傾力一搏。", 0.98, _bbox(756, 320, 100, 26)),
            ("消耗5個隨機音符", 0.90, _bbox(1000, 320, 140, 20)),
            ("獲得1個稀有潛能", 0.62, _bbox(1075, 320, 160, 20)),
            ("穩紮穩打。", 0.98, _bbox(756, 470, 100, 26)),
            ("獲得30", 0.95, _bbox(1166, 470, 58, 20)),
        ]

    def test_refuse_note_cost_default_picks_safe(self) -> None:
        # 預設(True)：拒消耗音符 → 選免費「穩紮穩打」(y≈470)。
        ctx = _make_ctx(self._note_cost_ocr(), strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(460 <= cy <= 490, f"預設應拒音符、選『穩紮穩打』(y≈470)，實點 y={cy}")

    def test_refuse_note_cost_false_unlocks_note_option(self) -> None:
        # refuse_note_cost=False：消耗音符選項解禁，稀有潛能 rank 最好 → 選「傾力一搏」(y≈320)。
        ctx = _make_ctx(
            self._note_cost_ocr(), strategy='aggressive',
            extra_event={'refuse_note_cost': False},
        )
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(310 <= cy <= 340, f"解禁後應選稀有潛能『傾力一搏』(y≈320)，實點 y={cy}")

    def _gamble_ocr(self):
        # 賭博：命運列 30% 機率獲得 650（純金錢，最高）。
        return [
            ("相信運氣", 0.99, _bbox(756, 288, 82, 24)),
            ("50%機率獲得200", 0.72, _bbox(934, 330, 132, 24)),
            ("50%機率失去100", 0.96, _bbox(1094, 330, 130, 24)),
            ("相信命運", 0.99, _bbox(756, 396, 82, 26)),
            ("30%機率獲得650", 0.51, _bbox(933, 443, 130, 20)),
            ("70%機率失去200", 0.76, _bbox(1092, 440, 132, 24)),
            ("相信現實", 0.82, _bbox(756, 506, 82, 26)),
            ("獲得30", 0.98, _bbox(1166, 550, 58, 24)),
        ]

    def test_aggressive_gamble_mode_default_picks_650(self) -> None:
        # 預設(True)：走 gamble-max-money → 選 650 列「相信命運」(y≈396~443)。
        ctx = _make_ctx(self._gamble_ocr(), strategy='aggressive')
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertTrue(390 <= cy <= 443, f"預設應走 gamble-max-money 選 650 列，實點 y={cy}")

    def test_aggressive_gamble_mode_false_skips_650(self) -> None:
        # aggressive_gamble_mode=False：不走 max-money 分支 → 不選 650 列。
        # 兩賭注皆含「機率失去金錢」→ generic(aggressive 只拒消耗音符)會挑報酬最好且有
        # 可點標題者；無論落點為何，至少不該落在 650 列(390<=cy<=443)。
        ctx = _make_ctx(
            self._gamble_ocr(), strategy='aggressive',
            extra_event={'aggressive_gamble_mode': False},
        )
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1)
        _cx, cy = ctx.input.clicks[0]
        self.assertFalse(390 <= cy <= 443, f"關閉後不應選 650 列(走 generic)，實點 y={cy}")

    def test_same_option_repeat_limit_one_gives_up_on_second_call(self) -> None:
        # same_option_repeat_limit=1：同一 ctx 連呼 handle_event 兩次（FakeOCR 回固定選項
        # → 同座標）。第 1 次 repeat=0<1 點擊；第 2 次 repeat=1>=1 放棄、clicks 不增加。
        ctx = _make_ctx(
            self._gamble_ocr(), strategy='aggressive',
            extra_event={'same_option_repeat_limit': 1},
        )
        with patch.object(states.time, 'sleep', return_value=None):
            states.handle_event(ctx)
            self.assertEqual(len(ctx.input.clicks), 1, "第 1 次應點擊一次")
            states.handle_event(ctx)
        self.assertEqual(len(ctx.input.clicks), 1, "第 2 次 repeat=1>=1 應放棄、clicks 不再增加")


if __name__ == "__main__":
    unittest.main()
