"""gui/event_rules_io.py 事件規則讀寫純函數測試（GUI_DESIGN_SPEC §10 步8 / §4）。

讀寫 data/event_rules.yaml 的 overrides;與 states._load_event_rules（bot 端讀取）共用同一檔。
"""
from __future__ import annotations

import pytest

from gui import event_rules_io


@pytest.fixture
def tmp_rules(tmp_path, monkeypatch):
    p = tmp_path / "event_rules.yaml"
    monkeypatch.setattr(event_rules_io, "_RULES_PATH", p)
    return p


class TestNormalize:
    def test_fills_missing(self):
        assert event_rules_io.normalize_override({}) == {
            "id": "", "match_any": [], "pick_any": [], "note": ""}

    def test_strips_and_filters(self):
        o = {"id": " q1 ", "match_any": [" 數字 ", ""], "pick_any": ["3", "  "], "note": " x "}
        assert event_rules_io.normalize_override(o) == {
            "id": "q1", "match_any": ["數字"], "pick_any": ["3"], "note": "x"}


class TestValid:
    def test_valid(self):
        assert event_rules_io.is_valid_override(
            {"id": "q", "match_any": ["a"], "pick_any": ["b"]})

    def test_invalid_missing_field(self):
        assert not event_rules_io.is_valid_override({"id": "q", "match_any": ["a"], "pick_any": []})
        assert not event_rules_io.is_valid_override({"id": "", "match_any": ["a"], "pick_any": ["b"]})


class TestLoadSave:
    def test_load_empty_when_missing(self, tmp_rules):
        assert event_rules_io.load_overrides() == []

    def test_save_then_load_roundtrip(self, tmp_rules):
        ov = [{"id": "q1", "match_any": ["數字"], "pick_any": ["3"], "note": "猜數字"}]
        event_rules_io.save_overrides(ov)
        assert event_rules_io.load_overrides() == ov

    def test_load_malformed_returns_empty(self, tmp_rules):
        tmp_rules.write_text(": : bad yaml :::\n", encoding="utf-8")
        assert event_rules_io.load_overrides() == []

    def test_save_normalizes(self, tmp_rules):
        event_rules_io.save_overrides(
            [{"id": " a ", "match_any": ["x"], "pick_any": ["y"], "note": ""}])
        assert event_rules_io.load_overrides()[0]["id"] == "a"

    def test_save_preserves_order(self, tmp_rules):
        ov = [
            {"id": "first", "match_any": ["a"], "pick_any": ["1"], "note": ""},
            {"id": "second", "match_any": ["b"], "pick_any": ["2"], "note": ""},
        ]
        event_rules_io.save_overrides(ov)
        assert [o["id"] for o in event_rules_io.load_overrides()] == ["first", "second"]
