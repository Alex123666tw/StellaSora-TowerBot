import os
import sys

from utils.privilege import exit_if_not_windows_admin

try:
    import torch
except ImportError:
    pass

import PyQt5
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from gui.app import StellaSoraApp

plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
os.environ["QT_PLUGIN_PATH"] = plugin_path
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(plugin_path, "platforms")


def main() -> None:
    exit_if_not_windows_admin("Stella Sora GUI")

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    setTheme(Theme.LIGHT)

    window = StellaSoraApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
