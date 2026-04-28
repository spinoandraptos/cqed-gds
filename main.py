"""
main.py — Entry point for the GDS Layout Editor.

Usage:
    python main.py
"""

import sys
from PyQt6.QtWidgets import QApplication
from app import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("GDS Layout Editor")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
