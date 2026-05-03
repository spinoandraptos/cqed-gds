"""
ui/panels.py — Dock panel widgets: Component Palette and Properties Panel.

Phase 2 changes:
  - ComponentPalette: Polygon and Path buttons now enabled; subtitles updated.
    Signal changed to place_mode_requested(kind, layer) — emits the mode to enter,
    not "place now at view center". The scene's state machine handles actual placement.
  - PropertiesPanel: shows vertex count for polygons, path width for paths.
    Added an editable layer spinbox (read-only in Phase 1 → editable in Phase 2).
    layer_change_requested(comp_id, new_layer) signal for undo-aware edit.
    geometry_change_requested(comp_id, field, value_dbu) signal for width/height/path_width edits.
"""

from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QSpinBox, QDoubleSpinBox, QComboBox,
    QFrame, QPushButton, QSizePolicy, QScrollArea,
    QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QPoint, QByteArray
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter, QDrag, QMouseEvent

from ui.theme import Colors, Fonts, Geometry
from core.model import GDSComponent, ComponentKind, dbu_to_um, um_to_dbu


# ── Helper widgets ────────────────────────────────────────────────────────────

class SectionLabel(QLabel):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text.upper(), parent)
        self.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            font-size: {Fonts.SIZE_XS}px;
            letter-spacing: 1.5px;
            padding: 8px 0 4px 0;
        """)


class Separator(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setStyleSheet(f"color: {Colors.BG_BORDER};")
        self.setFixedHeight(1)


class ValueRow(QWidget):
    """Label + read-only value in a horizontal pair."""
    def __init__(self, label: str, value: str = "—", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)

        self._val = QLabel(value)
        self._val.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; "
            f"font-family: {Fonts.MONO_FAMILY};"
        )
        layout.addWidget(lbl)
        layout.addWidget(self._val)
        layout.addStretch()

    def set_value(self, v: str) -> None:
        self._val.setText(v)


def _layer_icon(layer: int, size: int = 14) -> QIcon:
    color = QColor(Colors.LAYER_COLORS[layer % len(Colors.LAYER_COLORS)])
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(color)
    color.setAlpha(180)
    p.setPen(color.lighter(150))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 2, 2)
    p.end()
    return QIcon(pix)


# ── Component Palette ─────────────────────────────────────────────────────────

class ComponentPalette(QWidget):
    """
    Left dock: layer selector + shape buttons.

    Phase 2 signal change: place_mode_requested(kind_value, layer)
    — tells the main window to put the scene into placement mode,
      rather than immediately dropping a component at view center.
    """

    place_mode_requested = pyqtSignal(object, int)   # ComponentKind, layer

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PALETTE_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_layer_section())
        root.addWidget(Separator())
        root.addWidget(self._build_shapes_section())
        root.addStretch()

    # ── Layer selector ────────────────────────────────────────────────────────

    def _build_layer_section(self) -> QWidget:
        w = QWidget(); w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(4)
        lay.addWidget(SectionLabel("Active Layer"))

        self._layer_combo = QComboBox()
        for i in range(8):
            self._layer_combo.addItem(_layer_icon(i), f"Layer {i}", i)
        self._layer_combo.setCurrentIndex(0)
        self._layer_combo.setStyleSheet(f"""
            QComboBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: {Geometry.BORDER_RADIUS}px;
                padding: 8px 12px;
                font-size: {Fonts.SIZE_SM}px;
                color: {Colors.TEXT_PRIMARY};
                min-height: 36px;
            }}
            QComboBox::drop-down {{ border: none; width: 26px; }}
        """)
        lay.addWidget(self._layer_combo)
        return w

    @property
    def active_layer(self) -> int:
        return self._layer_combo.currentData()

    # ── Shape buttons ─────────────────────────────────────────────────────────

    def _build_shapes_section(self) -> QWidget:
        w = QWidget(); w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(6)
        lay.addWidget(SectionLabel("Shapes"))

        shapes = [
            (ComponentKind.RECTANGLE, "▭  Rectangle",  "Click to stamp"),
            (ComponentKind.POLYGON,   "⬠  Polygon",    "Click vertices, Enter/dbl-click to close"),
            (ComponentKind.PATH,      "╌  Path",        "Click vertices, Enter to commit"),
        ]
        for kind, label, sub in shapes:
            lay.addWidget(self._make_shape_button(kind, label, sub))
        return w

    def _make_shape_button(self, kind: ComponentKind, label: str, sub: str) -> "DraggableShapeButton":
        btn = DraggableShapeButton(kind, self)
        btn.setFixedHeight(52)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        inner = QVBoxLayout(btn)
        inner.setContentsMargins(12, 7, 12, 7)
        inner.setSpacing(2)

        top = QLabel(label)
        top.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; background: transparent;"
        )
        bot = QLabel(sub)
        bot.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; background: transparent;"
        )
        bot.setWordWrap(True)
        inner.addWidget(top)
        inner.addWidget(bot)

        btn.setStyleSheet(f"""
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

        # Also keep click-to-enter-mode as a fallback
        btn.clicked.connect(
            lambda _, k=kind: self.place_mode_requested.emit(k, self.active_layer)
        )
        return btn


class DraggableShapeButton(QPushButton):
    """
    Shape palette button that starts a Qt drag on mouse-move-while-pressed.
    MIME type: application/x-gds-shape
    Payload:   "<kind_value>:<layer>" as UTF-8, e.g. "1:0" for RECTANGLE on layer 0
    """

    MIME_TYPE = "application/x-gds-shape"

    def __init__(self, kind: ComponentKind, palette: "ComponentPalette") -> None:
        super().__init__(palette)
        self._kind    = kind
        self._palette = palette
        self._drag_start: Optional[QPoint] = None

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
        layer   = self._palette.active_layer
        payload = f"{self._kind.value}:{layer}".encode()

        mime = QMimeData()
        mime.setData(self.MIME_TYPE, QByteArray(payload))

        # Build a small semi-transparent drag pixmap
        pix = QPixmap(80, 32)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setOpacity(0.75)
        p.fillRect(pix.rect(), QColor("#334155"))
        p.setPen(QColor("#94a3b8"))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, self._kind.name.capitalize())
        p.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


# ── Properties Panel ──────────────────────────────────────────────────────────

class PropertiesPanel(QWidget):
    """
    Right dock: selected component properties.

    Phase 2: Layer is now an editable spinbox (emits layer_change_requested
    so MainWindow can wrap it in an EditComponent command for undo).
    Vertex count row shown for polygon/path; path width row for paths.

    Phase 3: Width, Height, and Path Width are now editable QDoubleSpinBoxes.
    Each emits geometry_change_requested(comp_id, field, value_dbu) on commit
    (editingFinished — fires on Enter or focus-out, not on every keystroke).
    """

    layer_change_requested    = pyqtSignal(str, int)       # comp_id, new_layer
    geometry_change_requested = pyqtSignal(str, str, int)  # comp_id, field, value_dbu

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PROPERTIES_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._current_comp_id: Optional[str] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        content = QWidget()
        content.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                              Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        cl.setSpacing(2)

        # ── Identity ──────────────────────────────────────────────────────────
        cl.addWidget(SectionLabel("Identity"))
        self._row_id   = ValueRow("ID")
        self._row_kind = ValueRow("Kind")
        cl.addWidget(self._row_id)
        cl.addWidget(self._row_kind)

        # Layer — editable spinbox
        layer_row = QWidget()
        lr = QHBoxLayout(layer_row)
        lr.setContentsMargins(0, 2, 0, 2)
        lr.setSpacing(8)
        lbl = QLabel("Layer")
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)
        self._layer_spin = QSpinBox()
        self._layer_spin.setRange(0, 63)
        self._layer_spin.setEnabled(False)
        self._layer_spin.setFixedWidth(72)
        self._layer_spin.valueChanged.connect(self._on_layer_changed)
        lr.addWidget(lbl)
        lr.addWidget(self._layer_spin)
        lr.addStretch()
        cl.addWidget(layer_row)

        cl.addWidget(Separator())

        # ── Geometry ──────────────────────────────────────────────────────────
        cl.addWidget(SectionLabel("Geometry"))
        self._row_x = ValueRow("X origin")
        self._row_y = ValueRow("Y origin")
        cl.addWidget(self._row_x)
        cl.addWidget(self._row_y)

        # Width — editable spinbox, rect only
        _, self._row_w_spin = self._make_dim_row("Width", "width", cl)

        # Height — editable spinbox, rect only
        _, self._row_h_spin = self._make_dim_row("Height", "height", cl)

        # Vertices — read-only, polygon/path only
        self._row_verts = ValueRow("Vertices")
        cl.addWidget(self._row_verts)

        # Path width — editable spinbox, path only
        _, self._row_pw_spin = self._make_dim_row("Path width", "path_width", cl)

        cl.addWidget(Separator())

        # ── Bounding box ──────────────────────────────────────────────────────
        cl.addWidget(SectionLabel("Bounding Box"))
        self._row_bbox = ValueRow("Extents")
        cl.addWidget(self._row_bbox)

        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root.addWidget(scroll)

        self.clear()

    # ── Dim row factory ───────────────────────────────────────────────────────

    def _make_dim_row(self, label_text: str, field: str, layout) -> tuple:
        """
        Build a label + QDoubleSpinBox row, add it to layout, return (row_widget, spinbox).
        The spinbox emits geometry_change_requested on editingFinished (Enter or focus-out),
        not on every keystroke — so the undo stack stays clean.
        """
        row = QWidget()
        hl  = QHBoxLayout(row)
        hl.setContentsMargins(0, 2, 0, 2)
        hl.setSpacing(8)

        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)

        sb = QDoubleSpinBox()
        sb.setRange(0.001, 10_000.0)   # µm: 1 nm minimum, 10 mm maximum
        sb.setDecimals(3)
        sb.setSuffix(" µm")
        sb.setSingleStep(0.5)
        sb.setFixedWidth(110)
        sb.setEnabled(False)
        sb.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_SM}px;
                padding: 3px 6px;
            }}
            QDoubleSpinBox:enabled:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; }}
        """)
        sb.editingFinished.connect(lambda f=field, s=sb: self._on_dim_changed(f, s))

        hl.addWidget(lbl)
        hl.addWidget(sb)
        hl.addStretch()
        layout.addWidget(row)
        return row, sb

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._current_comp_id = None
        for sb in (self._layer_spin, self._row_w_spin, self._row_h_spin, self._row_pw_spin):
            sb.blockSignals(True)
            sb.setValue(0)
            sb.setEnabled(False)
            sb.blockSignals(False)
        for r in [self._row_id, self._row_kind, self._row_x, self._row_y,
                  self._row_verts, self._row_bbox]:
            r.set_value("—")

    def show_component(self, comp: GDSComponent) -> None:
        self._current_comp_id = comp.id
        bb = comp.bbox

        self._row_id.set_value(comp.id)
        self._row_kind.set_value(comp.kind.name.capitalize())

        # Layer spinbox
        self._layer_spin.blockSignals(True)
        self._layer_spin.setValue(comp.layer)
        self._layer_spin.setEnabled(True)
        self._layer_spin.blockSignals(False)

        # Origin (read-only — move via drag)
        self._row_x.set_value(f"{dbu_to_um(comp.origin.x):.3f} µm")
        self._row_y.set_value(f"{dbu_to_um(comp.origin.y):.3f} µm")

        # Width / Height — rectangle only
        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(True)
        is_rect = comp.kind == ComponentKind.RECTANGLE
        self._row_w_spin.setEnabled(is_rect)
        self._row_h_spin.setEnabled(is_rect)
        if is_rect:
            self._row_w_spin.setValue(dbu_to_um(comp.width))
            self._row_h_spin.setValue(dbu_to_um(comp.height))
        else:
            self._row_w_spin.setValue(0.0)
            self._row_h_spin.setValue(0.0)
        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(False)

        # Vertices — polygon and path only
        if comp.kind in (ComponentKind.POLYGON, ComponentKind.PATH):
            self._row_verts.set_value(str(comp.vertex_count))
        else:
            self._row_verts.set_value("—")

        # Path width — path only
        self._row_pw_spin.blockSignals(True)
        is_path = comp.kind == ComponentKind.PATH
        self._row_pw_spin.setEnabled(is_path)
        if is_path:
            self._row_pw_spin.setValue(dbu_to_um(comp.path_width or 0))
        else:
            self._row_pw_spin.setValue(0.0)
        self._row_pw_spin.blockSignals(False)

        # Bounding box
        self._row_bbox.set_value(
            f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})"
        )

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_layer_changed(self, value: int) -> None:
        if self._current_comp_id:
            self.layer_change_requested.emit(self._current_comp_id, value)

    def _on_dim_changed(self, field: str, spinbox: QDoubleSpinBox) -> None:
        if self._current_comp_id:
            value_dbu = um_to_dbu(spinbox.value())
            self.geometry_change_requested.emit(self._current_comp_id, field, value_dbu)