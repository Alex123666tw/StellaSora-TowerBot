from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from core.decision_engine import DecisionEngine, ScreenOption


def _write_engine_fixture(tmpdir: Path, decision: dict) -> Path:
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
    return config_path


class DecisionEngineTests(unittest.TestCase):
    def test_guaranteed_always_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "guaranteed": ["Lucky"],
                "required": ["Req"],
            })
            engine = DecisionEngine(config_path=str(config_path))
            chosen = engine.decide([
                ScreenOption(name="Req", level=3),
                ScreenOption(name="Lucky", level=1),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "Lucky")
            self.assertEqual(engine.state.current_level("Lucky"), 1)

    def test_required_beats_level_required_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "required": ["Req"],
                "level_required": [{"name": "Lev", "target_level": 1}],
                "backup": ["BakA"],
            })
            engine = DecisionEngine(config_path=str(config_path))
            chosen = engine.decide([
                ScreenOption(name="BakA", level=3),
                ScreenOption(name="Lev", level=1),
                ScreenOption(name="Req", level=1),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "Req")

    def test_reroll_then_backup_group_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "backup": ["BakA", "BakB"],
                "backup_groups": [["BakA", "BakB"]],
                "max_reroll_before_backup": 2,
            })
            engine = DecisionEngine(config_path=str(config_path))
            self.assertIsNone(engine.decide([ScreenOption(name="Other")]))
            self.assertFalse(engine.state.accept_backup)
            self.assertIsNone(engine.decide([ScreenOption(name="Other")]))
            self.assertTrue(engine.state.accept_backup)

            chosen = engine.decide([
                ScreenOption(name="BakA", level=1),
                ScreenOption(name="BakB", level=3),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "BakB")
            self.assertEqual(engine.state.selected_per_group[0], "BakB")

            engine.state.accept_backup = True
            categorized = engine.categorize([
                ScreenOption(name="BakA", level=1),
                ScreenOption(name="BakB", level=1),
            ])
            categories = {opt.name: opt.category for opt in categorized}
            self.assertEqual(categories["BakA"], "unknown")
            self.assertEqual(categories["BakB"], "backup")

    def test_punctuation_variants_match_guaranteed_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            priority_path = tmpdir / "priority_list.json"
            priority_path.write_text(json.dumps({
                "potentials": [
                    {"name": "明日·薪火", "aliases": ["明日·薪火"]},
                    {"name": "往日·餘燼", "aliases": ["往日·餘燼"]},
                    {"name": "餘燼的火星", "aliases": ["餘燼的火星"]},
                ]
            }, ensure_ascii=False), encoding="utf-8")
            config_path = tmpdir / "config.yaml"
            config_path.write_text(yaml.safe_dump({
                "decision": {
                    "guaranteed": ["明日·薪火"],
                    "required": [],
                    "backup": [],
                },
                "ocr": {"priority_list_path": str(priority_path)},
            }, allow_unicode=True), encoding="utf-8")

            engine = DecisionEngine(config_path=str(config_path))
            chosen = engine.decide([
                ScreenOption(name="明日:薪火", level=1),
                ScreenOption(name="餘燼的火星", level=1),
                ScreenOption(name="往日:餘燼", level=1),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "明日:薪火".replace(":", ":"))
            self.assertEqual(chosen.category, "guaranteed")

    def test_recommendation_badge_mode_chooses_highest_recommended_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "mode": "recommendation_badge",
                "guaranteed": ["Lucky"],
                "required": ["Req"],
                "backup": ["BakA"],
            })
            engine = DecisionEngine(config_path=str(config_path))
            chosen = engine.decide([
                ScreenOption(name="Lucky", level=6, recommended=False, position=(100, 0)),
                ScreenOption(name="Req", level=1, recommended=True, position=(200, 0)),
                ScreenOption(name="BakA", level=3, recommended=True, position=(300, 0)),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "BakA")
            self.assertEqual(chosen.category, "recommended")
            self.assertEqual(engine.state.current_level("BakA"), 0)

    def test_recommendation_badge_mode_tiebreaks_by_leftmost_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "mode": "recommendation_badge",
            })
            engine = DecisionEngine(config_path=str(config_path))
            chosen = engine.decide([
                ScreenOption(name="Right", level=2, recommended=True, position=(600, 0)),
                ScreenOption(name="Left", level=2, recommended=True, position=(200, 0)),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "Left")

    def test_recommendation_badge_mode_rerolls_without_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "mode": "recommendation_badge",
                "guaranteed": ["Lucky"],
            })
            engine = DecisionEngine(config_path=str(config_path))
            # preview 不可改 reroll 狀態。
            preview = engine.preview_decision([ScreenOption(name="Lucky", level=6, recommended=False)])
            self.assertIsNone(preview)
            self.assertEqual(engine.state.reroll_count, 0)
            # decide：無紅字推薦 → reroll，且計數遞增（新：有上限）。
            chosen = engine.decide([ScreenOption(name="Lucky", level=6, recommended=False)])
            self.assertIsNone(chosen)
            self.assertEqual(engine.state.reroll_count, 1)

    def test_recommendation_badge_mode_bounds_reroll_then_takes_card(self) -> None:
        # session 20260613_223142：上樓後 3 卡全無紅字推薦 → 此模式原本無限 reroll，
        # 加上 reroll 鈕為 icon 文字點不到 → 卡死。新行為：reroll 上限內續抽，達上限
        # 改取最佳卡（最高等級、平手取最左）保證推進（永不卡死）。
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "mode": "recommendation_badge",
                "max_reroll_before_backup": 2,
            })
            engine = DecisionEngine(config_path=str(config_path))

            def opts():
                return [
                    ScreenOption(name="OptA", level=2, recommended=False, position=(200, 0)),
                    ScreenOption(name="OptB", level=3, recommended=False, position=(400, 0)),
                    ScreenOption(name="OptC", level=1, recommended=False, position=(600, 0)),
                ]

            # preview 不改狀態。
            self.assertIsNone(engine.preview_decision(opts()))
            self.assertEqual(engine.state.reroll_count, 0)
            # 上限內：reroll（None），計數遞增。
            self.assertIsNone(engine.decide(opts()))
            self.assertEqual(engine.state.reroll_count, 1)
            self.assertIsNone(engine.decide(opts()))
            self.assertEqual(engine.state.reroll_count, 2)
            # 達上限 → 不再 reroll，取最高等級卡（OptB, +3），reroll 計數歸零。
            chosen = engine.decide(opts())
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "OptB")
            self.assertEqual(engine.state.reroll_count, 0)

    def test_recommendation_badge_mode_resets_reroll_after_taking_recommended(self) -> None:
        # 連抽幾次後出現紅字推薦 → 取卡並把 reroll 計數歸零（下一畫面重新計）。
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_engine_fixture(Path(tmp), {
                "mode": "recommendation_badge",
                "max_reroll_before_backup": 3,
            })
            engine = DecisionEngine(config_path=str(config_path))
            self.assertIsNone(engine.decide([ScreenOption(name="X", level=1, recommended=False)]))
            self.assertEqual(engine.state.reroll_count, 1)
            chosen = engine.decide([
                ScreenOption(name="Y", level=2, recommended=True, position=(100, 0)),
                ScreenOption(name="Z", level=1, recommended=False, position=(300, 0)),
            ])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.name, "Y")
            self.assertEqual(engine.state.reroll_count, 0)
