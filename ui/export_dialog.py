"""
ui/export_dialog.py — GDS Export: layer mapping dialog and result dialog.

Flow:
  1. ExportDialog opens — shows one row per unique app-layer found in the design.
     Each row lets the user set the GDS layer number and datatype (both spinboxes).
  2. User picks a file path via QFileDialog.
  3. exporter.export_gds() is called.
  4. ExportResultDialog shows the verification summary (shapes, layers, bbox).
     It also offers a "Open in KLayout" button that launches klayout on the file.
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox,
    QDialogButtonBox, QFileDialog, QWidget, QScrollArea,
    QFrame, QPushButton, QMessageBox, QGridLayout,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont

from core.model import DesignScene, dbu_to_um
from core.exporter import export_gds, ExportError, LayerMap


# ── Tiny style helpers ────────────────────────────────────────────────────────

_BG     = "#1e293b"
_BORDER = "#334155"
_TEXT   = "#e2e8f0"
_MUTED  = "#94a3b8"
_ACCENT = "#38bdf8"


def _label(text: str, muted: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {'#94a3b8' if muted else '#e2e8f0'}; "
        f"font-size: 12px; background: transparent;"
    )
    return lbl


def _spinbox(value: int, lo: int, hi: int) -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(lo, hi)
    sb.setValue(value)
    sb.setFixedWidth(72)
    sb.setStyleSheet(f"""
        QSpinBox {{
            background: #0f172a;
            border: 1px solid {_BORDER};
            border-radius: 4px;
            color: {_TEXT};
            padding: 4px 6px;
            font-size: 12px;
        }}
        QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
    """)
    return sb


# ── Layer mapping dialog ──────────────────────────────────────────────────────

class ExportDialog(QDialog):
    """
    Shows one row per app-layer present in the design.
    User maps each to a (gds_layer, datatype) pair.
    On Accept → opens QFileDialog → calls exporter → shows result.
    """

    def __init__(self, design: DesignScene, parent=None, overlay=None) -> None:
        super().__init__(parent)
        self._design  = design
        self._overlay = overlay
        self._rows: Dict[int, Tuple[QSpinBox, QSpinBox]] = {}   # app_layer → (layer_sb, dt_sb)

        self.setWindowTitle("Export GDS")
        self.setMinimumWidth(480)
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("GDS Layer Mapping")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: bold;")
        root.addWidget(title)

        sub = _label("Map each canvas layer to a GDS (layer, datatype) pair.", muted=True)
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Header row
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        for col, hdr in enumerate(["Canvas Layer", "GDS Layer", "Datatype"]):
            lbl = _label(hdr, muted=True)
            lbl.setStyleSheet(lbl.styleSheet() + " font-size: 11px; letter-spacing: 1px;")
            grid.addWidget(lbl, 0, col)

        # One row per unique layer
        app_layers = sorted({c.layer for c in self._design.components})
        for row_i, app_layer in enumerate(app_layers, start=1):
            grid.addWidget(_label(f"Layer {app_layer}"), row_i, 0)
            layer_sb = _spinbox(app_layer, 0, 255)
            dt_sb    = _spinbox(0, 0, 255)
            grid.addWidget(layer_sb, row_i, 1)
            grid.addWidget(dt_sb,    row_i, 2)
            self._rows[app_layer] = (layer_sb, dt_sb)

        # Show the undercut ring layer as a fixed info row when overlay is active
        if self._overlay is not None and self._overlay.is_enabled:
            from core.exporter import UNDERCUT_RING_LAYER, UNDERCUT_RING_DATATYPE
            uc_row = len(app_layers) + 1
            uc_lbl = _label("Undercut Ring", muted=True)
            uc_lbl.setStyleSheet(uc_lbl.styleSheet() + " font-style: italic;")
            grid.addWidget(uc_lbl, uc_row, 0)
            grid.addWidget(_label(f"{UNDERCUT_RING_LAYER}  (fixed)", muted=True), uc_row, 1)
            grid.addWidget(_label(f"{UNDERCUT_RING_DATATYPE}  (fixed)", muted=True), uc_row, 2)

        scroll = QScrollArea()
        scroll.setWidget(grid_w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {_BG};")
        scroll.setMaximumHeight(280)
        root.addWidget(scroll)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        root.addWidget(sep)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Choose file & Export…")
        btns.accepted.connect(self._on_export)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: #0f172a;
                border: 1px solid {_BORDER};
                border-radius: 4px;
                color: {_TEXT};
                padding: 6px 18px;
                font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {_ACCENT}; color: {_ACCENT}; }}
        """)
        root.addWidget(btns)

    def _build_layer_map(self) -> LayerMap:
        return {
            app_layer: (layer_sb.value(), dt_sb.value())
            for app_layer, (layer_sb, dt_sb) in self._rows.items()
        }

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save GDS File",
            f"{self._design.name}.gds",
            "GDS Files (*.gds);;All Files (*)",
        )
        if not path:
            return   # user cancelled file dialog

        layer_map = self._build_layer_map()
        try:
            summary = export_gds(self._design, path, layer_map,
                                  overlay=self._overlay)
        except ExportError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
            return

        self.accept()
        result_dlg = ExportResultDialog(summary, self.parent())
        result_dlg.exec()


# ── Result / verification dialog ──────────────────────────────────────────────

class ExportResultDialog(QDialog):
    """Shows the post-export verification summary and KLayout launch button."""

    def __init__(self, summary: dict, parent=None) -> None:
        super().__init__(parent)
        self._path = summary["path"]
        self.setWindowTitle("Export Successful")
        self.setMinimumWidth(440)
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")
        self._build_ui(summary)

    def _build_ui(self, s: dict) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Export Successful")
        title.setStyleSheet(
            f"color: #4ade80; font-size: 15px; font-weight: bold;"
        )
        root.addWidget(title)

        bbox = s["bbox_um"]
        w_um = bbox[2] - bbox[0]
        h_um = bbox[3] - bbox[1]

        rows = [
            ("File",    s["path"]),
            ("Cell",    s["cell"]),
            ("Shapes",  str(s["shapes"])),
            ("Layers",  ", ".join(f"({l},{d})" for l, d in s["layers"]) or "—"),
            ("Width",   f"{w_um:.3f} µm"),
            ("Height",  f"{h_um:.3f} µm"),
            ("BBox",    f"({bbox[0]:.3f}, {bbox[1]:.3f}) → ({bbox[2]:.3f}, {bbox[3]:.3f}) µm"),
        ]

        grid_w = QWidget()
        grid   = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        for i, (k, v) in enumerate(rows):
            grid.addWidget(_label(k, muted=True), i, 0)
            val = QLabel(v)
            val.setWordWrap(True)
            val.setStyleSheet(
                f"color: {_TEXT}; font-size: 12px; "
                f"font-family: 'SF Mono', 'Fira Mono', monospace;"
            )
            grid.addWidget(val, i, 1)
        root.addWidget(grid_w)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        root.addWidget(sep)

        # Buttons row
        btn_row = QHBoxLayout()

        klayout_btn = QPushButton("Open in KLayout")
        klayout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        klayout_btn.setStyleSheet(f"""
            QPushButton {{
                background: #0f172a;
                border: 1px solid {_BORDER};
                border-radius: 4px;
                color: {_ACCENT};
                padding: 6px 18px;
                font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {_ACCENT}; background: #1e3a5f; }}
            QPushButton:disabled {{ color: {_MUTED}; border-color: {_BORDER}; }}
        """)
        klayout_btn.clicked.connect(self._open_klayout)

        # Grey out if klayout not on PATH
        if not shutil.which("klayout"):
            klayout_btn.setEnabled(False)
            klayout_btn.setToolTip("klayout not found on PATH")

        ok_btn = QPushButton("Close")
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(klayout_btn.styleSheet().replace(_ACCENT, _TEXT))
        ok_btn.clicked.connect(self.accept)

        btn_row.addWidget(klayout_btn)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        root.addLayout(btn_row)

    def _open_klayout(self) -> None:
        try:
            import os
            env = os.environ.copy()
            # Strip X11/ICE session-manager vars that Qt sets on itself.
            # KLayout inherits them, tries to attach to the same ICE socket,
            # fails (errno=0), and the ICE error handler calls exit().
            # Unsetting them makes KLayout start its own clean ICE session.
            for var in ("SESSION_MANAGER", "QT_SESSION_KEY",
                        "QT_SESSION_ID", "XSESSION_IS_UP"):
                env.pop(var, None)
            subprocess.Popen(
                ["klayout", self._path],
                close_fds=True,          # don't leak Qt's X11 fds
                start_new_session=True,  # setsid() — detach from our process group
                env=env,
            )
        except Exception as exc:
            QMessageBox.warning(self, "KLayout", f"Could not launch KLayout:\n{exc}")