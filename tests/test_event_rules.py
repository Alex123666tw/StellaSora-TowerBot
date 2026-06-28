"""tests/test_event_rules.py

驗收:event_rules 後端載入+比對。
  (a) 空 overrides → 走原路徑(結果 byte-identical)。
  (b) match_any 命中 + pick_any 選到對應選項座標。
  (c) match_any 不含於畫面 → 不命中、落回原邏輯。
  (d) 防呆:檔案不存在/yaml 格式錯 → 空規則不拋錯。
  (e) reason 字串格式為 'event_rule:<id>'。
先寫為紅測試(未實作前全部失敗),實作後轉綠。
"""
from __future__ import annotations

import importlib
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

import core.states as states
from tests.fakes import FakeOCR


# ---------------------------------------------------------------------------
# 輔助函數
# ---------------------------------------------------------------------------

def _bbox(x: int, y: int, w: int = 60, h: int = 24) -> tuple:
    """回傳四角座標(左上→右上→右下→左下),符合 read_text 期望格式。"""
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _make_ctx(question_texts: list[str], option_texts: list[tuple[str, int, int]]) -> SimpleNamespace:
    """
    建立最小化的 BotContext stub。

    question_texts: 問題面板文字列表(字串)。
    option_texts: 選項 (text, cx, cy) 列表；cx/cy 需落在 _choice_panel_roi 內。

    1920x1080 下:
    - 問題 ROI: x=1036, y=194, w=653, h=151   → 中心需在 [1036,1689] × [194,345]
    - 選項 ROI: x=960,  y=324, w=864, h=583   → 中心需在 [960, 1824] × [324,907]
    """
    frame_w, frame_h = 1920, 1080
    # 問題 ROI 中心約 (1036+326, 194+75) = (1362, 269)
    q_cx, q_cy = 1100, 260
    q_items = [
        (text, 0.9, _bbox(q_cx - 30, q_cy - 10))
        for text in question_texts
    ]

    # 選項 ROI 中心:x 從 1000 起,y 從 400 起,每列 y+80
    opt_items = []
    for text, cx, cy in option_texts:
        opt_items.append((text, 0.9, _bbox(cx - 30, cy - 10)))

    return SimpleNamespace(
        ocr=FakeOCR(global_results=q_items + opt_items),
        last_frame=np.zeros((frame_h, frame_w, 3), dtype=np.uint8),
        frame_w=frame_w,
        frame_h=frame_h,
        # 空 config → 讓各策略參數讀不到時退 default
        config={},
        ocr_trace=None,
    )


def _write_event_rules_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / 'event_rules.yaml'
    p.write_text(content, encoding='utf-8')
    return p


def _reload_states():
    """重載 states 模組以清除模組級快取 (_event_rules_cache)。"""
    importlib.reload(states)


# ---------------------------------------------------------------------------
# (d) 防呆:檔案不存在/yaml 格式錯 → 空規則不拋錯
# ---------------------------------------------------------------------------

class TestEventRulesLoadGuard:
    def test_missing_file_returns_empty_list(self, tmp_path):
        """data/event_rules.yaml 不存在 → 空 list,不拋 FileNotFoundError。"""
        _reload_states()
        nonexistent = tmp_path / 'no_such_event_rules.yaml'
        with patch.object(states, '_get_event_rules_path', return_value=nonexistent):
            result = states._load_event_rules()
        assert result == []

    def test_invalid_yaml_returns_empty_list(self, tmp_path):
        """yaml 格式錯 → 空 list,不拋 yaml.YAMLError。"""
        _reload_states()
        bad_yaml = _write_event_rules_yaml(tmp_path, ': : : invalid yaml :::\n  - [broken')
        with patch.object(states, '_get_event_rules_path', return_value=bad_yaml):
            result = states._load_event_rules()
        assert result == []

    def test_empty_overrides_returns_empty_list(self, tmp_path):
        """overrides: [] → 空 list。"""
        _reload_states()
        yaml_file = _write_event_rules_yaml(tmp_path, 'overrides: []\n')
        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._load_event_rules()
        assert result == []

    def test_missing_overrides_key_returns_empty_list(self, tmp_path):
        """yaml 有內容但無 overrides key → 空 list。"""
        _reload_states()
        yaml_file = _write_event_rules_yaml(tmp_path, 'something: else\n')
        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._load_event_rules()
        assert result == []

    def test_overrides_not_list_returns_empty_list(self, tmp_path):
        """overrides 不是 list → 空 list。"""
        _reload_states()
        yaml_file = _write_event_rules_yaml(tmp_path, 'overrides: "wrong_type"\n')
        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._load_event_rules()
        assert result == []


# ---------------------------------------------------------------------------
# (a) 空 overrides → byte-identical(不改變原邏輯結果)
# ---------------------------------------------------------------------------

class TestEventRulesEmptyByteIdentical:
    def test_empty_overrides_does_not_intercept(self, tmp_path):
        """空 overrides → _select_event_option 不進 event_rule 分支,走 strategy 路徑。

        畫面:選項兩個(免費選項),策略激進 → generic 評分選第一個(rank 最好=最小)。
        加了空規則後結果必須完全相同,不會 return 不同座標或 None。
        """
        _reload_states()
        yaml_file = _write_event_rules_yaml(tmp_path, 'overrides: []\n')

        # 選項 cx=1000,cy=400 / cx=1000,cy=480 — 落在選項 ROI 內
        ctx = _make_ctx(
            question_texts=['普通事件'],
            option_texts=[
                ('選項甲', 1000, 400),
                ('選項乙', 1000, 480),
            ],
        )
        ctx.config = {'event': {'strategy': 'aggressive',
                                'refuse_note_cost': True,
                                'aggressive_gamble_mode': False}}

        with (
            patch.object(states, '_get_event_rules_path', return_value=yaml_file),
            patch.object(states, 'time') as mock_time,
        ):
            mock_time.sleep = lambda *a, **k: None
            result_with_rules = states._select_event_option(ctx)

        # 不帶規則檔的 baseline:patch path 指到不存在的檔 → 空規則
        _reload_states()
        nonexistent = tmp_path / 'empty.yaml'
        ctx2 = _make_ctx(
            question_texts=['普通事件'],
            option_texts=[
                ('選項甲', 1000, 400),
                ('選項乙', 1000, 480),
            ],
        )
        ctx2.config = {'event': {'strategy': 'aggressive',
                                 'refuse_note_cost': True,
                                 'aggressive_gamble_mode': False}}

        with (
            patch.object(states, '_get_event_rules_path', return_value=nonexistent),
            patch.object(states, 'time') as mock_time2,
        ):
            mock_time2.sleep = lambda *a, **k: None
            result_baseline = states._select_event_option(ctx2)

        # 兩個結果必須相同(byte-identical)
        assert result_with_rules == result_baseline, (
            f'空 overrides 改變了輸出: {result_with_rules!r} vs baseline {result_baseline!r}'
        )


# ---------------------------------------------------------------------------
# (b) match_any 命中 → pick_any 選到對應選項座標 + reason 格式驗證
# ---------------------------------------------------------------------------

class TestEventRulesHit:
    def test_match_any_hit_picks_correct_option(self, tmp_path):
        """match_any 命中 → pick_any 對應選項座標，reason='event_rule:<id>'。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: test_number_quiz
                match_any: ["最喜歡哪個數字", "喜歡哪個數字"]
                pick_any: ["總是如此", "3"]
                note: "測試 quiz"
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        # 問題面板包含 match_any[0]「最喜歡哪個數字」
        # 選項面板:「總是如此」(應被 pick)、「隨機一個」(不應被選)
        ctx = _make_ctx(
            question_texts=['最喜歡哪個數字'],
            option_texts=[
                ('總是如此', 1000, 400),
                ('隨機一個', 1000, 480),
            ],
        )

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        assert result is not None, '命中 event_rule 後應回傳非 None'
        x, y, reason = result
        assert reason == 'event_rule:test_number_quiz', (
            f'reason 應為 event_rule:<id>,實得 {reason!r}'
        )
        # 座標應指向「總是如此」的中心 — cx=1000-30+30=1000, cy 約 400-10+12=402
        # 用寬鬆範圍確認(OCR bbox center_x = cx-30+30 = cx, center_y = cy-10+12 = cy+2)
        assert 980 <= x <= 1030, f'x 座標偏離預期: {x}'
        assert 390 <= y <= 420, f'y 座標偏離預期: {y}'

    def test_match_any_second_keyword_also_hits(self, tmp_path):
        """match_any 第二個關鍵字也能命中。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: multi_keyword_test
                match_any: ["關鍵字甲", "關鍵字乙"]
                pick_any: ["選這個"]
                note: ""
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['關鍵字乙出現了'],
            option_texts=[
                ('選這個', 1000, 400),
                ('別選', 1000, 480),
            ],
        )

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        assert result is not None
        _x, _y, reason = result
        assert reason == 'event_rule:multi_keyword_test'

    def test_first_rule_takes_priority_when_both_match(self, tmp_path):
        """多條規則同時命中 → 取第一條(優先序)。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: first_rule
                match_any: ["共同關鍵字"]
                pick_any: ["選項一"]
                note: "第一條"
              - id: second_rule
                match_any: ["共同關鍵字"]
                pick_any: ["選項二"]
                note: "第二條"
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['共同關鍵字'],
            option_texts=[
                ('選項一', 1000, 400),
                ('選項二', 1000, 480),
            ],
        )

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        assert result is not None
        _x, _y, reason = result
        assert reason == 'event_rule:first_rule', (
            f'應取第一條規則,實得 {reason!r}'
        )

    def test_pick_any_partial_match_works(self, tmp_path):
        """pick_any 的字串是選項文字的子字串也能命中(含有即可)。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: partial_pick
                match_any: ["事件A"]
                pick_any: ["選擇"]
                note: "部分比對"
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['這是事件A的測試'],
            option_texts=[
                ('請選擇正確答案', 1000, 400),  # 含「選擇」子字串
                ('放棄', 1000, 480),
            ],
        )

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        assert result is not None
        _x, _y, reason = result
        assert reason == 'event_rule:partial_pick'


# ---------------------------------------------------------------------------
# (c) match_any 不含於畫面 → 不命中、落回原邏輯
# ---------------------------------------------------------------------------

class TestEventRulesMiss:
    def test_no_match_falls_through_to_existing_logic(self, tmp_path):
        """match_any 不在問題/畫面文字中 → 不命中,走原 strategy 路徑(不 early return)。

        原邏輯:激進策略 + 無音符成本 + 無金錢成本 → 選 rank 最小(第一個)選項。
        結果應與無規則時相同(不為 None,且 reason 不含 'event_rule:')。
        """
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: irrelevant_rule
                match_any: ["這段文字不在畫面中"]
                pick_any: ["選項一"]
                note: ""
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['完全不同的畫面文字'],
            option_texts=[
                ('選項甲', 1000, 400),
                ('選項乙', 1000, 480),
            ],
        )
        ctx.config = {'event': {'strategy': 'aggressive',
                                'refuse_note_cost': True,
                                'aggressive_gamble_mode': False}}

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        # 原邏輯應回傳非 None(有選項可選)
        assert result is not None, '未命中規則時應落回原邏輯,不應回 None'
        _x, _y, reason = result
        assert 'event_rule:' not in reason, (
            f'未命中時 reason 不應含 event_rule:,實得 {reason!r}'
        )

    def test_empty_match_any_does_not_match(self, tmp_path):
        """match_any 為空列表 → 不命中(空列表不能匹配任何東西)。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: empty_match
                match_any: []
                pick_any: ["選項一"]
                note: ""
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['任意文字'],
            option_texts=[('選項一', 1000, 400), ('選項二', 1000, 480)],
        )
        ctx.config = {'event': {'strategy': 'aggressive',
                                'refuse_note_cost': True,
                                'aggressive_gamble_mode': False}}

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        if result is not None:
            _x, _y, reason = result
            assert 'event_rule:' not in reason

    def test_pick_any_not_found_in_options_falls_through(self, tmp_path):
        """match_any 命中但 pick_any 無法在選項中找到 → 落回原邏輯(不 early return)。"""
        _reload_states()
        yaml_content = textwrap.dedent("""\
            overrides:
              - id: pick_miss
                match_any: ["事件文字"]
                pick_any: ["根本不存在的選項"]
                note: ""
        """)
        yaml_file = _write_event_rules_yaml(tmp_path, yaml_content)

        ctx = _make_ctx(
            question_texts=['事件文字'],
            option_texts=[
                ('選項甲', 1000, 400),
                ('選項乙', 1000, 480),
            ],
        )
        ctx.config = {'event': {'strategy': 'aggressive',
                                'refuse_note_cost': True,
                                'aggressive_gamble_mode': False}}

        with patch.object(states, '_get_event_rules_path', return_value=yaml_file):
            result = states._select_event_option(ctx)

        # 若 pick_any 找不到,應落回原邏輯(非 event_rule reason)
        if result is not None:
            _x, _y, reason = result
            assert 'event_rule:' not in reason
