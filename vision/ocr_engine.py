"""
OCR 核心引擎模組 (OcrEngine)
封裝 EasyOCR 引擎，針對特定 ROI (Region of Interest) 進行文字辨識。
"""
import cv2
import numpy as np
import logging

class OcrEngine:
    """
    提供基於 EasyOCR 的文字辨識功能封裝。
    
    支援中/英文辨識，以及單向限縮辨識範圍 (ROI)。
    """
    def __init__(self, languages: list[str] = None, gpu: bool = True):
        """
        Args:
            languages: 要載入的模型語系，預設為繁體中文 ('ch_tra') 與英文 ('en')。
            gpu: 是否啟用 GPU 加速，若環境無顯卡會自動回退為 CPU。
        """
        if languages is None:
            # 優先載入繁體中文和英文
            languages = ['ch_tra', 'en']
            
        # [效能優化] 限制 PyTorch 在 CPU 模式下吃的執行緒數量，避免吃滿 100% 導致電腦卡頓
        try:
            import torch
            if not gpu or not torch.cuda.is_available():
                torch.set_num_threads(4)
        except ImportError:
            pass

        logging.info(f"正在初始化 EasyOCR 引擎，語系: {languages}, GPU: {gpu}")
        # 第一版實作：使用 EasyOCR 作為引擎
        try:
            import easyocr
        except ImportError as e:
            raise RuntimeError("easyocr 未安裝，無法初始化 OCR 引擎。") from e

        self.reader = easyocr.Reader(languages, gpu=gpu)
        logging.info("EasyOCR 引擎初始化完成。")

    def _preprocess_image(self, img: np.ndarray) -> np.ndarray:
        """
        影像預處理：轉灰階、強化對比、二值化等，提升辨識率。
        
        Args:
            img: OpenCV BGR 圖片矩陣。
        Returns:
            處理後的灰階圖片矩陣。
        """
        # 轉灰階
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # 可選：根據遊戲畫面的特性加入 CLAHE (限制對比度自適應直方圖均衡化)
        # 提升局部對比度，有助於看清背景複雜的文字
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        
        return enhanced

    def read_text(self, img: np.ndarray, roi: tuple[int, int, int, int] = None) -> list[tuple[str, float, tuple]]:
        """
        辨識影像中的文字。
        
        Args:
            img: OpenCV BGR 圖片矩陣 (從 WindowManager.capture() 取得)。
            roi: 限制辨識範圍 (x, y, width, height)。若為 None 則辨識全圖。
            
        Returns:
            辨識結果列表：[(文字字串, 信心值, 邊界框)]。
            邊界框格式：((x1, y1), (x2, y2), (x3, y3), (x4, y4))，座標為原始圖片中的絕對座標。
        """
        # 若指定了 ROI，則先裁切圖片
        if roi is not None:
            x, y, w, h = roi
            # 確保範圍不超出圖片邊界
            img_h, img_w = img.shape[:2]
            y1, y2 = max(0, y), min(img_h, y + h)
            x1, x2 = max(0, x), min(img_w, x + w)
            target_img = img[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        else:
            target_img = img.copy()
            offset_x, offset_y = 0, 0

        # 影像預處理以提升準確率
        processed_img = self._preprocess_image(target_img)

        # 執行辨識
        # detail=1 回傳 [([bbox], text, prob)]
        raw_results = self.reader.readtext(processed_img, detail=1)
        
        final_results = []
        for bbox, text, prob in raw_results:
            # 清理文字：去除頭尾空白
            clean_text = text.strip()
            if not clean_text:
                continue
                
            # 將 bbox 座標加上 offset_x, offset_y 還原成原圖的絕對座標
            abs_bbox = []
            for point in bbox:
                abs_bbox.append((int(point[0] + offset_x), int(point[1] + offset_y)))
                
            final_results.append((clean_text, float(prob), tuple(abs_bbox)))
            
        return final_results

    def read_text_simple(self, img: np.ndarray, roi: tuple[int, int, int, int] = None) -> list[str]:
        """
        簡易版的 read_text()，只回傳純文字列表。
        
        Args:
            img: OpenCV BGR 圖片矩陣。
            roi: 限制辨識範圍。
            
        Returns:
            辨識出的字串列表。
        """
        results = self.read_text(img, roi)
        return [text for text, prob, bbox in results]
