"""
ui/panels.py — Dock panel widgets: Component Palette and Properties Panel.
"""

from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QSpinBox, QDoubleSpinBox, QComboBox,
    QFrame, QPushButton, QSizePolicy, QListWidget,
    QListWidgetItem, QScrollArea, QGridLayout,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QColor, QPalette, QFont, QIcon, QPixmap, QPainter

from ui.theme import Colors, Fonts, Geometry
from core.model import GDSComponent, ComponentKind, dbu_to_um


# ── Helper widgets ────────────────────────────────────────────────────────────

class SectionLabel(QLabel):
    """Small uppercase section header used inside panels."""
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
        lbl.setFixedWidth(80)

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
    """Generate a small colored square icon for a layer."""
    color = QColor(Colors.LAYER_COLORS[layer % len(Colors.LAYER_COLORS)])
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    color.setAlpha(180)
    painter.setPen(color.lighter(150))
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 2, 2)
    painter.end()
    return QIcon(pix)


# ── Component Palette ─────────────────────────────────────────────────────────

class ComponentPalette(QWidget):
    """
    Left-side dock: layer selector + shape buttons.
    Emits place_requested(kind, layer) when user clicks a shape.
    """

    place_requested = pyqtSignal(int, int)   # kind (ComponentKind), layer

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

    # ── Layer section ─────────────────────────────────────────────────────────

    def _build_layer_section(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                                   Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        layout.setSpacing(4)

        layout.addWidget(SectionLabel("Active Layer"))

        self._layer_combo = QComboBox()
        for i in range(8):
            self._layer_combo.addItem(_layer_icon(i), f"Layer {i}", i)
        self._layer_combo.setCurrentIndex(0)
        self._layer_combo.setStyleSheet(f"""
            QComboBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 5px;
                padding: 8px 12px;
                font-size: {Fonts.SIZE_SM}px;
                color: {Colors.TEXT_PRIMARY};
                min-height: 36px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 24px;
            }}
        """)
        layout.addWidget(self._layer_combo)
        return w

    @property
    def active_layer(self) -> int:
        return self._layer_combo.currentData()

    # ── Shapes section ────────────────────────────────────────────────────────

    def _build_shapes_section(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                                   Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        layout.setSpacing(6)

        layout.addWidget(SectionLabel("Shapes"))

        shapes = [
            (ComponentKind.RECTANGLE, "▭  Rectangle",  "10 × 5 µm"),
            (ComponentKind.POLYGON,   "⬠  Polygon",    "Coming Phase 2"),
            (ComponentKind.PATH,      "╌  Path",        "Coming Phase 2"),
        ]

        for kind, label, sub in shapes:
            btn = self._make_shape_button(kind, label, sub)
            layout.addWidget(btn)

        return w

    def _make_shape_button(self, kind: ComponentKind, label: str, sub: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedHeight(48)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        enabled = kind == ComponentKind.RECTANGLE

        inner = QVBoxLayout(btn)
        inner.setContentsMargins(12, 6, 12, 6)
        inner.setSpacing(1)

        top = QLabel(label)
        top.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY if enabled else Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_SM}px; background: transparent;"
        )
        bot = QLabel(sub)
        bot.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; background: transparent;"
        )
        inner.addWidget(top)
        inner.addWidget(bot)

        base_style = f"""
            QPushButton {{
                background: {Colors.BG_ELEVATED if enabled else Colors.BG_SURFACE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 6px;
                text-align: left;
            }}
        """
        hover_style = f"""
            QPushButton:hover {{
                background: {Colors.BG_OVERLAY};
                border-color: {Colors.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {Colors.ACCENT_GLOW};
                border-color: {Colors.ACCENT};
            }}
        """
        btn.setStyleSheet(base_style + (hover_style if enabled else ""))
        btn.setEnabled(enabled)

        if enabled:
            btn.clicked.connect(lambda _, k=kind: self.place_requested.emit(k, self.active_layer))

        return btn


# ── Properties Panel ──────────────────────────────────────────────────────────

class PropertiesPanel(QWidget):
    """
    Right-side dock: shows selected component properties.
    Read-only in Phase 1; editable fields come in Phase 2.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PROPERTIES_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._content = QWidget()
        self._content.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                                           Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        content_layout.setSpacing(2)

        # Identity
        content_layout.addWidget(SectionLabel("Identity"))
        self._row_id    = ValueRow("ID")
        self._row_kind  = ValueRow("Kind")
        self._row_layer = ValueRow("Layer")
        for r in [self._row_id, self._row_kind, self._row_layer]:
            content_layout.addWidget(r)

        content_layout.addWidget(Separator())

        # Geometry
        content_layout.addWidget(SectionLabel("Geometry"))
        self._row_x  = ValueRow("X origin")
        self._row_y  = ValueRow("Y origin")
        self._row_w  = ValueRow("Width")
        self._row_h  = ValueRow("Height")
        for r in [self._row_x, self._row_y, self._row_w, self._row_h]:
            content_layout.addWidget(r)

        content_layout.addWidget(Separator())

        # BBox
        content_layout.addWidget(SectionLabel("Bounding Box"))
        self._row_bbox = ValueRow("Extents")
        content_layout.addWidget(self._row_bbox)

        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root.addWidget(scroll)

        self.clear()

    def clear(self) -> None:
        for row in [self._row_id, self._row_kind, self._row_layer,
                    self._row_x, self._row_y, self._row_w, self._row_h,
                    self._row_bbox]:
            row.set_value("—")

    def show_component(self, comp: GDSComponent) -> None:
        bb = comp.bbox
        self._row_id.set_value(comp.id)
        self._row_kind.set_value(comp.kind.name.capitalize())
        self._row_layer.set_value(str(comp.layer))
        self._row_x.set_value(f"{dbu_to_um(comp.origin.x):.3f} µm")
        self._row_y.set_value(f"{dbu_to_um(comp.origin.y):.3f} µm")
        self._row_w.set_value(f"{dbu_to_um(comp.width):.3f} µm")
        self._row_h.set_value(f"{dbu_to_um(comp.height):.3f} µm")
        self._row_bbox.set_value(
            f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})"
        )
