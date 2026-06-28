import sys
from PyQt5.QtCore import QObject, pyqtSignal

class WorkerSignals(QObject):
    """定義背景 Worker 到前端 UI 溝通用的 Qt Signals"""
    log_msg = pyqtSignal(str)           # 用於傳遞 Log 訊息
    status_update = pyqtSignal(dict)    # 用於傳遞狀態更新 (包含 floor, runs, money 等)
    parser_result = pyqtSignal(dict)    # 用於傳遞 Parser 的解析結果
    finished = pyqtSignal()             # 執行緒結束信號
    error = pyqtSignal(str)             # 執行錯誤信號

# 全域單例
signals = WorkerSignals()
