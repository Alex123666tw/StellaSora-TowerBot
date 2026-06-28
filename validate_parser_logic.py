import os
import cv2
import yaml
import json
import numpy as np
import unittest
import sys
from unittest.mock import patch, MagicMock

# Mock easyocr to avoid heavy installation just for testing logic
sys.modules['easyocr'] = MagicMock()

from tools.parse_guide_image import (
    classify_block,
    fuzzy_match,
    load_alias_map,
    pair_labels_to_potentials,
    ParsedBlock,
    write_config,
    parse_guide_image,
    LABEL_KEYWORDS
)

class TestParseGuideImage(unittest.TestCase):

    def setUp(self):
        # Create a temp config yaml
        self.test_config = "test_config.yaml"
        if os.path.exists(self.test_config):
            os.remove(self.test_config)
            
        # Create a valid tiny image
        self.valid_img_path = "test_valid.png"
        cv2.imwrite(self.valid_img_path, np.zeros((100, 100, 3), dtype=np.uint8))
        
        # Create a blank/empty file
        self.empty_img_path = "test_empty.png"
        with open(self.empty_img_path, "wb") as f:
            pass
            
    def tearDown(self):
        for f in [self.test_config, self.valid_img_path, self.empty_img_path]:
            if os.path.exists(f):
                os.remove(f)

    # 1. 正常讀取圖片與邊界案例 (空白圖片/錯誤路徑)
    def test_image_loading_and_edge_cases(self):
        # 測試錯誤路徑，不應拋出例外
        try:
            parse_guide_image("non_existent_image.png", self.test_config)
            success = True
        except Exception:
            success = False
        self.assertTrue(success, "Non-existent image should not throw exception")
        
        # 測試空檔案 (不合法圖片格式)，不應崩潰
        try:
            parse_guide_image(self.empty_img_path, self.test_config)
            success = True
        except Exception:
            success = False
        self.assertTrue(success, "Empty image file should not throw exception")
        
        # 測試沒有文字的圖片
        try:
            with patch('builtins.input', return_value='n'):  # 模擬不寫入
                parse_guide_image(self.valid_img_path, self.test_config)
            success = True
        except Exception:
            success = False
        self.assertTrue(success, "Image with no text should not throw exception")

    # 2. 中文文字辨識分類 (LABEL_KEYWORDS)
    def test_classify_labels(self):
        alias_map = load_alias_map("data/priority_list.json")
        
        # 測試必選標籤
        block = classify_block(" 必拿 ", alias_map)
        self.assertEqual(block.block_type, "label")
        self.assertEqual(block.matched_label, "required")
        
        block = classify_block(" S級 ", alias_map)
        self.assertEqual(block.block_type, "label")
        self.assertEqual(block.matched_label, "required")

        # 測試備選標籤
        block = classify_block(" 備選 ", alias_map)
        self.assertEqual(block.block_type, "label")
        self.assertEqual(block.matched_label, "backup")

        # 測試垃圾標籤
        block = classify_block(" 地雷 ", alias_map)
        self.assertEqual(block.block_type, "label")
        self.assertEqual(block.matched_label, "skip")

    # 3. 模糊比對
    def test_fuzzy_match(self):
        alias_map = load_alias_map("data/priority_list.json")
        self.assertTrue(len(alias_map) > 0, "Should load alias map")
        
        # 精確匹配
        self.assertEqual(fuzzy_match("攻擊提升", alias_map), "攻擊力提升")
        self.assertEqual(fuzzy_match("ATKUP", alias_map), "攻擊力提升")
        
        # 子字串匹配 (包含前後雜訊)
        self.assertEqual(fuzzy_match(">>攻擊提升<<", alias_map), "攻擊力提升")
        self.assertEqual(fuzzy_match("【攻擊提升】", alias_map), "攻擊力提升")
        
        # 不匹配
        self.assertIsNone(fuzzy_match("完全無關的文字", alias_map))

    # 4. 空間配對邏輯
    def test_spatial_pairing(self):
        # 建立模擬的 OCR 區塊
        blocks = [
            # 標籤: 必拿 (x=10, y=10)
            ParsedBlock(text="必拿", bbox=(0, 0, 20, 20), block_type="label", matched_label="required"),
            # 標籤: 備選 (x=10, y=100)
            ParsedBlock(text="備選", bbox=(0, 90, 20, 20), block_type="label", matched_label="backup"),
            
            # 潛能 1: 靠近"必拿" (x=40, y=10)
            ParsedBlock(text="攻擊力提升", bbox=(30, 0, 20, 20), block_type="potential_name", matched_potential="攻擊力提升"),
            # 潛能 2: 靠近"備選" (x=40, y=100)
            ParsedBlock(text="防禦力提升", bbox=(30, 90, 20, 20), block_type="potential_name", matched_potential="防禦力提升"),
            # 潛能 3: 距離極遠，超過 distance_threshold (img_width * 0.3)
            # 假設 img_width = 100, threshold = 30. 這個在 x=90, y=10, 距離超過 30，應該配對不到
            ParsedBlock(text="生命值強化", bbox=(80, 0, 20, 20), block_type="potential_name", matched_potential="生命值強化"),
        ]
        
        img_width = 100
        result = pair_labels_to_potentials(blocks, img_width)
        
        self.assertIn("攻擊力提升", result["required"])
        self.assertIn("防禦力提升", result["backup"])
        # 如果超出距離，是否綁定得到？ (x=90, y=10)中心點(90, 10)。必拿中心點(10, 10)。距離80。大於30。所以不該綁定到必拿。
        self.assertNotIn("生命值強化", result["required"])
        self.assertNotIn("生命值強化", result["backup"])

    # 5. 最終輸出 (config.yaml) 寫入行為
    def test_write_config(self):
        # 先建立帶有 max_reroll_before_backup 的模擬 config
        initial_cfg = {
            "decision": {
                "max_reroll_before_backup": 5,
                "required": ["舊必選"],
                "backup": ["舊備選"]
            }
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            yaml.dump(initial_cfg, f)
            
        new_result = {
            "required": ["攻擊力提升"],
            "backup": []
        }
        
        write_config(new_result, self.test_config)
        
        # 讀取檢查
        with open(self.test_config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            
        self.assertEqual(cfg["decision"]["required"], ["攻擊力提升"])
        self.assertEqual(cfg["decision"]["backup"], [])
        self.assertEqual(cfg["decision"]["max_reroll_before_backup"], 5, "Should preserve original settings")

if __name__ == "__main__":
    unittest.main(verbosity=2)
