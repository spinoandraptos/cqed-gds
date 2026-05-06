"""
ui/cell_palette.py — Cell Library panel (left dock, "Cells" tab).

Design
------
- Zero business logic.  Drag to canvas → scene.drop_cell() handles placement.
- Each cell gets one DraggableCellButton, styled identically to DraggableShapeButton.
- All parameter editing is done *after* placement, in the Properties panel
  (PropertiesPanel.show_cell_group), matching the UX of every other component.
- Parameters travel as JSON in the drag MIME payload so the default values are
  available at drop time without needing a separate dialog.

MIME type: application/x-gds-cell
Payload  : "<cell_id>:<json_params_dict>"  — params are the catalogue defaults.
           The receiver (CanvasView.dropEvent) calls scene.drop_cell(cell_id, pos, params).
"""

from __future__ import annotations

import json
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QApplication,
)
from PyQt6.QtCore import Qt, QMimeData, QPoint, QByteArray, QPointF
from PyQt6.QtGui import QColor, QPixmap, QPainter, QPen, QPainterPath, QDrag, QMouseEvent

from ui.theme import Colors, Fonts, Geometry
from core.cell_library import CELL_CATALOGUE, CellDef


# ── Category section header ───────────────────────────────────────────────────

class _CategoryLabel(QLabel):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text.upper(), parent)
        self.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            font-size: {Fonts.SIZE_XS}px;
            letter-spacing: 1.5px;
            padding: 10px 0 4px 0;
        """)



# ── Icon helper ───────────────────────────────────────────────────────────────
# Import the accurate per-cell icon painters from panels.py so both palette
# widgets show identical icons without duplicating drawing code.
# If panels is not importable (standalone usage), fall back to a plain rect.
try:
    from ui.panels import _cell_icon  # noqa: F401  (re-exported for DraggableCellButton below)
except ImportError:
    def _cell_icon(cell_id: str, size: int = 36) -> QPixmap:  # type: ignore[misc]
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        c = QColor("#7c3aed"); c.setAlpha(120)
        p.setBrush(c); p.setPen(QPen(QColor("#a78bfa"), 1.2))
        p.drawRoundedRect(4, 4, size - 8, size - 8, 2, 2)
        p.end()
        return pix


# ── Draggable cell tile ───────────────────────────────────────────────────────

class DraggableCellButton(QPushButton):
    """
    Palette tile for one parametric cell.

    Drag behaviour
    --------------
    Mouse-move-while-pressed starts a Qt drag carrying:
      MIME type : application/x-gds-cell
      Payload   : "<cell_id>:<json_defaults>"

    The canvas view decodes this in dropEvent and calls
    scene.drop_cell(cell_id, scene_pos, params).

    Visual style mirrors DraggableShapeButton in panels.py so the two
    palette tabs feel identical.
    """

    MIME_TYPE = "application/x-gds-cell"

    def __init__(self, cdef: CellDef, parent=None) -> None:
        super().__init__(parent)
        self._cdef       = cdef
        self._drag_start: Optional[QPoint] = None

        self.setFixedHeight(58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # ── Outer horizontal layout: icon | text column ───────────────────────
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 7, 12, 7)
        outer.setSpacing(10)

        # Icon (hexagon with centre dot)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_cell_icon(cdef.cell_id, size=36))
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setStyleSheet("background: transparent;")
        outer.addWidget(icon_lbl)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        top = QLabel(cdef.name)
        top.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; background: transparent;"
        )
        bot = QLabel("Drag to canvas to place")
        bot.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; background: transparent;"
        )
        text_col.addWidget(top)
        text_col.addWidget(bot)
        outer.addLayout(text_col)

        self.setStyleSheet(f"""
            QPushButton {{
                background: {Colors.BG_ELEVATED};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: {Geometry.BORDER_RADIUS}px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: {Colors.BG_OVERLAY};
                border-color: {Colors.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {Colors.ACCENT_GLOW};
                border-color: {Colors.ACCENT};
            }}
        """)

    # ── Drag logic ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.pos() - self._drag_start).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        payload = f"{self._cdef.cell_id}:{json.dumps(self._cdef.defaults)}".encode()

        mime = QMimeData()
        mime.setData(self.MIME_TYPE, QByteArray(payload))

        # Semi-transparent drag ghost
        pix = QPixmap(120, 32)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setOpacity(0.80)
        p.fillRect(pix.rect(), QColor("#1e293b"))
        p.setPen(QColor("#7c3aed"))
        p.drawRect(0, 0, pix.width() - 1, pix.height() - 1)
        p.setPen(QColor("#c4b5fd"))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, f"⬡ {self._cdef.name}")
        p.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


# ── Public panel widget ───────────────────────────────────────────────────────

class CellLibraryPanel(QWidget):
    """
    Scrollable panel listing all cells from CELL_CATALOGUE, grouped by
    category.  Each tile is a drag source — drag it onto the canvas to place
    the cell with default parameters.  Edit parameters afterward in the
    Properties panel (right dock).

    No signals needed — all communication goes through the drag-and-drop
    protocol and the canvas scene.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(2)

        title = QLabel("Cell Library")
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 13px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        sub = QLabel("Drag a cell onto the canvas to place it")
        sub.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        hl.addWidget(title)
        hl.addWidget(sub)
        root.addWidget(header)

        # ── Scrollable cell list ───────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        container = QWidget()
        container.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(Geometry.PANEL_PADDING, 4,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(4)

        self._populate(lay)
        lay.addStretch()

        scroll.setWidget(container)
        root.addWidget(scroll)

    # ── Build tiles grouped by category ──────────────────────────────────────

    def _populate(self, layout: QVBoxLayout) -> None:
        seen: list[str] = []
        by_cat: dict[str, list[CellDef]] = {}
        for cdef in CELL_CATALOGUE:
            if cdef.category not in by_cat:
                seen.append(cdef.category)
                by_cat[cdef.category] = []
            by_cat[cdef.category].append(cdef)

        for cat in seen:
            layout.addWidget(_CategoryLabel(cat))
            for cdef in by_cat[cat]:
                layout.addWidget(DraggableCellButton(cdef))

            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {Colors.BG_BORDER}; margin-top: 4px;")
            layout.addWidget(sep)