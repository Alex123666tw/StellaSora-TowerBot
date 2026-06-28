"""Capture and click probe for the game window."""
from __future__ import annotations

import argparse
import sys

import cv2
import yaml

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from _bootstrap import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from utils.window_mgr import WindowManager
from utils.input_sim import InputSimulator
from utils.privilege import exit_if_not_windows_admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual capture and click probe")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--window", default=None, help="Override target window title")
    parser.add_argument("--input-mode", choices=["foreground", "auto"], default=None, help="Override input.mode")
    args = parser.parse_args()

    exit_if_not_windows_admin("Stella Sora Manual Probe")

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    window_name = args.window or cfg.get("window", {}).get("name", "StellaSora")
    input_mode = args.input_mode or cfg.get("input", {}).get("mode", "foreground")

    print("=== Stella Sora Manual Probe ===")

    win_mgr = WindowManager(window_name=window_name)
    input_sim = InputSimulator(window_name=window_name, mode=input_mode)

    try:
        print("[Step 1] Finding game window...")
        hwnd = win_mgr.find_window()
        print(f"[PASS] Found window HWND={hwnd}")

        print("\n[Step 2] Capturing frame...")
        img, method = win_mgr.capture()

        save_path = "test_output.jpg"
        cv2.imwrite(save_path, img)
        print(f"[PASS] Capture succeeded via {method}")
        print(f"        Shape: {img.shape}, saved to {save_path}")

        print(f"\n[Step 3] Sending {input_mode} click to X=100, Y=100...")
        input_sim.click(100, 100, delay=0.1)
        print("[PASS] Click dispatch completed")

        print("\n=== Probe Complete ===")
    except Exception as e:
        print(f"\n[FAIL] Probe failed: {e}")
        print("Check that the game window is open and the title matches the config.")


if __name__ == "__main__":
    main()
