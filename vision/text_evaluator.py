"""
文字與權重判定評估器 (TextEvaluator)

接收 OCR 辨識結果，與 priority_list.json 的權重設定進行關鍵字比對，
決定目前畫面中最優的選擇項目。
"""
import json
import logging
from pathlib import Path

class TextEvaluator:
    """
    載入外部設定檔 (JSON)，提供權重判定功能。
    支援模糊匹配與包含關係比對。
    """
    def __init__(self, priority_list_path: str = "data/priority_list.json"):
        """
        Args:
            priority_list_path: 權重設定檔的路徑。
        """
        self.priority_list_path = Path(priority_list_path)
        self.potentials = {}
        self.default_weight = 0
        self._load_priority_list()

    def _load_priority_list(self) -> None:
        """載入並解析 JSON 設定檔。"""
        if not self.priority_list_path.exists():
            logging.warning(f"找不到權重設定檔 {self.priority_list_path}，將使用空設定。")
            return

        try:
            with open(self.priority_list_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # 建立 name/alias 到 weight 的映射
                self.potentials = {}
                # 定義 Tier 的預設權重
                tier_weights = {"S": 100, "A": 80, "B": 60, "C": 40}
                
                potentials_list = data.get("potentials", [])
                for item in potentials_list:
                    name = item.get("name", "")
                    tier = item.get("tier", "C")
                    aliases = item.get("aliases", [name])
                    
                    weight = tier_weights.get(tier, 10)
                    
                    for alias in aliases:
                        # 將空白去除，以便後續與 OCR 讀出的字串比對
                        clean_alias = alias.replace(" ", "")
                        self.potentials[clean_alias] = weight
                        
                self.default_weight = data.get("default_weight", 0)
                logging.info(f"成功載入權重設定檔，共 {len(self.potentials)} 組別名對應。")
        except Exception as e:
            logging.error(f"解析 {self.priority_list_path} 發生錯誤: {e}")

    def evaluate(self, ocr_results: list[tuple[str, float, tuple]]) -> list[dict]:
        """
        對 OCR 辨識出的文字列表進行權重評分排序。
        
        Args:
            ocr_results: OcrEngine._read_text() 回傳的辨識結果格式。
                         [(text, probability, bbox), ...]
                         
        Returns:
            排序後（權重由高到低）的選項列表：
            [{"text": "原始文字", "matched_key": "比對到的關鍵字", "weight": 權重值, "bbox": 邊界框}, ...]
        """
        evaluated = []
        
        for text, prob, bbox in ocr_results:
            # 1. 將讀取出的文字做初步清理 (去空白/轉小寫等，視需求而定)
            clean_text = text.replace(" ", "")
            if not clean_text:
                continue

            best_match_key = None
            best_weight = self.default_weight

            # 2. 與潛能關鍵字進行包含比對（或精確比對）
            # 例如: OCR 讀出 "攻擊力提升 I"，而設定檔有 "攻擊力提升" => 匹配成功
            for key, weight in self.potentials.items():
                if key in clean_text:
                    if weight > best_weight:
                        best_weight = weight
                        best_match_key = key

            evaluated.append({
                "text": text,
                "matched_key": best_match_key,
                "weight": best_weight,
                "bbox": bbox,
                "prob": prob
            })

        # 3. 根據權重由高至低排序 (如果權重相同，則以 OCR 信心值 prob 輔助排序)
        evaluated.sort(key=lambda x: (x["weight"], x["prob"]), reverse=True)
        return evaluated

    def get_best_option(self, ocr_results: list[tuple[str, float, tuple]]) -> dict:
        """
        取得所有 OCR 辨識結果中，權重最高的一個選項 (通常就是我們要點擊的目標)。
        """
        evaluated = self.evaluate(ocr_results)
        if evaluated:
            return evaluated[0]
        return None
