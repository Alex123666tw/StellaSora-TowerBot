"""
模板比對模組互動式測試腳本

使用流程：
1. 先執行本腳本，它會自動擷取一張遊戲畫面並存成 test_output.jpg
2. 用小畫家或 IrfanView 打開 test_output.jpg，裁切你想辨識的 UI 元素
3. 把裁切下來的圖存到 assets/templates/ 目錄下，例如 quick_battle_btn.png
4. 再次執行本腳本，選擇「[2] 比對單一模板」或「[3] 比對全部模板」驗證準確率
"""
import sys
import os
import cv2

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from _bootstrap import PROJECT_ROOT

from utils.window_mgr import WindowManager
from vision.matcher import TemplateMatcher


# ────────────────────────────────────────────
# 設定區（依需求調整）
# ────────────────────────────────────────────
GAME_WINDOW_NAME = "StellaSora"
TEMPLATE_DIR     = "assets/templates"
THRESHOLD        = 0.80          # 辨識信心值門檻（建議 0.75 ~ 0.85）
SAVE_ANNOTATED   = True          # 是否將標記結果存成圖片
OUTPUT_DIR       = "test_results"


def capture_scene(win_mgr: WindowManager) -> tuple:
    """擷取目前遊戲畫面，回傳 (圖片, 方法說明)"""
    img, method = win_mgr.capture()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cv2.imwrite(os.path.join(OUTPUT_DIR, "latest_scene.jpg"), img)
    print(f"✅ 畫面擷取成功 [{method}]，尺寸: {img.shape[1]}x{img.shape[0]}")
    print(f"   已存至 {OUTPUT_DIR}/latest_scene.jpg\n")
    return img


def test_single(scene, matcher: TemplateMatcher):
    """比對單一模板並顯示結果"""
    templates = matcher.templates
    if not templates:
        print("❌ 尚無模板，請先將截圖裁切後放到 assets/templates/ 目錄下。")
        return

    print("可用模板：")
    for i, name in enumerate(templates, 1):
        print(f"  [{i}] {name}")

    try:
        idx = int(input("\n請輸入編號: ")) - 1
        name = templates[idx]
    except (ValueError, IndexError):
        print("❌ 輸入無效")
        return

    result = matcher.match(scene, name)
    print(f"\n結果：{result}")

    if SAVE_ANNOTATED:
        annotated = matcher.draw_result(scene, result)
        out_path = os.path.join(OUTPUT_DIR, f"match_{name}.jpg")
        cv2.imwrite(out_path, annotated)
        print(f"✅ 標記結果已存至：{out_path}")


def test_all(scene, matcher: TemplateMatcher):
    """比對全部模板，輸出完整結果表格"""
    results = matcher.match_all(scene)
    if not results:
        print("❌ 尚無模板，請先將截圖裁切後放到 assets/templates/ 目錄下。")
        return

    found     = [r for r in results.values() if r.found]
    not_found = [r for r in results.values() if not r.found]

    print(f"\n{'─'*55}")
    print(f"  {'模板名稱':<25} {'信心值':>8}  {'狀態'}")
    print(f"{'─'*55}")

    for r in sorted(results.values(), key=lambda x: -x.confidence):
        status = "✅ 找到" if r.found else "❌ 未找"
        print(f"  {r.name:<25} {r.confidence:>8.3f}  {status}")

    print(f"{'─'*55}")
    print(f"  找到 {len(found)} / {len(results)} 個模板\n")

    if SAVE_ANNOTATED and found:
        annotated = scene.copy()
        for r in found:
            annotated = matcher.draw_result(annotated, r)
        out_path = os.path.join(OUTPUT_DIR, "match_all_result.jpg")
        cv2.imwrite(out_path, annotated)
        print(f"✅ 所有匹配標記已存至：{out_path}")


def test_threshold(scene, matcher: TemplateMatcher):
    """調整閾值，觀察辨識結果變化"""
    templates = matcher.templates
    if not templates:
        print("❌ 尚無模板")
        return

    print("可用模板：")
    for i, name in enumerate(templates, 1):
        print(f"  [{i}] {name}")

    try:
        idx = int(input("\n請輸入編號: ")) - 1
        name = templates[idx]
    except (ValueError, IndexError):
        print("❌ 輸入無效")
        return

    print(f"\n{'閾值':>8}  {'信心值':>8}  {'結果'}")
    print("─" * 32)
    for thr in [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60]:
        r = matcher.match(scene, name, threshold=thr)
        status = "✅ 找到" if r.found else "  未找"
        print(f"  {thr:.2f}   {r.confidence:>8.3f}  {status}")


def main():
    win_mgr = WindowManager(window_name=GAME_WINDOW_NAME)
    matcher  = TemplateMatcher(
        template_dir=TEMPLATE_DIR,
        threshold=THRESHOLD,
    )

    scene = None

    while True:
        print("\n" + "═" * 45)
        print("  ★ 星塔旅人 - 模板比對測試工具")
        print("═" * 45)
        print("  [1] 擷取目前遊戲畫面")
        print("  [2] 比對單一模板")
        print("  [3] 比對全部模板")
        print("  [4] 閾值調整測試")
        print("  [5] 重新載入所有模板")
        print("  [0] 離開")
        print("─" * 45)

        choice = input("請輸入選項：").strip()

        if choice == "1":
            print()
            try:
                scene = capture_scene(win_mgr)
            except Exception as e:
                print(f"❌ 擷取失敗：{e}")

        elif choice in ("2", "3", "4"):
            if scene is None:
                print("⚠ 請先選 [1] 擷取畫面。")
                continue
            if choice == "2":
                test_single(scene, matcher)
            elif choice == "3":
                test_all(scene, matcher)
            else:
                test_threshold(scene, matcher)

        elif choice == "5":
            matcher.reload()

        elif choice == "0":
            print("離開。")
            break

        else:
            print("請輸入 0~5 的選項。")


if __name__ == "__main__":
    main()

