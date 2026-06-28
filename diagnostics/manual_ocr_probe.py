"""
OCR 與權重判定功能測試腳本

這是一個簡單的測試腳本，用來驗證 OCR 引擎與潛能權重判定邏輯。
"""
import sys
import os
import cv2

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# 確保可載入 utils 與 vision 模組
from _bootstrap import PROJECT_ROOT

from utils.window_mgr import WindowManager
from vision.ocr_engine import OcrEngine
from vision.text_evaluator import TextEvaluator

def main():
    print("=== 星塔旅人：OCR 文字辨識與權重判定測試 ===")

    # 初始化模組
    print("[1/4] 正在初始化 OCR 引擎 (這可能需要幾秒鐘的時間以載入模型)...")
    try:
        # 指定使用繁體中文語系
        ocr = OcrEngine(languages=['ch_tra', 'en'], gpu=True)
    except Exception as e:
        print(f"❌ 初始化 OCR 引擎失敗: {e}")
        return

    print("[2/4] 正在載入權重設定檔...")
    evaluator = TextEvaluator(priority_list_path="data/priority_list.json")
    print(f"✅ 成功載入 {len(evaluator.potentials)} 筆權重設定。")

    print("\n[3/4] 正在擷取遊戲畫面...")
    win_mgr = WindowManager(window_name="StellaSora")
    try:
        img, method = win_mgr.capture()
        print(f"✅ 畫面擷取成功 [{method}]，尺寸: {img.shape[1]}x{img.shape[0]}")
    except Exception as e:
        print(f"❌ 無法擷取畫面: {e}")
        print("💡 提示：請確認遊戲是否已啟動，或是改用既有的測試圖片 (例如 test_output.jpg) 代替。")
        return

    # 指定 ROI (Region of Interest) 加快辨識速度並減少雜訊
    # 這裡的 ROI 只是一個範例 (例如畫面中間偏下的選擇區域)，可以根據實際情況調整
    # 如果不知道在哪裡，也可以傳 None 進行全圖辨識 (會比較耗時)
    # 範例 ROI (x, y, w, h)，假設潛能選項出現在畫面中間
    # roi = (img.shape[1] // 4, img.shape[0] // 3, img.shape[1] // 2, img.shape[0] // 2)
    # 先做全圖測試
    print("\n[4/4] 正在執行 OCR 文字辨識 (全圖辨識可能需要 1~2 秒)...")
    results = ocr.read_text(img, roi=None)
    
    if not results:
        print("⚠ 畫面中未辨識到任何文字。")
        return

    print(f"\n✅ 成功辨識到 {len(results)} 組文字。")
    print("\n=== 與權重清單比對並排序 ===")
    
    evaluated_results = evaluator.evaluate(results)
    
    # 印出排序後的前 10 個結果
    for i, res in enumerate(evaluated_results[:10]):
        text = res["text"]
        weight = res["weight"]
        prob = res["prob"]
        match_key = res["matched_key"]
        
        # UI 顯示美化
        mark = "🌟" if match_key else "  "
        match_info = f"[匹配: {match_key}]" if match_key else ""
        
        print(f"{mark} {i+1}. 權重: {weight:>3} | 信心: {prob:.2f} | 文字: '{text}' {match_info}")

    print("\n=== 最終選擇 ===")
    best_option = evaluator.get_best_option(results)
    if best_option and best_option["weight"] > 0:
         print(f"👉 系統判斷最佳選項為: '{best_option['text']}' (權重: {best_option['weight']})")
    else:
         print("🤷‍♂️ 畫面上沒有匹配到任何有設定權重的選項。")

if __name__ == "__main__":
    main()

