"""
GUI 監控頁顯示修復測試（offscreen，絕不開視窗）。

涵蓋 repair/phase-3 的三項顯示 bug 修復：
  - 改動 B-1：辨識信心條接真值（mon_conf_bar / mon_conf_label）。
  - 改動 B-2：商店造訪格接真實計數（shop_visits，floor 後備）。
  - 改動 C：設定頁/進階頁補上 shop.upgrade.enabled、bot.click_settle 旋鈕。

規則：
  - QT_QPA_PLATFORM=offscreen，只建 QApplication + 直接呼叫方法斷言，絕不 window.show()。
  - keyboard.add_hotkey monkeypatch 成 no-op，避免在測試機註冊真實全域快捷鍵。
參考既有風格：tests/test_progress.py。
"""
from __future__ import annotations

import os

import PyQt5

# Qt 外掛路徑（比照 main_gui.py）：本機需顯式指定才找得到 platform 外掛，
# 否則連 offscreen 都載入失敗 → 會彈出 "no Qt platform plugin" 致命對話框。
_plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
os.environ["QT_PLUGIN_PATH"] = _plugin_path
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(_plugin_path, "platforms")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PyQt5.QtWidgets import QApplication

import gui.app as gui_app


def _make_app_singleton() -> QApplication:
    """整個測試行程共用單一 QApplication（QApplication 不可重複建立）。"""
    existing = QApplication.instance()
    if existing is not None:
        return existing
    return QApplication([])


def _base_stats(**overrides) -> dict:
    stats = {
        "floor": 0,
        "shop_visits": 0,
        "runs": 0,
        "max_runs": 30,
        "success": 0,
        "state": "STATE_LOBBY",
        "confidence": 0.0,
        "money": 0,
        "card_counter": {
            "enabled": False,
            "initial_total": 0,
            "current_total": 0,
            "target_total": 0,
        },
        "notes": {"current": {}, "target": {}},
    }
    stats.update(overrides)
    return stats


class GuiMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._qapp = _make_app_singleton()
        # 避免在測試機真的綁全域熱鍵
        cls._orig_add_hotkey = gui_app.keyboard.add_hotkey
        gui_app.keyboard.add_hotkey = lambda *a, **k: None
        cls.window = gui_app.StellaSoraApp()

    @classmethod
    def tearDownClass(cls) -> None:
        gui_app.keyboard.add_hotkey = cls._orig_add_hotkey
        cls.window.deleteLater()

    # ── 改動 B-1：辨識信心條 ──────────────────────────────────────────────
    def test_confidence_bar_reflects_value(self) -> None:
        self.window.update_status(_base_stats(confidence=0.92))
        self.assertAlmostEqual(self.window.mon_conf_bar.value(), 92, delta=1)
        self.assertIn("92", self.window.mon_conf_label.text())

    def test_confidence_missing_resets_to_dash(self) -> None:
        # 餵入沒有 confidence 的 dict → bar=0、label 顯示 "—"，且不噴例外
        stats = _base_stats()
        del stats["confidence"]
        self.window.update_status(stats)
        self.assertEqual(self.window.mon_conf_bar.value(), 0)
        self.assertIn("—", self.window.mon_conf_label.text())

    def test_confidence_none_resets_to_dash(self) -> None:
        self.window.update_status(_base_stats(confidence=None))
        self.assertEqual(self.window.mon_conf_bar.value(), 0)
        self.assertIn("—", self.window.mon_conf_label.text())

    def test_confidence_clamped_to_range(self) -> None:
        # 超出 1.0 / 低於 0 都該被 clamp 到 0–100，setValue 不報錯
        self.window.update_status(_base_stats(confidence=1.5))
        self.assertEqual(self.window.mon_conf_bar.value(), 100)
        self.window.update_status(_base_stats(confidence=-0.3))
        self.assertEqual(self.window.mon_conf_bar.value(), 0)

    # ── 改動 B-2：商店造訪計數 ────────────────────────────────────────────
    def test_shop_visits_uses_real_count(self) -> None:
        self.window.update_status(_base_stats(shop_visits=3, floor=0))
        self.assertIn("3", self.window.metric_visit[1].text())

    def test_shop_visits_falls_back_to_floor(self) -> None:
        stats = _base_stats(floor=5)
        del stats["shop_visits"]
        self.window.update_status(stats)
        self.assertIn("5", self.window.metric_visit[1].text())

    # ── 改動 C：新旋鈕登記進 _setting_widgets ─────────────────────────────
    def test_new_settings_registered(self) -> None:
        self.assertIn("bot.click_settle", self.window._setting_widgets)
        self.assertIn("shop.upgrade.enabled", self.window._setting_widgets)


if __name__ == "__main__":
    unittest.main()
