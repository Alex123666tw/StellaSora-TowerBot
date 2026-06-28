# gui/event_editor.py — 事件規則編輯器對話框（GUI_DESIGN_SPEC §10 步8 / §4）。
#   列出/新增/編輯/刪除/上下移 data/event_rules.yaml 的 overrides,儲存回檔。
#   接 §3.5 後端（states._load_event_rules 讀同一檔）。

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem
)
from qfluentwidgets import (
    PushButton, PrimaryPushButton, LineEdit, CaptionLabel, StrongBodyLabel, FluentIcon
)

from gui import event_rules_io


def _parse_csv(text: str) -> list:
    """逗號分隔（中英文逗號）→ 去空白、濾空的 list。"""
    return [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]


class _OverrideEditDialog(QDialog):
    """編輯單條 override（id / match_any / pick_any / note）。"""

    def __init__(self, parent=None, override=None):
        super().__init__(parent)
        self.setWindowTitle("編輯規則")
        self.resize(440, 300)
        o = event_rules_io.normalize_override(override or {})

        v = QVBoxLayout(self)
        v.setSpacing(6)

        v.addWidget(CaptionLabel("規則名稱（id）"))
        self.edit_id = LineEdit()
        self.edit_id.setText(o["id"])
        v.addWidget(self.edit_id)

        v.addWidget(CaptionLabel("比對畫面文字（含任一即命中,逗號分隔）"))
        self.edit_match = LineEdit()
        self.edit_match.setText(", ".join(o["match_any"]))
        v.addWidget(self.edit_match)

        v.addWidget(CaptionLabel("要選的選項（含任一字,逗號分隔）"))
        self.edit_pick = LineEdit()
        self.edit_pick.setText(", ".join(o["pick_any"]))
        v.addWidget(self.edit_pick)

        v.addWidget(CaptionLabel("說明（選填）"))
        self.edit_note = LineEdit()
        self.edit_note.setText(o["note"])
        v.addWidget(self.edit_note)

        v.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = PushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = PrimaryPushButton("確定")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def get_override(self) -> dict:
        return event_rules_io.normalize_override({
            "id": self.edit_id.text(),
            "match_any": _parse_csv(self.edit_match.text()),
            "pick_any": _parse_csv(self.edit_pick.text()),
            "note": self.edit_note.text(),
        })


class EventEditorDialog(QDialog):
    """事件規則列表編輯器（mockup 事件編輯器對話框）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("編輯事件規則")
        self.resize(580, 500)
        self._overrides = event_rules_io.load_overrides()

        v = QVBoxLayout(self)
        v.addWidget(StrongBodyLabel("編輯事件規則"))
        v.addWidget(CaptionLabel(
            "命中比對字 → 直接選指定選項;沒命中 → 走預設策略評分。"
            "遊戲更新出新事件時自己加一條,bot 就認得。順序 = 優先序。"))

        self.list_widget = QListWidget()
        v.addWidget(self.list_widget, 1)

        ops = QHBoxLayout()
        btn_add = PushButton(FluentIcon.ADD, "新增規則")
        btn_add.clicked.connect(self._on_add)
        btn_edit = PushButton("編輯")
        btn_edit.clicked.connect(self._on_edit)
        btn_del = PushButton("刪除")
        btn_del.clicked.connect(self._on_delete)
        btn_up = PushButton("上移")
        btn_up.clicked.connect(lambda: self._move(-1))
        btn_down = PushButton("下移")
        btn_down.clicked.connect(lambda: self._move(1))
        for b in (btn_add, btn_edit, btn_del, btn_up, btn_down):
            ops.addWidget(b)
        ops.addStretch(1)
        v.addLayout(ops)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = PushButton("取消")
        cancel.clicked.connect(self.reject)
        save = PrimaryPushButton("儲存")
        save.clicked.connect(self._on_save)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        v.addLayout(bottom)

        self._refresh_list()

    def _refresh_list(self):
        self.list_widget.clear()
        for o in self._overrides:
            match = " / ".join(o["match_any"])
            pick = " / ".join(o["pick_any"])
            text = f"【{o['id']}】問題:{match}  →  選:{pick}"
            if o["note"]:
                text += f"  · {o['note']}"
            self.list_widget.addItem(QListWidgetItem(text))

    def _on_add(self):
        dlg = _OverrideEditDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            o = dlg.get_override()
            if event_rules_io.is_valid_override(o):
                self._overrides.append(o)
                self._refresh_list()

    def _on_edit(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            return
        dlg = _OverrideEditDialog(self, self._overrides[idx])
        if dlg.exec_() == QDialog.Accepted:
            o = dlg.get_override()
            if event_rules_io.is_valid_override(o):
                self._overrides[idx] = o
                self._refresh_list()

    def _on_delete(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            return
        del self._overrides[idx]
        self._refresh_list()

    def _move(self, delta):
        idx = self.list_widget.currentRow()
        j = idx + delta
        if idx < 0 or j < 0 or j >= len(self._overrides):
            return
        self._overrides[idx], self._overrides[j] = self._overrides[j], self._overrides[idx]
        self._refresh_list()
        self.list_widget.setCurrentRow(j)

    def _on_save(self):
        event_rules_io.save_overrides(self._overrides)
        self.accept()
