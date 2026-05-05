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
from core.model import GDSComponent, ComponentKind, PortSide, dbu_to_um, um_to_dbu
from core.cell_library import CELL_BY_ID


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

class MemberCard(QWidget):
    """
    Compact editable card for one group member shown inside the group view.
    Emits the same signals as PropertiesPanel so MainWindow needs no changes.
    """
    layer_change_requested    = pyqtSignal(str, int)
    geometry_change_requested = pyqtSignal(str, str, int)

    def __init__(self, comp: GDSComponent, design, parent=None) -> None:
        super().__init__(parent)
        self._comp   = comp
        self._design = design
        self.setStyleSheet(f"""
            QWidget {{
                background: {Colors.BG_ELEVATED};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 4px;
            }}
        """)
        self._build(comp, design)

    def _build(self, comp: GDSComponent, design) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)

        # ── Header: kind + id + layer colour swatch ───────────────────────────
        header = QHBoxLayout()
        swatch = QLabel("█")
        color  = Colors.LAYER_COLORS[comp.layer % len(Colors.LAYER_COLORS)]
        swatch.setStyleSheet(
            f"color: {color}; font-size: 14px; background: transparent; border: none;"
        )
        kind_lbl = QLabel(f"{comp.kind.name.capitalize()}  ·  {comp.id}")
        kind_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_SM}px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        header.addWidget(swatch)
        header.addWidget(kind_lbl)
        header.addStretch()
        lay.addLayout(header)

        # ── Inline rows ───────────────────────────────────────────────────────
        def inline(label: str, value: str) -> None:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                f"background: transparent; border: none;"
            )
            lbl.setFixedWidth(72)
            val = QLabel(value)
            val.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_XS}px; "
                f"font-family: {Fonts.MONO_FAMILY}; background: transparent; border: none;"
            )
            row.addWidget(lbl); row.addWidget(val); row.addStretch()
            lay.addLayout(row)

        bb = comp.bbox
        inline("Origin", f"{dbu_to_um(comp.origin.x):.2f}, {dbu_to_um(comp.origin.y):.2f} µm")
        inline("BBox",
               f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f}) → "
               f"({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})")

        # Connections
        if design is not None:
            sides = design.connected_sides(comp.id)
            if sides:
                order  = [PortSide.NORTH, PortSide.SOUTH, PortSide.EAST, PortSide.WEST]
                labels = {PortSide.NORTH:"N", PortSide.SOUTH:"S",
                          PortSide.EAST:"E",  PortSide.WEST:"W"}
                inline("Connected",
                       "  ·  ".join(labels[s] for s in order if s in sides))

        # ── Editable fields ───────────────────────────────────────────────────
        spin_style = f"""
            QDoubleSpinBox, QSpinBox {{
                background: {Colors.BG_BASE}; border: 1px solid {Colors.BG_BORDER};
                border-radius: 3px; color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_XS}px; padding: 2px 4px;
            }}
            QDoubleSpinBox:hover, QSpinBox:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
            QSpinBox::up-button,       QSpinBox::down-button {{ width: 14px; }}
        """

        def spin_row(label: str, widget) -> None:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                f"background: transparent; border: none;"
            )
            lbl.setFixedWidth(72)
            widget.setFixedWidth(100)
            widget.setStyleSheet(spin_style)
            row.addWidget(lbl); row.addWidget(widget); row.addStretch()
            lay.addLayout(row)

        # Layer
        layer_sb = QSpinBox()
        layer_sb.setRange(0, 63)
        layer_sb.setValue(comp.layer)
        layer_sb.valueChanged.connect(
            lambda v, cid=comp.id: self.layer_change_requested.emit(cid, v)
        )
        spin_row("Layer", layer_sb)

        # Kind-specific editable fields
        if comp.kind == ComponentKind.RECTANGLE:
            w_sb = QDoubleSpinBox()
            w_sb.setRange(0.001, 10_000); w_sb.setDecimals(3)
            w_sb.setSuffix(" µm"); w_sb.setValue(dbu_to_um(comp.width))
            w_sb.editingFinished.connect(
                lambda s=w_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "width", um_to_dbu(s.value()))
            )
            spin_row("Width", w_sb)

            h_sb = QDoubleSpinBox()
            h_sb.setRange(0.001, 10_000); h_sb.setDecimals(3)
            h_sb.setSuffix(" µm"); h_sb.setValue(dbu_to_um(comp.height))
            h_sb.editingFinished.connect(
                lambda s=h_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "height", um_to_dbu(s.value()))
            )
            spin_row("Height", h_sb)

        elif comp.kind == ComponentKind.PATH and comp.path_width:
            pw_sb = QDoubleSpinBox()
            pw_sb.setRange(0.001, 1_000); pw_sb.setDecimals(3)
            pw_sb.setSuffix(" µm"); pw_sb.setValue(dbu_to_um(comp.path_width))
            pw_sb.editingFinished.connect(
                lambda s=pw_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "path_width", um_to_dbu(s.value()))
            )
            spin_row("Path W", pw_sb)

        elif comp.kind in (ComponentKind.POLYGON, ComponentKind.PATH):
            inline("Vertices", str(comp.vertex_count))


class PropertiesPanel(QWidget):
    """
    Right dock — two modes:
      • Single component selected  → existing single-component view (unchanged)
      • Group selected             → group view: header + scrollable MemberCard list
    """

    layer_change_requested    = pyqtSignal(str, int)
    geometry_change_requested = pyqtSignal(str, str, int)
    # Emitted when a cell-group parameter spinbox changes:
    # (group_id, cell_id, param_key, new_value_as_float_or_str)
    cell_param_change_requested = pyqtSignal(str, str, str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PROPERTIES_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._current_comp_id: Optional[str] = None
        self._current_group_id: Optional[str] = None
        self._cell_params_widget = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Stack: page 0 = single component, page 1 = group, page 2 = multi ─
        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_single_page())
        self._stack.addWidget(self._build_group_page())
        self._stack.addWidget(self._build_multi_page())
        root.addWidget(self._stack)

        self.clear()

    # ── Single-component page (identical to original) ─────────────────────────

    def _build_single_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                              Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        cl.setSpacing(2)

        cl.addWidget(SectionLabel("Identity"))
        self._row_id   = ValueRow("ID")
        self._row_kind = ValueRow("Kind")
        cl.addWidget(self._row_id)
        cl.addWidget(self._row_kind)

        layer_row = QWidget()
        lr = QHBoxLayout(layer_row)
        lr.setContentsMargins(0, 2, 0, 2); lr.setSpacing(8)
        lbl = QLabel("Layer")
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)
        self._layer_spin = QSpinBox()
        self._layer_spin.setRange(0, 63)
        self._layer_spin.setEnabled(False)
        self._layer_spin.setFixedWidth(72)
        self._layer_spin.valueChanged.connect(self._on_layer_changed)
        lr.addWidget(lbl); lr.addWidget(self._layer_spin); lr.addStretch()
        cl.addWidget(layer_row)
        cl.addWidget(Separator())

        cl.addWidget(SectionLabel("Geometry"))
        self._row_x = ValueRow("X origin")
        self._row_y = ValueRow("Y origin")
        cl.addWidget(self._row_x); cl.addWidget(self._row_y)
        _, self._row_w_spin  = self._make_dim_row("Width",      "width",      cl)
        _, self._row_h_spin  = self._make_dim_row("Height",     "height",     cl)
        self._row_verts      = ValueRow("Vertices")
        cl.addWidget(self._row_verts)
        _, self._row_pw_spin = self._make_dim_row("Path width", "path_width", cl)
        cl.addWidget(Separator())

        cl.addWidget(SectionLabel("Bounding Box"))
        self._row_bbox = ValueRow("Extents")
        cl.addWidget(self._row_bbox)

        cl.addWidget(Separator())
        cl.addWidget(SectionLabel("Connections"))
        self._row_connections = ValueRow("Connected")
        cl.addWidget(self._row_connections)

        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root.addWidget(scroll)
        return page

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

    # ── Group page ────────────────────────────────────────────────────────────

    def _build_group_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Fixed header
        self._grp_header = QWidget()
        self._grp_header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(self._grp_header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(3)
        self._grp_name_lbl  = QLabel("Group")
        self._grp_name_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 14px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        self._grp_meta_lbl  = QLabel("")
        self._grp_meta_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        self._grp_bbox_lbl  = QLabel("")
        self._grp_bbox_lbl.setStyleSheet(self._grp_meta_lbl.styleSheet())
        hl.addWidget(self._grp_name_lbl)
        hl.addWidget(self._grp_meta_lbl)
        hl.addWidget(self._grp_bbox_lbl)
        root.addWidget(self._grp_header)

        # Scrollable member cards
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        self._cards_container = QWidget()
        self._cards_container.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch()

        self._cards_scroll.setWidget(self._cards_container)
        root.addWidget(self._cards_scroll)
        return page

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._stack.setCurrentIndex(0)
        self._current_comp_id  = None
        self._current_group_id = None
        if hasattr(self, "_cell_params_widget") and self._cell_params_widget is not None:
            self._cell_params_widget.hide()
            self._cell_params_widget.setParent(None)
            self._cell_params_widget.deleteLater()
            self._cell_params_widget = None
        for sb in (self._layer_spin, self._row_w_spin,
                   self._row_h_spin, self._row_pw_spin):
            sb.blockSignals(True); sb.setValue(0)
            sb.setEnabled(False);  sb.blockSignals(False)
        for r in [self._row_id, self._row_kind, self._row_x, self._row_y,
                  self._row_verts, self._row_bbox, self._row_connections]:
            r.set_value("—")

    def show_component(self, comp: GDSComponent, design=None) -> None:
        self._stack.setCurrentIndex(0)
        self._current_comp_id = comp.id
        bb = comp.bbox

        self._row_id.set_value(comp.id)
        self._row_kind.set_value(comp.kind.name.capitalize())

        self._layer_spin.blockSignals(True)
        self._layer_spin.setValue(comp.layer)
        self._layer_spin.setEnabled(True)
        self._layer_spin.blockSignals(False)

        self._row_x.set_value(f"{dbu_to_um(comp.origin.x):.3f} µm")
        self._row_y.set_value(f"{dbu_to_um(comp.origin.y):.3f} µm")

        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(True)
        is_rect = comp.kind == ComponentKind.RECTANGLE
        self._row_w_spin.setEnabled(is_rect)
        self._row_h_spin.setEnabled(is_rect)
        if is_rect:
            self._row_w_spin.setValue(dbu_to_um(comp.width))
            self._row_h_spin.setValue(dbu_to_um(comp.height))
        else:
            self._row_w_spin.setValue(0.0); self._row_h_spin.setValue(0.0)
        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(False)

        if comp.kind in (ComponentKind.POLYGON, ComponentKind.PATH):
            self._row_verts.set_value(str(comp.vertex_count))
        else:
            self._row_verts.set_value("—")

        self._row_pw_spin.blockSignals(True)
        is_path = comp.kind == ComponentKind.PATH
        self._row_pw_spin.setEnabled(is_path)
        self._row_pw_spin.setValue(
            dbu_to_um(comp.path_width or 0) if is_path else 0.0
        )
        self._row_pw_spin.blockSignals(False)

        self._row_bbox.set_value(
            f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})"
        )

        if design is not None:
            sides = design.connected_sides(comp.id)
            if sides:
                order  = [PortSide.NORTH, PortSide.SOUTH,
                          PortSide.EAST,  PortSide.WEST]
                labels = {PortSide.NORTH:"N", PortSide.SOUTH:"S",
                          PortSide.EAST:"E",  PortSide.WEST:"W"}
                self._row_connections.set_value(
                    "  ·  ".join(labels[s] for s in order if s in sides)
                )
            else:
                self._row_connections.set_value("None")
        else:
            self._row_connections.set_value("—")

    def show_group(self, group, design) -> None:
        """Switch to group view and populate member cards + optional cell params."""
        self._stack.setCurrentIndex(1)
        self._current_comp_id = None
        self._current_group_id = group.id

        # Header
        members = [design.get(cid) for cid in group.member_ids
                   if design.get(cid)]
        bb = group.bbox_from(design.components)

        self._grp_name_lbl.setText(f"⬡  {group.name}")
        self._grp_meta_lbl.setText(
            f"{len(members)} component{'s' if len(members) != 1 else ''}  ·  "
            f"id {group.id}"
        )
        self._grp_bbox_lbl.setText(
            f"BBox  ({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f}) µm"
        )

        # ── Cell params section (shown when group matches a catalogue cell) ───
        self._rebuild_cell_params(group)

        # Rebuild member cards
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for comp in members:
            card = MemberCard(comp, design)
            card.layer_change_requested.connect(self.layer_change_requested)
            card.geometry_change_requested.connect(self.geometry_change_requested)
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, card
            )

    def _rebuild_cell_params(self, group) -> None:
        """
        If the group was created by a catalogue cell (group.cell_id is set),
        render an editable parameter section above the member cards.
        Otherwise hides the section cleanly.
        """
        # Clear any existing params widget.
        # IMPORTANT: hide() before setParent(None) — otherwise Qt briefly
        # promotes the widget to a top-level window between reparenting and
        # deleteLater(), which causes a visible flash/popup on every selection.
        if hasattr(self, "_cell_params_widget") and self._cell_params_widget is not None:
            self._cell_params_widget.hide()
            self._cell_params_widget.setParent(None)
            self._cell_params_widget.deleteLater()
            self._cell_params_widget = None

        # Match via the cell_id attribute stamped by PlaceCellCommand
        cell_id = getattr(group, "cell_id", None)
        if not cell_id:
            return
        matched_cdef = CELL_BY_ID.get(cell_id)
        if matched_cdef is None:
            return

        # Current param values: start from catalogue defaults, overlay stored overrides
        current_params = dict(matched_cdef.defaults)
        stored = getattr(group, "_cell_params", {})
        current_params.update(stored)

        # ── Build the params widget ───────────────────────────────────────────
        w = QWidget()
        w.setStyleSheet(f"""
            QWidget {{
                background: {Colors.BG_ELEVATED};
                border-bottom: 1px solid {Colors.BG_BORDER};
            }}
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, 10,
                               Geometry.PANEL_PADDING, 10)
        lay.setSpacing(6)

        header_lbl = QLabel("Cell Parameters")
        header_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_SM}px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        hint_lbl = QLabel("Edit then re-place to apply")
        hint_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(header_lbl)
        lay.addWidget(hint_lbl)

        spin_style = f"""
            QDoubleSpinBox, QSpinBox, QComboBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 3px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_XS}px;
                padding: 2px 4px;
            }}
            QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 14px; }}
        """

        _STRING_OPTIONS: dict[str, list[str]] = {
            "cap_style":      ["top", "side"],
            "undercut_style": ["right", "top"],
        }

        group_id  = group.id
        cell_id   = matched_cdef.cell_id

        for key, default in matched_cdef.defaults.items():
            current_val = current_params.get(key, default)
            row = QHBoxLayout()
            lbl = QLabel(key.replace("_", " "))
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                f"background: transparent; border: none;"
            )
            lbl.setFixedWidth(90)

            if isinstance(default, float):
                sb = QDoubleSpinBox()
                sb.setDecimals(3)
                sb.setRange(0.001, 1000.0)
                sb.setSingleStep(0.1)
                sb.setSuffix(" µm")
                sb.setValue(current_val)
                sb.setFixedWidth(110)
                sb.setStyleSheet(spin_style)
                sb.editingFinished.connect(
                    lambda _k=key, _sb=sb, _gid=group_id, _cid=cell_id:
                        self.cell_param_change_requested.emit(_gid, _cid, _k, _sb.value())
                )
                row.addWidget(lbl)
                row.addWidget(sb)
                row.addStretch()

            elif isinstance(default, str):
                options = _STRING_OPTIONS.get(key)
                if options:
                    combo = QComboBox()
                    combo.addItems(options)
                    combo.setCurrentText(str(current_val))
                    combo.setFixedWidth(110)
                    combo.setStyleSheet(spin_style)
                    # NoFocus prevents the combo from grabbing focus when the
                    # panel is rebuilt on selection change, which was causing
                    # the dropdown to pop open immediately on every cell click.
                    combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    combo.currentTextChanged.connect(
                        lambda val, _k=key, _gid=group_id, _cid=cell_id:
                            self.cell_param_change_requested.emit(_gid, _cid, _k, val)
                    )
                    row.addWidget(lbl)
                    row.addWidget(combo)
                    row.addStretch()
                else:
                    val_lbl = QLabel(str(default))
                    val_lbl.setStyleSheet(
                        f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_XS}px; "
                        f"background: transparent; border: none;"
                    )
                    row.addWidget(lbl)
                    row.addWidget(val_lbl)
                    row.addStretch()

            lay.addLayout(row)

        self._cell_params_widget = w
        # Insert above the scrollable cards area (index 1 = after the fixed header)
        # The group page root layout is: [0]=grp_header  [1]=cards_scroll
        grp_page = self._stack.widget(1)
        grp_page.layout().insertWidget(1, w)

    # ── Multi-selection page ──────────────────────────────────────────────────

    def _build_multi_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(3)
        self._multi_title = QLabel("Multiple Selected")
        self._multi_title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 14px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        self._multi_subtitle = QLabel("")
        self._multi_subtitle.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        hl.addWidget(self._multi_title)
        hl.addWidget(self._multi_subtitle)
        root.addWidget(header)

        self._multi_scroll = QScrollArea()
        self._multi_scroll.setWidgetResizable(True)
        self._multi_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._multi_scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        self._multi_container = QWidget()
        self._multi_container.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._multi_layout = QVBoxLayout(self._multi_container)
        self._multi_layout.setContentsMargins(
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        self._multi_layout.setSpacing(6)
        self._multi_layout.addStretch()

        self._multi_scroll.setWidget(self._multi_container)
        root.addWidget(self._multi_scroll)
        return page

    def show_multi_selection(self, components: list, design=None) -> None:
        """Switch to multi-select page and show a compact card per component."""
        self._stack.setCurrentIndex(2)
        self._current_comp_id = None

        n = len(components)
        self._multi_title.setText(f"{n} Components Selected")
        layers = sorted({c.layer for c in components})
        self._multi_subtitle.setText(
            f"Layers: {', '.join(f'L{l}' for l in layers)}"
        )

        while self._multi_layout.count() > 1:
            item = self._multi_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for comp in components:
            card = MemberCard(comp, design)
            card.layer_change_requested.connect(self.layer_change_requested)
            card.geometry_change_requested.connect(self.geometry_change_requested)
            self._multi_layout.insertWidget(self._multi_layout.count() - 1, card)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_layer_changed(self, value: int) -> None:
        if self._current_comp_id:
            self.layer_change_requested.emit(self._current_comp_id, value)

    def _on_dim_changed(self, field: str, spinbox: QDoubleSpinBox) -> None:
        if self._current_comp_id:
            self.geometry_change_requested.emit(
                self._current_comp_id, field, um_to_dbu(spinbox.value())
            )