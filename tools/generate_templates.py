"""
generate_templates.py — 靜態圖示截圖輔助工具

使用方式：
  1. 開啟遊戲，切換至含目標圖示的畫面（如結算畫面左下角的垃圾桶）
  2. 執行本腳本：
       python tools/generate_templates.py
  3. 在彈出的截圖視窗上，用滑鼠拖拉框選目標「無文字圖示」
  4. 按 Enter 儲存 / 按 Esc 重框選
  5. 輸入此圖示的模板名稱（如 icon_trash），自動存至 assets/templates/

【注意】由於最新的點擊策略已改用 OCR 尋找文字按鈕，您 **不需要** 截取任何帶有文字的按鈕（如出發、確認、下一步等）。

請只截取以下「無文字圖示」並使用指定的名稱：
  icon_money        — 右上角的金錢圖示 💰
  icon_reset        — 商店內「重置商店」旁邊的循環圖示
  icon_trash        — 結算畫面左下角的垃圾桶圖示 🗑️
  note_1 ~ note_13  — 13 種不同的音符小圖示（名稱只要前綴是 note_ 即可分辨）
"""

import sys
import argparse
from pathlib import Path

# 確保能 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from utils.window_mgr import WindowManager


# ─────────────────────────────────────────────────────────────
# 全域：框選狀態
# ─────────────────────────────────────────────────────────────
_roi_start = None
_roi_end   = None
_drawing   = False
_selection = None   # (x, y, w, h)
_display   = None   # 用於 imshow 的圖片


def _on_mouse(event, x, y, flags, param):
    global _roi_start, _roi_end, _drawing, _selection, _display
    frame = param["frame"]

    if event == cv2.EVENT_LBUTTONDOWN:
        _roi_start = (x, y)
        _drawing   = True
        _selection = None

    elif event == cv2.EVENT_MOUSEMOVE and _drawing:
        overlay = frame.copy()
        cv2.rectangle(overlay, _roi_start, (x, y), (0, 200, 0), 2)
        _display = overlay

    elif event == cv2.EVENT_LBUTTONUP:
        _roi_end  = (x, y)
        _drawing  = False
        x1, y1 = min(_roi_start[0], x), min(_roi_start[1], y)
        x2, y2 = max(_roi_start[0], x), max(_roi_start[1], y)
        _selection = (x1, y1, x2 - x1, y2 - y1)
        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(overlay, "按 Enter 確認 / Esc 重選",
                    (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        _display = overlay


# ─────────────────────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────────────────────

def main():
    global _display, _selection

    parser = argparse.ArgumentParser(description="星塔旅人模板截圖輔助工具")
    parser.add_argument("--window", default="StellaSora", help="遊戲視窗標題")
    parser.add_argument("--output-dir", default="assets/templates", help="模板存放目錄")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 55)
    print("  星塔旅人靜態按鈕模板截圖工具")
    print("=" * 55)
    print(f"  輸出目錄：{output_dir.resolve()}")
    print()

    # 截圖
    print("[步驟 1] 擷取遊戲視窗截圖...")
    wm = WindowManager(window_name=args.window)
    try:
        wm.find_window()
        frame, method = wm.capture()
        print(f"  截圖成功 ({method})，解析度：{frame.shape[1]}×{frame.shape[0]}")
    except Exception as e:
        print(f"  [錯誤] 截圖失敗：{e}")
        print("  請先開啟遊戲並切換至目標畫面，再執行本工具。")
        sys.exit(1)

    _display = frame.copy()
    cv2.namedWindow("【框選按鈕區域】", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("【框選按鈕區域】", _on_mouse, param={"frame": frame})

    print()
    print("[步驟 2] 在視窗上用滑鼠框選目標按鈕區域")
    print("  Enter = 確認儲存  |  Esc = 重新框選  |  Q = 結束")
    print()

    while True:
        if _display is not None:
            cv2.imshow("【框選按鈕區域】", _display)

        key = cv2.waitKey(20) & 0xFF

        if key == 27:   # Esc — 重選
            _selection = None
            _display = frame.copy()
            print("  [重選] 請重新框選。")

        elif key == ord("q") or key == ord("Q"):
            print("  [退出] 使用者手動退出。")
            break

        elif key == 13 and _selection is not None:  # Enter
            x, y, w, h = _selection
            if w < 5 or h < 5:
                print("  [警告] 框選區域太小，請重試。")
                continue

            cropped = frame[y:y+h, x:x+w]
            print()
            name = input("  請輸入此圖示的模板名稱（例如 icon_trash）：").strip()
            if not name:
                print("  [跳過] 名稱為空，未儲存。")
                continue

            out_path = output_dir / f"{name}.png"
            cv2.imwrite(str(out_path), cropped)
            print(f"  [✅ 已儲存] {out_path}（{w}×{h} px）")
            print()

            # 詢問是否繼續
            again = input("  是否繼續截取其他按鈕？[y/N] ").strip().lower()
            if again != "y":
                break
            _selection = None
            _display = frame.copy()

    cv2.destroyAllWindows()
    print()
    print("[完成] 所有模板儲存完畢。")
    print(f"  目前已存在的模板：")
    for f in sorted(output_dir.glob("*.png")):
        print(f"    {f.name}")


if __name__ == "__main__":
    main()
