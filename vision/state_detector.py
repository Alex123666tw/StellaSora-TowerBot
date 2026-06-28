"""
畫面狀態辨識器 (vision/state_detector.py) — Phase 1.2 StateDetector v2

透過 OCR 辨識截圖中的關鍵文字，判定目前遊戲所處的狀態。
所有提示字 / 顏色錨點資料一律來自 vision/signatures.py（Phase 1.1
單一事實來源）；本模組只負責 OCR 前處理、簽名走訪與結果包裝。

辨識策略：
  - signatures.SIGNATURES 依 priority 走訪（數字小者先判，彈窗類 > 全頁類）。
  - detect() 一律回傳 DetectionResult(state, confidence, evidence)，
    呼叫端不再需要處理雙介面。
  - v2（預設）：所有 signature 都低於 min_score → STATE_UNKNOWN（解 R2：
    辨識失敗不再被「維持原狀態」掩蓋）。
  - v1（config vision.detector: v1 可切回對照）：未命中維持原狀態，
    confidence/evidence 給相容預設值（0.0 / 空）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from vision import signatures

logger = logging.getLogger(__name__)

# 無法辨識畫面時的狀態（Phase 1.2；core/bot.py 主迴圈對此狀態
# 不執行任何 handler、不點擊）。
STATE_UNKNOWN = "STATE_UNKNOWN"

DETECTOR_MODE_V1 = "v1"
DETECTOR_MODE_V2 = "v2"


@dataclass(frozen=True)
class DetectionResult:
    """detect() 的回傳值。

    Attributes:
        state:      判定的狀態字串（v2 無命中時為 STATE_UNKNOWN，
                    v1 無命中時為呼叫端傳入的 current_state）。
        confidence: 命中 signature 的得分（無命中 = 0.0）。
        evidence:   命中的關鍵字 / 錨點清單（JSON 可序列化字串，
                    可直接寫入 state_trace / failure bundle）。
    """

    state: str
    confidence: float = 0.0
    evidence: tuple[str, ...] = field(default_factory=tuple)


class StateDetector:
    """
    使用 OCR 辨識畫面文字，比對 vision/signatures.py 的簽名表，判定當前遊戲狀態。

    Args:
        ocr_engine: OcrEngine 實例（vision/ocr_engine.py）。
        roi:        辨識範圍，格式 (x, y, w, h)，None 代表全畫面。
        mode:       "v2"（預設，未命中 → STATE_UNKNOWN）或
                    "v1"（未命中維持原狀態，行為對照用）。
    """

    def __init__(self, ocr_engine, roi: tuple = None, mode: str = DETECTOR_MODE_V2):
        self._ocr = ocr_engine
        self._roi = roi  # 可限縮辨識區域（如上半部 UI 欄）提升效率
        normalized_mode = str(mode or DETECTOR_MODE_V2).strip().lower()
        if normalized_mode not in (DETECTOR_MODE_V1, DETECTOR_MODE_V2):
            logger.warning(
                f"[StateDetector] 未知偵測模式「{mode}」，改用預設 {DETECTOR_MODE_V2}。"
            )
            normalized_mode = DETECTOR_MODE_V2
        self.mode = normalized_mode

    def _no_hit_result(self, current_state: str, evidence: tuple[str, ...] = ()) -> DetectionResult:
        if self.mode == DETECTOR_MODE_V1:
            return DetectionResult(state=current_state, confidence=0.0, evidence=evidence)
        return DetectionResult(state=STATE_UNKNOWN, confidence=0.0, evidence=evidence)

    def detect(self, frame: np.ndarray, current_state: str) -> DetectionResult:
        """
        對截圖執行 OCR，依簽名表回傳判定結果。

        Args:
            frame:         BGR 格式的截圖陣列。
            current_state: 當前狀態字串（v1 模式未命中時保持不變）。

        Returns:
            DetectionResult: state / confidence / evidence。
        """
        # 若指定 ROI，裁切後辨識（提升速度）
        if self._roi:
            x, y, w, h = self._roi
            region = frame[y:y+h, x:x+w]
        else:
            region = frame

        # [效能優化] 狀態判定只需知道「字串是否存在」，不倚賴精確座標
        # 將圖片縮小 50%，可讓 PyTorch 神經網路的參數量與 RAM 佔用大幅下降，提升 3~4 倍辨識速度
        try:
            import cv2
            h, w = region.shape[:2]
            # 限制最高解析度在 960x540 左右，這是 EasyOCR 讀取大標題的最佳效能甜區
            scale = 0.5 if w > 1280 else 1.0
            if scale != 1.0:
                region = cv2.resize(region, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        except Exception as e:
            logger.debug(f"[StateDetector] 縮放圖片失敗: {e}")

        # OCR 辨識
        try:
            raw_texts = self._ocr.read_text_simple(region)
        except Exception as e:
            logger.warning(f"[StateDetector] OCR 辨識失敗: {e}")
            # v2：OCR 失敗不可掩蓋為「維持原狀態」，視為無法辨識；
            # v1：沿用舊行為（保持當前狀態）。
            return self._no_hit_result(current_state, evidence=("ocr_exception",))

        logger.debug(f"[StateDetector] OCR 全文: {' '.join(raw_texts)[:120]}")

        # 簽名走訪（priority 小者先判）；顏色錨點以原始 frame 判定
        # （v1 的青色按鈕檢查同樣使用完整 frame，而非 ROI/縮圖）。
        state, score, signature = signatures.classify(raw_texts, frame=frame)
        if state is None:
            if self.mode == DETECTOR_MODE_V1:
                logger.debug("[StateDetector] 無 signature 命中，保持當前狀態（v1）。")
            else:
                logger.info("[StateDetector] 無 signature 命中 → STATE_UNKNOWN。")
            return self._no_hit_result(current_state)

        evidence = (f"signature:{getattr(signature, 'name', '?')}",) + signatures.signature_evidence(
            signature, raw_texts, frame=frame
        )

        if state != current_state:
            logger.info(
                f"[StateDetector] signature「{getattr(signature, 'name', '?')}」命中"
                f"（score={score:.2f}）→ 判定狀態 {state}"
            )
        return DetectionResult(state=state, confidence=float(score), evidence=evidence)
