import logging
import traceback
import sys
from pathlib import Path
from PyQt5.QtCore import QThread
import numpy as np
import cv2
import json

from gui.signals import signals
from core.bot import StateMachine
from tools.parse_guide_image import (
    _PRIORITY_LIST, preprocess, _run_ocr, 
    classify_blocks, spatial_pair_1_to_n, write_to_config
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEBUG_LOG_PATH = _PROJECT_ROOT / "debug_bot.log"


# === 攔截原有 logging 到 UI ===
class GuiLogHandler(logging.Handler):
    """將 logging 模組的日誌重定向發送給前端 UI"""
    def emit(self, record):
        try:
            msg = self.format(record)
            signals.log_msg.emit(msg)
            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


# === 背景自動探索執行緒 ===
class BotWorker(QThread):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = None
        self.gui_handler = None
        self._stop_requested = False

    def run(self):
        self._stop_requested = False
        try:
            with open(_DEBUG_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("=== BotWorker Thread Started ===\n")

            root_logger = logging.getLogger()
            root_logger.setLevel(logging.INFO)
            self.gui_handler = GuiLogHandler()
            self.gui_handler.setLevel(logging.INFO)
            self.gui_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
            root_logger.addHandler(self.gui_handler)

            signals.log_msg.emit("=== StellaSora bot starting ===")

            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(">>> Entering StateMachine() init\n")

            self.runner = StateMachine()
            if self._stop_requested and self.runner and self.runner.ctx:
                self.runner.ctx.running = False

            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write("<<< Exited StateMachine() init successfully\n")

            original_detect = self.runner._detect_state

            def patched_detect():
                result = original_detect()                 # 先偵測,拿本拍最新 state + confidence
                ctx = self.runner.ctx
                status = {
                    "floor": ctx.current_floor,
                    "shop_visits": ctx.shop_visit_count,   # 新增:真實商店造訪計數
                    "runs": ctx.run_count,
                    "max_runs": ctx.max_runs,
                    "success": ctx.success_count,
                    "state": result.state,                 # 改:用最新偵測結果,而非上一拍 ctx.current_state
                    "confidence": result.confidence,       # 新增:辨識信心(0.0~1.0)
                    "money": ctx.current_money,
                    "card_counter": {
                        "enabled": ctx.card_counter_enabled,
                        "initial_total": ctx.card_counter_initial_total,
                        "current_total": ctx.card_counter_current_total,
                        "target_total": ctx.card_counter_target_total,
                    },
                    "notes": {
                        "current": dict(getattr(ctx, "current_notes", {}) or {}),
                        "target": dict(getattr(ctx, "target_notes", {}) or {}),
                    },
                }
                signals.status_update.emit(status)
                return result

            self.runner._detect_state = patched_detect

            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(">>> Calling self.runner.run()\n")

            self.runner.run()

        except Exception as e:
            err_msg = traceback.format_exc()
            signals.error.emit(str(e))
            signals.log_msg.emit(f"[ERROR] Fatal worker exception:\n{err_msg}")
            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[FATAL ERROR] {err_msg}\n")
        finally:
            if self.gui_handler:
                logging.getLogger().removeHandler(self.gui_handler)
            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write("=== Bot Worker Thread Exit ===\n")
            self._stop_requested = False
            signals.finished.emit()

    def stop(self):
        """Request a cooperative stop without blocking the UI thread."""
        if self._stop_requested:
            signals.log_msg.emit(">> [UI] Stop already requested; waiting for worker to exit.")
            return
        self._stop_requested = True
        signals.log_msg.emit(">> [UI] Stop requested; waiting for worker to exit cleanly.")
        if self.runner and self.runner.ctx:
            self.runner.ctx.running = False
        self.requestInterruption()


class ParserWorker(QThread):
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.image_path = image_path

    def run(self):
        try:
            signals.log_msg.emit(f"[Parser] 準備解析攻略圖: {self.image_path}")
            # 使用 imdecode 支援 Windows 下的中文/特殊字元路徑
            img_array = np.fromfile(self.image_path, dtype=np.uint8)
            raw_img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            
            if raw_img is None:
                raise ValueError("無法讀取圖片，請檢查檔案是否存在或損毀。")
                
            signals.log_msg.emit("[Parser] 正在進行前處理強化與 OCR...")
            processed, actual_scale = preprocess(raw_img, scale=2.0)
            blocks = _run_ocr(processed)
            
            # 座標縮回原比例
            for b in blocks:
                b.x, b.y, b.w, b.h = int(b.x / actual_scale), int(b.y / actual_scale), int(b.w / actual_scale), int(b.h / actual_scale)
                
            signals.log_msg.emit(f"[Parser] 共識別到 {len(blocks)} 個文字區塊。進行分類與模糊配對...")
            with open(_PRIORITY_LIST, "r", encoding="utf-8") as f:
                potentials_db = json.load(f).get("potentials", [])
                
            blocks = classify_blocks(blocks, potentials_db)
            parsed = spatial_pair_1_to_n(blocks, img_height=raw_img.shape[0], y_offset_ratio=0.30, potentials_db=potentials_db, raw_img=raw_img)
            
            if not parsed.get("required") and not parsed.get("backup") and not parsed.get("guaranteed"):
                signals.log_msg.emit("[Parser] 警告：這張圖片沒有偵測到任何關鍵標籤（必拿/備選/保底）。")
                signals.log_msg.emit("[Parser] 請檢查圖片，或是使用終端機執行 `python tools/parse_guide_image.py` 來進行手動校驗。")
                signals.error.emit("解析失敗：找不到任何圖中潛能或標籤")
            else:
                write_to_config(parsed)
                signals.log_msg.emit("\n[Parser] ✅ 已經成功將潛能目標解析完畢！")
                signals.log_msg.emit(f"   ➤ 必拿: {parsed.get('required', [])}")
                signals.log_msg.emit(f"   ➤ 備選: {parsed.get('backup', [])}")
                signals.log_msg.emit(f"   ➤ 保底: {parsed.get('guaranteed', [])}\n")
                signals.parser_result.emit(parsed)
                
        except Exception as e:
            err_msg = traceback.format_exc()
            signals.error.emit(str(e))
            signals.log_msg.emit(f"[Parser] 解析發生錯誤:\n{err_msg}")
        finally:
            signals.finished.emit()
