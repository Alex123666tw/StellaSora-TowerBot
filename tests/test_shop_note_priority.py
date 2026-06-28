"""任務 §3.7（GUI_DESIGN_SPEC §0b 第5點）：shop.buy.note_priority 音符購買優先序。

釘住：
  - _note_priority reader：預設空 / buy 非 dict / note_priority 非 list → [];有值去空白。
  - _order_gaps_by_priority：空 priority → 維持 note_gaps 原序（byte-identical 現行）;
    非空 → priority 命中的音符排前（按 priority 序）、其餘按原 dict 序接後;
    priority 含不在 gaps 的音符 → 忽略。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.states as states


class NotePriorityReaderTests(unittest.TestCase):
    def test_default_empty(self) -> None:
        self.assertEqual(states._note_priority(SimpleNamespace(config={})), [])
        self.assertEqual(states._note_priority(SimpleNamespace(config={"shop": {"buy": {}}})), [])

    def test_non_dict_buy_empty(self) -> None:
        self.assertEqual(states._note_priority(SimpleNamespace(config={"shop": {"buy": "x"}})), [])

    def test_non_list_empty(self) -> None:
        ctx = SimpleNamespace(config={"shop": {"buy": {"note_priority": "風"}}})
        self.assertEqual(states._note_priority(ctx), [])

    def test_values_stripped(self) -> None:
        ctx = SimpleNamespace(config={"shop": {"buy": {"note_priority": ["風", " 絕招 ", ""]}}})
        self.assertEqual(states._note_priority(ctx), ["風", "絕招"])


class OrderGapsByPriorityTests(unittest.TestCase):
    def test_empty_priority_keeps_order(self) -> None:
        # 空 priority → 維持 note_gaps 原序（byte-identical 現行）。
        gaps = {"風": 35, "絕招": 10, "強攻": 18}
        self.assertEqual(states._order_gaps_by_priority(gaps, []), ["風", "絕招", "強攻"])

    def test_priority_reorders(self) -> None:
        gaps = {"風": 35, "絕招": 10, "強攻": 18}
        self.assertEqual(states._order_gaps_by_priority(gaps, ["絕招", "強攻"]), ["絕招", "強攻", "風"])

    def test_priority_not_in_gaps_ignored(self) -> None:
        # 幸運不在 gaps → 忽略;風排前;其餘按原序接後。
        gaps = {"風": 35, "絕招": 10}
        self.assertEqual(states._order_gaps_by_priority(gaps, ["幸運", "風"]), ["風", "絕招"])

    def test_remaining_gaps_appended_in_order(self) -> None:
        gaps = {"風": 35, "絕招": 10, "強攻": 18, "專注": 5}
        self.assertEqual(states._order_gaps_by_priority(gaps, ["強攻"]), ["強攻", "風", "絕招", "專注"])


if __name__ == "__main__":
    unittest.main()
