"""
Phase 3 整合測試腳本 (test_integration.py)

驗證目標：
  1. 成功找到「星塔旅人」遊戲視窗。
  2. 使用 WindowManager（背景截圖優先 / mss 降級）擷取畫面。
  3. 將截圖存成 latest_scene.jpg。
  4. 使用 StateDetector（OCR）分析截圖並印出偵測到的狀態。

執行方式：
    python test_integration.py               # 標準執行
    python test_integration.py --save-path my_scene.jpg  # 自訂存檔路徑
    python test_integration.py --no-ocr      # 跳過 OCR，僅測試截圖

若遊戲未啟動，腳本仍會嘗試截全螢幕做降級示範，不會直接崩潰。
"""
import argparse
import logging
import sys
import cv2
from pathlib import Path
import yaml

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from _bootstrap import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from utils.privilege import exit_if_not_windows_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Phase 3 視覺控制層整合測試")
    parser.add_argument("--config", default="config.yaml", help="設定檔路徑")
    parser.add_argument("--save-path", default="latest_scene.jpg", help="截圖存檔路徑")
    parser.add_argument("--no-ocr", action="store_true", help="跳過 OCR 辨識，僅測試截圖")
    parser.add_argument("--window", default=None, help="目標視窗標題")
    parser.add_argument("--input-mode", choices=["foreground", "auto"], default=None, help="覆蓋 input.mode")
    args = parser.parse_args()

    exit_if_not_windows_admin("Stella Sora Active Probe")

    from utils.window_mgr import WindowManager
    from utils.input_sim import InputSimulator

    # ── 1. 鎖定視窗 ───────────────────────────────────────────
    print("=" * 55)
    print("  Phase 3 整合測試：截圖 + 狀態辨識")
    print("=" * 55)

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    window_name = args.window or cfg.get("window", {}).get("name", "StellaSora")
    input_mode = args.input_mode or cfg.get("input", {}).get("mode", "foreground")

    wm = WindowManager(window_name=window_name)
    inp = InputSimulator(window_name=window_name, mode=input_mode)

    try:
        wm.find_window()
        inp.attach()
        print(f"[PASS] 找到星塔旅人視窗！(HWND={wm.hwnd})")
    except Exception as e:
        print(f"[WARN] 找不到視窗: {e}")
        print("[INFO] 降級為 mss 全螢幕截圖示範...")

    # ── 2. 截圖 ───────────────────────────────────────────────
    print("\n[INFO] 嘗試截圖...")
    try:
        frame, method = wm.capture()
        h, w = frame.shape[:2]
        print(f"[PASS] 截圖成功！方法: {method}，解析度: {w}x{h}")
    except Exception as e:
        print(f"[FAIL] 截圖失敗: {e}")
        sys.exit(1)

    # ── 3. 存檔 ───────────────────────────────────────────────
    save_path = Path(args.save_path)
    success = cv2.imwrite(str(save_path), frame)
    if success:
        print(f"[PASS] 截圖已存至: {save_path.resolve()}")
    else:
        print(f"[FAIL] 截圖存檔失敗: {save_path}")

    # ── 4. OCR 狀態辨識 ───────────────────────────────────────
    if args.no_ocr:
        print("\n[INFO] --no-ocr 旗標啟用，跳過 OCR 辨識。")
    else:
        print("\n[INFO] 載入 OCR 引擎（首次載入需數秒）...")
        try:
            ocr_cfg = cfg.get("ocr", {})

            from vision.ocr_engine import OcrEngine
            from vision.state_detector import StateDetector

            ocr = OcrEngine(
                languages=ocr_cfg.get("languages", ["ch_tra", "en"]),
                gpu=ocr_cfg.get("gpu", False),
            )
            detector = StateDetector(ocr_engine=ocr)

            print("[INFO] OCR 辨識畫面中...")
            detection = detector.detect(frame, "STATE_HOME")
            detected = getattr(detection, "state", detection)
            confidence = getattr(detection, "confidence", None)
            print(f"[PASS] 偵測到狀態: {detected} (confidence={confidence})")
        except Exception as e:
            print(f"[WARN] OCR 辨識失敗（可略過）: {e}")

    print("\n" + "=" * 55)
    print("  測試完成！")
    print("=" * 55)


if __name__ == "__main__":
    main()

