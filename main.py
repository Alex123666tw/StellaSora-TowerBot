"""
程式進入點 (main.py)

初始化日誌系統與靜態資源，啟動狀態機主迴圈。

執行方式（確保 .venv 已啟用）：
    python main.py
    python main.py --config config.yaml
    python main.py --dry-run   # 骨架測試模式，不執行真實點擊
"""
import argparse
import logging
import sys
from pathlib import Path

# ── 確保可匯入專案模組 ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.bot import StateMachine
from utils.privilege import exit_if_not_windows_admin


def setup_logging(level: str = "INFO") -> None:
    """設定日誌格式與輸出層級。強制將 stdout 設為 UTF-8，避免 Windows cp950 編碼問題。"""
    import io
    # Windows 終端機預設為 cp950，強制轉換為 UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="星塔旅人自動刷紀錄 Bot"
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="設定檔路徑 (預設: config.yaml)"
    )
    parser.add_argument(
        "--log-level", "-l",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日誌輸出層級 (預設: INFO)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="骨架測試模式：僅執行狀態機迴圈數次，不進行真實操作"
    )
    args = parser.parse_args()

    exit_if_not_windows_admin("Stella Sora Bot")

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"找不到設定檔：{config_path}")
        sys.exit(1)

    logger.info(f"使用設定檔：{config_path.resolve()}")

    bot = StateMachine(config_path=str(config_path))

    if args.dry_run:
        logger.info("【Dry-Run 模式】僅執行 3 次迴圈後停止。")
        bot.ctx.max_runs = 1  # 只跑一輪確認流程暢通

    bot.run()


if __name__ == "__main__":
    main()
