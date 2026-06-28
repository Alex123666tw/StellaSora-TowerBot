import yaml
import keyboard
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSlot, QMetaObject
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QLabel,
    QFrame, QSizePolicy, QDialog, QInputDialog, QFileDialog
)
from PyQt5.QtGui import QFont
from qfluentwidgets import (
    SubtitleLabel, BodyLabel, StrongBodyLabel, CaptionLabel, PushButton,
    PrimaryPushButton, TextEdit, CardWidget, SpinBox, DoubleSpinBox,
    LineEdit, ComboBox, SwitchButton, Pivot, IconWidget, SingleDirectionScrollArea,
    FluentIcon, ProgressBar, CheckBox
)

from gui.signals import signals
from gui.workers import BotWorker
from gui import config_io, profiles
from gui.settings_schema import ALL_SETTINGS, by_key, by_module
from utils.window_mgr import WindowManager

_CONFIG_PATH = Path("config.yaml")

# 設定頁四模組卡片標題 icon + 強調色（mockup 頁面1 模組標題）。
_MODULE_META = {
    "選卡": (FluentIcon.TILES, "#534AB7"),
    "結算": (FluentIcon.FLAG, "#185FA5"),
    "商店": (FluentIcon.SHOPPING_CART, "#0F6E56"),
    "事件": (FluentIcon.GAME, "#993C1D"),
}

# ── 設定頁布局（嚴格照 mockup 頁面1：精選 + 順序，不是 schema 全 render）──────
# 每張卡列出要顯示的 schema key（照 mockup 由上而下）。特殊標記：
#   "__upgrade_schedule__" = 逐次強化/順序（times_by_visit + order_by_visit 合併摘要入口）
_SETTINGS_PAGE_LAYOUT = {
    "選卡": [
        "decision.mode",
        "card_counter.target_total",
        "decision.upgrade_strategy",
        "decision.recommendation_target.enabled",
        "decision.required_target_level",
        "decision.min_level_threshold",
    ],
    "結算": [
        "result.rating_threshold",
        "result.require_all_secrets",
        "result.potential_total_threshold",
    ],
    "商店": [
        "shop.buy.strategy",
        "shop.buy.affordability",
        "shop.buy.prefer_discount",
        "shop.buy.buy_non_discounted",
        "shop.refresh.trigger",
        "shop.refresh.start_from_visit",
        "shop.upgrade.enabled",
        "__upgrade_schedule__",
        "shop.upgrade.price_ceiling",
        "shop.buy.note_priority",
        "shop.post_target.note_spree",
    ],
    "事件": [
        "event.strategy",
        "event.refuse_note_cost",
        "event.aggressive_gamble_mode",
        "event_rules",
    ],
}
_LEFT_COLUMN_MODULES = ("選卡", "結算")
_RIGHT_COLUMN_MODULES = ("商店", "事件")

# ── 進階頁布局（照 mockup 頁面3：精選 6 條，danger 項需勾風險才解鎖）──────────
_ADVANCED_PAGE_LAYOUT = [
    "bot.poll_interval",
    "bot.take_settle_delay",
    "bot.click_settle",
    "bot.ocr_cache.enabled",
    "bot.adaptive_settle.enabled",   # danger
    "vision.detector",               # danger
    "window.capture_mode",           # danger
]

# 在設定頁以「摘要入口」（唯讀摘要 + ＞，點開編輯留後續）呈現的 key（mockup 的 .sel 摘要列）。
_SUMMARY_ENTRY_KEYS = {"shop.buy.note_priority"}

# tier 小標籤（mockup .bt.t 測試版 / .bt.adv 進階）。
_TIER_BADGE_STYLE = {
    "test": ("測試版", "#0C447C", "#E6F1FB"),
    "advanced": ("進階", "#5F5E5A", "#F1EFE8"),
    "danger": ("危險", "#A32D2D", "#FCEBEB"),
}

# 監控頁 FSM 流程步驟（mockup 頁面2 流程進度）。
_FLOW_STEPS = [
    ("lobby", "大廳"), ("formation", "編隊"), ("prepare", "準備"),
    ("battle", "快速戰鬥"), ("potential", "選潛能"), ("note", "獲得音符"),
    ("event", "隨機事件"), ("shop", "商店"), ("result", "結算"),
]
_STATE_TO_FLOW = {
    "STATE_LOBBY": "lobby", "STATE_FORMATION": "formation",
    "STATE_PREPARE": "prepare", "STATE_FAST_BATTLE": "battle",
    "STATE_POTENTIAL_SELECT": "potential", "STATE_NOTE_ACQUIRED": "note",
    "STATE_EVENT": "event", "STATE_SHOP": "shop", "STATE_SHOP_CHOICE": "shop",
    "STATE_LEAVE_TOWER_CONFIRM": "result", "STATE_RESULT": "result",
}
# 監控頁「當前生效設定總覽」要顯示的項（mockup 頁面2）。
_SUMMARY_OVERVIEW_KEYS = [
    ("decision.mode", "決策模式"),
    ("decision.upgrade_strategy", "升等策略"),
    ("event.strategy", "事件策略"),
    ("shop.buy.strategy", "商店買法"),
    ("result.rating_threshold", "評分達標門檻"),
]


def _make_tier_badge(tier):
    spec = _TIER_BADGE_STYLE.get(tier)
    if not spec:
        return None
    text, fg, bg = spec
    badge = QLabel(text)
    badge.setStyleSheet(
        f"QLabel{{color:{fg};background:{bg};border-radius:6px;"
        f"padding:1px 7px;font-size:11px;}}"
    )
    return badge


class StellaSoraApp(QWidget):
    def __init__(self):
        super().__init__()
        self.bot_worker = None
        # key -> (Setting, 主控件, 附屬控件 or None)
        self._setting_widgets = {}
        # 監控頁元件參照
        self._flow_widgets = {}
        self._overview_labels = {}

        self.setWindowTitle("星塔旅人 Auto-Helper")
        self.resize(880, 780)
        self.setStyleSheet("StellaSoraApp { background-color: #F3F3F3; }")

        self.setup_ui()
        self.bind_signals()
        self.load_config()
        self._refresh_profiles()
        self._refresh_overview()

        try:
            keyboard.add_hotkey(self._configured_hotkey(), self._on_hotkey_pressed)
        except Exception as e:
            self.append_log(f"[UI] ⚠️ 無法綁定全域快捷鍵 (需手動停止掛機)。錯誤: {e}")

    # ─── 全域快捷鍵 ──────────────────────────────────────────────────────────
    def _configured_hotkey(self) -> str:
        """讀 config bot.hotkey_stop 作為全域中止鍵（預設 ctrl+q）。"""
        try:
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                hk = (cfg.get("bot", {}) or {}).get("hotkey_stop", "ctrl+q")
                return str(hk or "ctrl+q").strip()
        except Exception:
            pass
        return "ctrl+q"

    def _on_hotkey_pressed(self):
        QMetaObject.invokeMethod(self, "stop_bot_from_hotkey", Qt.QueuedConnection)

    @pyqtSlot()
    def stop_bot_from_hotkey(self):
        self.append_log("[UI] 收到 Ctrl+Q 全域中斷指令！")
        self.stop_bot()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ─── 頂層：三頁籤 ────────────────────────────────────────────────────────
    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 16, 20, 16)
        self.main_layout.setSpacing(12)

        self.pivot = Pivot(self)
        self.stack = QStackedWidget(self)

        self.settings_page = self._build_settings_page()
        self.monitor_page = self._build_monitor_page()
        self.advanced_page = self._build_advanced_page()

        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.monitor_page)
        self.stack.addWidget(self.advanced_page)

        self.pivot.addItem(routeKey="settings", text="設定", icon=FluentIcon.SETTING,
                           onClick=lambda: self.stack.setCurrentWidget(self.settings_page))
        self.pivot.addItem(routeKey="monitor", text="監控", icon=FluentIcon.SPEED_HIGH,
                           onClick=lambda: self.stack.setCurrentWidget(self.monitor_page))
        self.pivot.addItem(routeKey="advanced", text="進階", icon=FluentIcon.DEVELOPER_TOOLS,
                           onClick=lambda: self.stack.setCurrentWidget(self.advanced_page))
        self.pivot.setCurrentItem("settings")
        self.stack.setCurrentWidget(self.settings_page)
        self.stack.currentChanged.connect(self._on_stack_changed)

        self.main_layout.addWidget(self.pivot)
        self.main_layout.addWidget(self.stack, 1)

    def _on_stack_changed(self, index):
        route = {0: "settings", 1: "monitor", 2: "advanced"}.get(index)
        if route:
            self.pivot.setCurrentItem(route)

    # ─── 設定頁 ──────────────────────────────────────────────────────────────
    def _build_settings_page(self):
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        outer.addWidget(self._build_settings_toolbar())

        scroll = SingleDirectionScrollArea(orient=Qt.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.enableTransparentBackground()

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 2, 2)
        content_layout.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        for mod in _LEFT_COLUMN_MODULES:
            left_col.addWidget(self._build_module_card(mod))
        left_col.addStretch(1)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        for mod in _RIGHT_COLUMN_MODULES:
            right_col.addWidget(self._build_module_card(mod))
        right_col.addStretch(1)

        grid.addLayout(left_col, 0, 0)
        grid.addLayout(right_col, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        content_layout.addLayout(grid)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save_config = PushButton(FluentIcon.SAVE, "儲存設定", page)
        self.btn_save_config.clicked.connect(self.save_config)
        save_row.addWidget(self.btn_save_config)
        content_layout.addLayout(save_row)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return page

    def _build_settings_toolbar(self):
        bar = CardWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(8)

        layout.addWidget(CaptionLabel("設定檔", bar))
        self.combo_profile = ComboBox(bar)
        self.combo_profile.setMinimumWidth(110)
        layout.addWidget(self.combo_profile)

        for text, icon, handler in (
            ("新建", FluentIcon.ADD, self._on_profile_save_as),
            ("另存", None, self._on_profile_save_as),
            ("載入", None, self._on_profile_load),
            ("導出", FluentIcon.DOWNLOAD, self._on_profile_export),
            ("匯入", FluentIcon.UP, self._on_profile_import),
        ):
            btn = PushButton(text, bar) if icon is None else PushButton(icon, text, bar)
            btn.clicked.connect(handler)
            layout.addWidget(btn)

        layout.addStretch(1)

        layout.addWidget(CaptionLabel("最大輪數", bar))
        self.spin_runs = SpinBox(bar)
        self.spin_runs.setRange(0, 999)
        self.spin_runs.setValue(1)
        self.spin_runs.setMinimumWidth(84)
        self.spin_runs.setToolTip("bot 自動執行的最大輪數。1＝跑完一輪即停；0＝無限輪。")
        layout.addWidget(self.spin_runs)

        self.btn_start = PrimaryPushButton(FluentIcon.PLAY, "開始", bar)
        layout.addWidget(self.btn_start)

        self.btn_help = PushButton(FluentIcon.HELP, "說明", bar)
        self.btn_help.setToolTip("顯示所有設定的說明")
        layout.addWidget(self.btn_help)
        return bar

    def _build_module_card(self, module):
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 8, 18, 14)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon, color = _MODULE_META.get(module, (FluentIcon.SETTING, "#5F5E5A"))
        icon_w = IconWidget(icon, card)
        icon_w.setFixedSize(18, 18)
        header.addWidget(icon_w)
        header.addWidget(StrongBodyLabel(module, card))
        header.addStretch(1)
        layout.addLayout(header)

        for key in _SETTINGS_PAGE_LAYOUT.get(module, []):
            layout.addWidget(self._build_setting_row(key))
        return card

    def _build_setting_row(self, key):
        """產一列：label（左）+ 控件（右），列間有上分隔線（mockup .row）。"""
        row = QWidget()
        row.setObjectName("srow")
        row.setStyleSheet("QWidget#srow{border-top:1px solid rgba(0,0,0,0.10);}")
        hbox = QHBoxLayout(row)
        hbox.setContentsMargins(0, 9, 0, 9)
        hbox.setSpacing(10)

        # 特殊：逐次強化/順序（合併摘要入口）
        if key == "__upgrade_schedule__":
            lbl = BodyLabel("逐次強化/順序", row)
            lbl.setToolTip("第幾次造訪商店強化幾次 + 先升級機/先商店（times_by_visit + order_by_visit）")
            hbox.addWidget(lbl)
            badge = _make_tier_badge("advanced")
            if badge:
                hbox.addWidget(badge)
            hbox.addStretch(1)
            hbox.addWidget(self._make_summary_entry(self._summarize_upgrade_schedule(),
                                                    "逐次強化/順序的細項編輯留後續步驟"))
            return row

        s = by_key(key)
        if s is None:
            hbox.addWidget(BodyLabel(key, row))
            hbox.addStretch(1)
            return row

        lbl = BodyLabel(s.label, row)
        lbl.setToolTip(s.help)
        hbox.addWidget(lbl)
        badge = _make_tier_badge(s.tier)
        if badge:
            badge.setToolTip(s.help)
            hbox.addWidget(badge)
        hbox.addStretch(1)
        if s.tier in ("advanced", "danger"):
            lbl.setStyleSheet("color: rgba(0,0,0,0.55);")

        widget = self._render_control(s)
        if widget is not None:
            hbox.addWidget(widget)
        return row

    def _render_control(self, s):
        """依 s.type 產控件並登記到 _setting_widgets。"""
        t = s.type

        # 設定頁：note_spree（group）照 mockup 顯示為 switch（只控 enabled 子欄,
        # notes/max_spend 留 config 手調）。
        if s.key == "shop.post_target.note_spree":
            sw = SwitchButton()
            sw.setToolTip(s.help)
            self._setting_widgets[s.key] = (s, sw, "__note_spree__")
            return sw

        # 設定頁指定用摘要入口呈現的 list（mockup 的 .sel 摘要列，如音符購買優先序）
        if s.key in _SUMMARY_ENTRY_KEYS:
            found, value = config_io.get_by_path(self._summary_cfg(), s.key)
            entry = self._make_summary_entry(self._summarize_value(value if found else s.default),
                                             s.help)
            self._setting_widgets[s.key] = (s, None, None)  # 唯讀，不參與 save（值由 config 保留）
            return entry

        if t == "enum":
            combo = ComboBox()
            for zh, en in s.options:
                combo.addItem(zh, userData=en)
            combo.setToolTip(s.help)
            combo.setMinimumWidth(132)
            self._setting_widgets[s.key] = (s, combo, None)
            return combo

        if t == "int":
            spin = SpinBox()
            spin.setRange(0, 9999)
            spin.setToolTip(s.help)
            spin.setMinimumWidth(96)
            spin.setMaximumWidth(132)
            spin.setAlignment(Qt.AlignCenter)
            self._setting_widgets[s.key] = (s, spin, None)
            return spin

        if t == "float":
            spin = DoubleSpinBox()
            spin.setRange(0.0, 9999.0)
            spin.setSingleStep(0.1)
            spin.setDecimals(2)
            spin.setToolTip(s.help)
            spin.setMinimumWidth(96)
            spin.setMaximumWidth(132)
            spin.setAlignment(Qt.AlignCenter)
            self._setting_widgets[s.key] = (s, spin, None)
            return spin

        if t == "bool":
            sw = SwitchButton()
            sw.setToolTip(s.help)
            self._setting_widgets[s.key] = (s, sw, None)
            return sw

        if t == "list":
            edit = LineEdit()
            edit.setToolTip(s.help)
            edit.setMinimumWidth(180)
            edit.setClearButtonEnabled(True)
            self._setting_widgets[s.key] = (s, edit, None)
            return edit

        if t == "hotkey":
            edit = LineEdit()
            edit.setToolTip(s.help)
            edit.setMinimumWidth(120)
            edit.setMaximumWidth(160)
            edit.setPlaceholderText("例: ctrl+q")
            self._setting_widgets[s.key] = (s, edit, None)
            return edit

        if t == "int_toggle":
            holder = QWidget()
            hb = QHBoxLayout(holder)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(8)
            sw = SwitchButton()
            sw.setToolTip(s.help)
            spin = SpinBox()
            spin.setRange(1, 9999)
            spin.setToolTip(s.help)
            spin.setMinimumWidth(84)
            spin.setMaximumWidth(120)
            spin.setAlignment(Qt.AlignCenter)
            hb.addWidget(sw)
            hb.addWidget(spin)
            sw.checkedChanged.connect(lambda checked, sp=spin: sp.setEnabled(checked))
            self._setting_widgets[s.key] = (s, sw, spin)
            return holder

        if t == "editor":
            btn = PushButton(FluentIcon.EDIT, "開啟編輯器")
            btn.setToolTip(s.help)
            btn.clicked.connect(self._open_event_editor)
            self._setting_widgets[s.key] = (s, None, None)
            return btn

        # group / dict / dict-list / list-of-list / hotkey → 唯讀摘要
        found, value = config_io.get_by_path(self._summary_cfg(), s.key)
        summary = self._summarize_value(value if found else s.default)
        entry = self._make_summary_entry(summary, s.help)
        self._setting_widgets[s.key] = (s, None, None)
        return entry

    def _make_summary_entry(self, text, tooltip):
        """mockup 的 .sel 摘要列：唯讀摘要文字 + ＞（點開編輯留後續步驟，本步禁用）。"""
        holder = QFrame()
        holder.setStyleSheet(
            "QFrame{border:1px solid rgba(0,0,0,0.22);border-radius:6px;}"
        )
        hb = QHBoxLayout(holder)
        hb.setContentsMargins(10, 4, 8, 4)
        hb.setSpacing(6)
        lab = CaptionLabel(text)
        lab.setToolTip(tooltip)
        hb.addWidget(lab)
        chevron = IconWidget(FluentIcon.CHEVRON_RIGHT, holder)
        chevron.setFixedSize(12, 12)
        hb.addWidget(chevron)
        holder.setToolTip(tooltip)
        holder.setMaximumWidth(220)
        return holder

    def _summarize_upgrade_schedule(self):
        cfg = self._summary_cfg()
        _, tbv = config_io.get_by_path(cfg, "shop.upgrade.times_by_visit")
        if isinstance(tbv, dict) and tbv:
            parts = [f"{k}次→{v}" for k, v in list(tbv.items())[:3]]
            return " ".join(parts)
        return "（未設定）"

    # 摘要用：render 期間讀一次 config（之後以控件值為準）。
    _summary_cfg_cache = None

    def _summary_cfg(self):
        if self._summary_cfg_cache is None:
            try:
                if _CONFIG_PATH.exists():
                    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                        self._summary_cfg_cache = yaml.safe_load(f) or {}
                else:
                    self._summary_cfg_cache = {}
            except Exception:
                self._summary_cfg_cache = {}
        return self._summary_cfg_cache

    @staticmethod
    def _summarize_value(value):
        if value is None:
            return "（未設定）"
        if isinstance(value, dict):
            if not value:
                return "（未設定）"
            text = " ".join(f"{k}→{v}" for k, v in list(value.items())[:3])
        elif isinstance(value, list):
            if not value:
                return "（未設定）"
            flat = []
            for item in value[:4]:
                if isinstance(item, dict):
                    flat.append("/".join(str(v) for v in item.values()))
                elif isinstance(item, list):
                    flat.append("/".join(str(v) for v in item))
                else:
                    flat.append(str(item))
            text = "＞".join(flat)
            if len(value) > 4:
                text += "…"
        else:
            text = str(value)
        return text if len(text) <= 26 else text[:25] + "…"

    # ─── 監控頁（照 mockup 頁面2）───────────────────────────────────────────
    def _build_monitor_page(self):
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # 工具列：開始（灰，跑起來時）+ 停止（紅）。§0b：停止鈕在監控頁。
        bar = CardWidget(page)
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(14, 9, 14, 9)
        bar_l.addWidget(CaptionLabel("設定檔", bar))
        prof = ComboBox(bar)
        prof.addItem("預設")
        prof.setMinimumWidth(96)
        bar_l.addWidget(prof)
        bar_l.addStretch(1)
        self.btn_stop = PushButton(FluentIcon.CANCEL, "停止 (Ctrl+Q)", bar)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("PushButton{color:#A32D2D;}")
        bar_l.addWidget(self.btn_stop)
        outer.addWidget(bar)

        scroll = SingleDirectionScrollArea(orient=Qt.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.enableTransparentBackground()
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(12)

        # (1) LIVE 狀態列
        live = QFrame()
        live.setStyleSheet("QFrame{background:#F7F6F1;border-radius:12px;}")
        live_l = QHBoxLayout(live)
        live_l.setContentsMargins(16, 12, 16, 12)
        self.lbl_live = CaptionLabel("● LIVE")
        self.lbl_live.setStyleSheet("color:#A32D2D;font-weight:bold;")
        live_l.addWidget(self.lbl_live)
        state_box = QVBoxLayout()
        state_box.setSpacing(1)
        self.mon_state = StrongBodyLabel("待機中")
        self.mon_state_detail = CaptionLabel("尚未啟動")
        self.mon_state_detail.setStyleSheet("color:rgba(0,0,0,0.45);")
        state_box.addWidget(self.mon_state)
        state_box.addWidget(self.mon_state_detail)
        live_l.addLayout(state_box)
        live_l.addStretch(1)
        conf_box = QVBoxLayout()
        conf_box.setSpacing(3)
        self.mon_conf_label = CaptionLabel("辨識信心 —")
        self.mon_conf_bar = ProgressBar()
        self.mon_conf_bar.setFixedWidth(120)
        self.mon_conf_bar.setValue(0)
        conf_box.addWidget(self.mon_conf_label)
        conf_box.addWidget(self.mon_conf_bar)
        live_l.addLayout(conf_box)
        layout.addWidget(live)

        # (2) 進度 metrics（5 格）
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(10)
        self.metric_visit = self._make_metric("商店造訪", "—")
        self.metric_cards = self._make_metric("卡片總等級", "—")
        self.metric_money = self._make_metric("金錢", "—")
        self.metric_round = self._make_metric("本輪", "—")
        self.metric_target = self._make_metric("保留目標", "—")
        for i, m in enumerate((self.metric_visit, self.metric_cards, self.metric_money,
                               self.metric_round, self.metric_target)):
            metrics.addWidget(m[0], 0, i)
            metrics.setColumnStretch(i, 1)
        layout.addLayout(metrics)

        # (3) 兩欄：流程進度 / （音符進度 + 設定總覽）
        two = QGridLayout()
        two.setHorizontalSpacing(12)
        two.addWidget(self._build_flow_card(), 0, 0)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_notes_card())
        right.addWidget(self._build_overview_card())
        right.addStretch(1)
        rw = QWidget()
        rw.setLayout(right)
        two.addWidget(rw, 0, 1)
        two.setColumnStretch(0, 1)
        two.setColumnStretch(1, 1)
        layout.addLayout(two)

        # log console（保留，放監控頁底部）
        self.log_edit = TextEdit(content)
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("系統準備就緒...")
        fc = QFont("Consolas")
        fc.setPixelSize(13)
        self.log_edit.setFont(fc)
        self.log_edit.setMinimumHeight(120)
        layout.addWidget(self.log_edit, 1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return page

    def _make_metric(self, title, value):
        card = QFrame()
        card.setStyleSheet("QFrame{background:#F7F6F1;border-radius:8px;}")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(2)
        t = CaptionLabel(title)
        t.setStyleSheet("color:rgba(0,0,0,0.55);")
        val = StrongBodyLabel(value)
        val.setStyleSheet("font-size:20px;")
        v.addWidget(t)
        v.addWidget(val)
        return (card, val)

    def _build_flow_card(self):
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 13, 18, 16)
        v.setSpacing(3)
        v.addWidget(CaptionLabel("流程進度"))
        for key, label in _FLOW_STEPS:
            row = QLabel("○  " + label)
            row.setStyleSheet("color:rgba(0,0,0,0.45);font-size:13px;padding:5px 8px;")
            self._flow_widgets[key] = row
            v.addWidget(row)
        return card

    def _build_notes_card(self):
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 12, 18, 14)
        v.setSpacing(8)
        v.addWidget(CaptionLabel("🎵 協奏音符進度"))
        self.notes_box = QVBoxLayout()
        self.notes_box.setSpacing(6)
        self._notes_placeholder = CaptionLabel("（執行後顯示各音符進度）")
        self._notes_placeholder.setStyleSheet("color:rgba(0,0,0,0.40);")
        self.notes_box.addWidget(self._notes_placeholder)
        v.addLayout(self.notes_box)
        return card

    def _build_overview_card(self):
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 12, 18, 14)
        v.setSpacing(6)
        v.addWidget(CaptionLabel("✔ 當前生效設定總覽"))
        for key, label in _SUMMARY_OVERVIEW_KEYS:
            row = QHBoxLayout()
            row.addWidget(CaptionLabel(label))
            row.addStretch(1)
            val = CaptionLabel("—")
            val.setStyleSheet("color:rgba(0,0,0,0.85);")
            self._overview_labels[key] = val
            row.addWidget(val)
            v.addLayout(row)
        return card

    def _refresh_overview(self):
        """讀設定頁控件當前值，更新監控頁「設定總覽」（enum 顯示中文）。"""
        for key, _label in _SUMMARY_OVERVIEW_KEYS:
            lab = self._overview_labels.get(key)
            if lab is None:
                continue
            entry = self._setting_widgets.get(key)
            if not entry:
                continue
            s, primary, _sec = entry
            if s.type == "enum" and primary is not None:
                lab.setText(config_io.enum_to_display(s, primary.currentData()))
            elif primary is not None and hasattr(primary, "value"):
                lab.setText(str(primary.value()))

    # ─── 進階頁（照 mockup 頁面3：速度/感知/硬體 + 安全鎖）────────────────────
    def _build_advanced_page(self):
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # 風險勾選框（未勾 → danger 旋鈕鎖定；每次重開 GUI 重置為未勾）
        risk = QFrame()
        risk.setStyleSheet("QFrame{background:#FAEEDA;border-radius:8px;}")
        rl = QHBoxLayout(risk)
        rl.setContentsMargins(14, 10, 14, 10)
        self.chk_risk = CheckBox(
            "我了解調整以下「危險」項可能讓 bot 失常（勾選後才解鎖鎖定的旋鈕）")
        self.chk_risk.setStyleSheet("color:#854F0B;")
        self.chk_risk.stateChanged.connect(self._on_risk_toggle)
        rl.addWidget(self.chk_risk)
        rl.addStretch(1)
        outer.addWidget(risk)

        card = CardWidget(page)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 8, 18, 14)
        cl.setSpacing(0)
        cl.addWidget(StrongBodyLabel("速度 / 感知 / 硬體", card))
        self._danger_keys = []
        for key in _ADVANCED_PAGE_LAYOUT:
            cl.addWidget(self._build_setting_row(key))
            s = by_key(key)
            if s is not None and s.tier == "danger":
                self._danger_keys.append(key)
        # 緊急中止快捷鍵（步7,使用者拍板放進階頁底部;改後重啟 GUI 生效）
        cl.addWidget(self._build_setting_row("bot.hotkey_stop"))
        outer.addWidget(card)
        outer.addStretch(1)

        self._set_danger_enabled(False)  # 初始鎖定
        return page

    def _on_risk_toggle(self, state):
        self._set_danger_enabled(bool(state))

    def _set_danger_enabled(self, enabled):
        for key in getattr(self, "_danger_keys", []):
            entry = self._setting_widgets.get(key)
            if entry and entry[1] is not None:
                entry[1].setEnabled(enabled)

    # ─── help 介面（步6：schema 衍生的所有設定說明）───────────────────────────
    def _show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("設定說明")
        dlg.resize(560, 620)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(16, 16, 16, 16)
        scroll = SingleDirectionScrollArea(orient=Qt.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setSpacing(4)
        _badge = {"test": "［測試版］", "advanced": "［進階］", "danger": "［危險］"}
        for mod in ("選卡", "商店", "事件", "結算", "執行", "進階"):
            items = by_module(mod)
            if not items:
                continue
            head = StrongBodyLabel(mod)
            head.setStyleSheet("margin-top:8px;")
            cl.addWidget(head)
            for s in items:
                row = QLabel(f"• {s.label}{_badge.get(s.tier, '')}：{s.help}")
                row.setWordWrap(True)
                row.setStyleSheet(
                    "color:rgba(0,0,0,0.72);font-size:12px;padding:2px 0 2px 10px;")
                cl.addWidget(row)
        cl.addStretch(1)
        scroll.setWidget(content)
        v.addWidget(scroll)
        dlg.exec_()

    # ─── 事件編輯器入口（步8,接 §3.5 event_rules 後端）──────────────────────
    def _open_event_editor(self):
        from gui.event_editor import EventEditorDialog
        EventEditorDialog(self).exec_()

    # ─── 設定檔管理（步5：多 profile;存 configs/<名稱>.yaml）──────────────────
    def _refresh_profiles(self):
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        names = profiles.list_profiles()
        if names:
            for n in names:
                self.combo_profile.addItem(n)
        else:
            self.combo_profile.addItem("（尚無設定檔）")
        self.combo_profile.blockSignals(False)

    def _on_profile_save_as(self):
        name, ok = QInputDialog.getText(self, "儲存設定檔", "輸入設定檔名稱:")
        if not ok or not name.strip():
            return
        if not profiles.is_valid_name(name):
            self.append_log(f"[UI] 設定檔名稱非法（限中英數/-/空白,≤40 字）: {name}")
            return
        self.save_config()  # 先把 GUI 當前值寫入 config.yaml
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            profiles.save_profile(name.strip(), cfg)
            self._refresh_profiles()
            self.append_log(f"[UI] 已儲存設定檔「{name.strip()}」")
        except Exception as e:
            self.append_log(f"[UI] 儲存設定檔失敗: {e}")

    def _on_profile_load(self):
        name = self.combo_profile.currentText().strip()
        if not name or "尚無" in name:
            return
        try:
            profiles.apply_profile_to_config(name)
            self._summary_cfg_cache = None
            self.load_config()
            self._refresh_overview()
            self.append_log(f"[UI] 已載入設定檔「{name}」（摘要欄位需重啟 GUI 才更新顯示）")
        except Exception as e:
            self.append_log(f"[UI] 載入設定檔失敗: {e}")

    def _on_profile_export(self):
        name = self.combo_profile.currentText().strip()
        if not name or "尚無" in name:
            return
        path, _ = QFileDialog.getSaveFileName(self, "導出設定檔", f"{name}.yaml", "YAML (*.yaml)")
        if not path:
            return
        try:
            profiles.export_profile(name, path)
            self.append_log(f"[UI] 已導出設定檔「{name}」→ {path}")
        except Exception as e:
            self.append_log(f"[UI] 導出失敗: {e}")

    def _on_profile_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "匯入設定檔", "", "YAML (*.yaml)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "匯入設定檔", "為匯入的設定檔命名:")
        if not ok or not profiles.is_valid_name(name):
            self.append_log("[UI] 匯入取消或名稱非法")
            return
        try:
            profiles.import_profile(path, name.strip())
            self._refresh_profiles()
            self.append_log(f"[UI] 已匯入設定檔「{name.strip()}」")
        except Exception as e:
            self.append_log(f"[UI] 匯入失敗: {e}")

    # ─── signal 綁定 ────────────────────────────────────────────────────────
    def bind_signals(self):
        self.btn_start.clicked.connect(self.start_bot)
        self.btn_stop.clicked.connect(self.stop_bot)
        self.btn_help.clicked.connect(self._show_help)
        signals.log_msg.connect(self.append_log)
        signals.status_update.connect(self.update_status)
        signals.finished.connect(self.on_worker_finished)
        signals.error.connect(self.on_worker_error)

    # ─── load / save（schema 驅動，保留未知 key；攻略解析/潛能清單已分開不在此）──
    def load_config(self):
        try:
            cfg = {}
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            for key, (s, primary, secondary) in self._setting_widgets.items():
                if primary is None:
                    continue  # 唯讀摘要型
                found, value = config_io.get_by_path(cfg, key)
                if not found:
                    value = s.default
                self._set_widget_value(s, primary, secondary, value)
            run_cfg = cfg.get("run", {}) or {}
            self.spin_runs.setValue(int(run_cfg.get("max_runs", 1)))
        except Exception as e:
            self.append_log(f"[UI] 讀取 config.yaml 失敗: {e}")

    def _set_widget_value(self, s, primary, secondary, value):
        if secondary == "__note_spree__":
            primary.setChecked(bool(value.get("enabled")) if isinstance(value, dict) else False)
            return
        t = s.type
        try:
            if t == "enum":
                idx = primary.findData(value)
                if idx < 0:
                    idx = primary.findData(s.default)
                primary.setCurrentIndex(idx if idx >= 0 else 0)
            elif t == "int":
                primary.setValue(int(value))
            elif t == "float":
                primary.setValue(float(value))
            elif t == "bool":
                primary.setChecked(bool(value))
            elif t == "list":
                primary.setText(config_io.format_list_text(value))
            elif t == "hotkey":
                primary.setText(str(value or ""))
            elif t == "int_toggle":
                try:
                    ival = int(value)
                except (TypeError, ValueError):
                    ival = 0
                if ival > 0:
                    primary.setChecked(True)
                    secondary.setValue(ival)
                    secondary.setEnabled(True)
                else:
                    primary.setChecked(False)
                    secondary.setValue(max(1, secondary.value()))
                    secondary.setEnabled(False)
        except Exception:
            pass

    def _read_widget_value(self, s, primary, secondary):
        t = s.type
        if t == "enum":
            data = primary.currentData()
            return data if data is not None else s.default
        if t in ("int", "float"):
            return primary.value()
        if t == "bool":
            return primary.isChecked()
        if t == "list":
            return primary.text()
        if t == "hotkey":
            return primary.text().strip()
        if t == "int_toggle":
            return secondary.value() if primary.isChecked() else 0
        return None

    def save_config(self):
        try:
            cfg = {}
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            for key, (s, primary, secondary) in self._setting_widgets.items():
                if primary is None:
                    continue  # 唯讀摘要型（值由 config 原樣保留）
                if secondary == "__note_spree__":
                    config_io.set_by_path(
                        cfg, "shop.post_target.note_spree.enabled", bool(primary.isChecked()))
                    continue
                raw = self._read_widget_value(s, primary, secondary)
                if raw is None:
                    continue
                coerced = config_io.coerce_value_for_config(s, raw)
                config_io.set_by_path(cfg, key, coerced)
            config_io.set_by_path(cfg, "run.max_runs", int(self.spin_runs.value()))
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self._refresh_overview()
            self.append_log("[UI] ✅ 設定已成功儲存至 config.yaml！")
        except Exception as e:
            self.append_log(f"[UI] 儲存設定失敗: {e}")

    # ─── bot 啟停（開始鈕設定頁 / 停止鈕監控頁，互斥）──────────────────────
    def start_bot(self):
        if self.bot_worker and self.bot_worker.isRunning():
            self.append_log("[UI] Bot worker is still running.")
            return
        if self.bot_worker and not self.bot_worker.isRunning():
            self.bot_worker = None

        self.save_config()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.mon_state.setText("啟動中…")
        self.stack.setCurrentWidget(self.monitor_page)
        try:
            window_name = "StellaSora"
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                window_name = cfg.get("window", {}).get("name", window_name)
            WindowManager(window_name=window_name).focus_window()
            self.append_log(f"[UI] 已將遊戲視窗「{window_name}」切到前景。")
        except Exception as e:
            self.append_log(f"[UI] 無法將遊戲視窗切到前景: {e}")

        self.bot_worker = BotWorker()
        self.bot_worker.start()

    def stop_bot(self):
        if not self.bot_worker or not self.bot_worker.isRunning():
            self.on_worker_finished()
            return
        self.btn_stop.setEnabled(False)
        self.mon_state.setText("停止中…")
        self.bot_worker.stop()
        self.append_log("[UI] Stop signal sent; waiting for worker shutdown.")

    # ─── 監控顯示 ───────────────────────────────────────────────────────────
    def append_log(self, msg):
        self.log_edit.append(msg)
        sb = self.log_edit.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def update_status(self, stats):
        state = str(stats.get('state', 'Unknown'))
        zh = {
            "lobby": "大廳", "formation": "編隊", "prepare": "準備", "battle": "快速戰鬥",
            "potential": "選潛能", "note": "獲得音符", "event": "隨機事件",
            "shop": "商店", "result": "結算",
        }
        flow = _STATE_TO_FLOW.get(state)
        self.mon_state.setText(zh.get(flow, state) if flow else state)
        self.mon_state_detail.setText(state)

        # 辨識信心條接真值（缺漏/None → 0 與 "—"）
        conf = stats.get('confidence')
        if conf is None:
            self.mon_conf_bar.setValue(0)
            self.mon_conf_label.setText("辨識信心 —")
        else:
            pct = int(round(float(conf) * 100))
            pct = max(0, min(100, pct))
            self.mon_conf_bar.setValue(pct)
            self.mon_conf_label.setText(f"辨識信心 {float(conf) * 100:.0f}%")

        # 商店造訪接真實計數（shop_visits），保留 floor 當後備
        visits = stats.get('shop_visits', stats.get('floor', 0))
        self.metric_visit[1].setText(f"第 {visits} 次")
        counter = stats.get('card_counter') or {}
        if counter.get('enabled'):
            self.metric_cards[1].setText(
                f"{counter.get('current_total', 0)} / {counter.get('target_total', 0)}")
        else:
            self.metric_cards[1].setText("關閉")
        self.metric_money[1].setText(str(stats.get('money', 0)))
        self.metric_round[1].setText(f"{stats.get('runs', 0)} / {stats.get('max_runs', 0)}")
        self.metric_target[1].setText(self._target_summary())

        # FSM 聚光燈：current 高亮，其餘灰
        for key, w in self._flow_widgets.items():
            label = dict(_FLOW_STEPS)[key]
            if key == flow:
                w.setText("▶  " + label)
                w.setStyleSheet(
                    "color:#3C3489;font-size:14px;font-weight:bold;"
                    "background:#EEEDFE;border-radius:6px;padding:6px 8px;")
            else:
                w.setText("○  " + label)
                w.setStyleSheet("color:rgba(0,0,0,0.45);font-size:13px;padding:5px 8px;")

        # 協奏音符進度（步9：接 workers emit 的 current/target notes）
        notes = stats.get("notes") or {}
        self._update_notes(notes.get("current") or {}, notes.get("target") or {})

    def _update_notes(self, current, target):
        """依 current/target 音符 dict 動態重建監控頁的音符進度條。"""
        while self.notes_box.count():
            item = self.notes_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not isinstance(target, dict) or not target:
            ph = CaptionLabel("（執行後顯示各音符進度）")
            ph.setStyleSheet("color:rgba(0,0,0,0.40);")
            self.notes_box.addWidget(ph)
            return
        cur = current if isinstance(current, dict) else {}
        for name, tgt in target.items():
            try:
                tgt_i = int(tgt or 0)
            except (TypeError, ValueError):
                tgt_i = 0
            try:
                cur_i = int(cur.get(name, 0) or 0)
            except (TypeError, ValueError):
                cur_i = 0
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(8)
            nm = CaptionLabel(str(name))
            nm.setFixedWidth(46)
            hb.addWidget(nm)
            bar = ProgressBar()
            bar.setValue(int(min(100, cur_i * 100 / tgt_i)) if tgt_i > 0 else 0)
            hb.addWidget(bar, 1)
            val = CaptionLabel(f"{cur_i}/{tgt_i}")
            val.setFixedWidth(50)
            hb.addWidget(val)
            self.notes_box.addWidget(row)

    def _target_summary(self):
        entry = self._setting_widgets.get("result.rating_threshold")
        if entry and entry[1] is not None:
            return f"評分 ≥ {entry[1].value()}"
        return "—"

    def on_worker_finished(self):
        if self.bot_worker and not self.bot_worker.isRunning():
            self.bot_worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.mon_state.setText("待機")
        self.append_log("=== Background worker finished ===\n")
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_worker_error(self, err_msg):
        self.mon_state.setText("錯誤")
        self.append_log(f"[UI] Worker error: {err_msg}")
