"""
GDS Canvas Designer — Phase 1
Entry point. Bootstraps QApplication and launches the main window.
"""

import sys
import os

# Ensure HiDPI scaling before QApplication is created
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtCore import Qt

from ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("GDS Canvas Designer")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("Fabrication Tools")

    # Set application-wide default font
    font = QFont("SF Mono", 11)
    font.setStyleHint(QFont.StyleHint.Monospace)
    app.setFont(font)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
