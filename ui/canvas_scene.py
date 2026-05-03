"""
ui/canvas_scene.py — QGraphicsScene subclass.

Responsibilities:
  - Owns the mapping between GDSComponent model objects and QGraphicsItems
  - Handles mouse events for placement and selection
  - Enforces snap-to-grid in DBU coordinates
  - Emits signals for status bar / property panel updates

Coordinate conventions:
  - Scene coordinates = screen pixels at zoom 1× (QGraphicsScene units)
  - We use 1 scene unit = 1 DBU (nm). The view scales via QGraphicsView.
  - Y-axis is INVERTED vs GDS: screen Y increases downward, GDS Y upward.
    The export layer handles the flip; the canvas does NOT flip internally
    so Qt's built-in geometry stays sane.
"""

from __future__ import annotations

from typing import Optional, Dict
import math

from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsItem, QGraphicsRectItem,
    QGraphicsLineItem, QGraphicsEllipseItem,
)
from PyQt6.QtGui import (
    QPen, QBrush, QColor, QPainter, QFont,
    QFontMetrics,
)
from PyQt6.QtCore import (
    Qt, QRectF, QPointF, QLineF, pyqtSignal, QObject,
)

from core.model import (
    DesignScene, GDSComponent, ComponentKind,
    Point, dbu_to_um, um_to_dbu,
)
from core.commands import CommandStack, AddComponent, MoveComponent
from ui.theme import Colors


# ── Constants ─────────────────────────────────────────────────────────────────

GRID_MINOR_DBU = um_to_dbu(1)    # 1 µm minor grid
GRID_MAJOR_DBU = um_to_dbu(10)   # 10 µm major grid
SCENE_EXTENT   = um_to_dbu(5000) # ±5 mm canvas extent


# ── Component Graphics Item ───────────────────────────────────────────────────

class ComponentItem(QGraphicsRectItem):
    """
    Visual representation of a GDSComponent on the canvas.

    Wraps a QGraphicsRectItem with:
      - Layer-based color
      - Selection highlight
      - Drag-move support with snap-to-grid
      - Hover effects
    """

    def __init__(self, component: GDSComponent, scene_ref: "CanvasScene") -> None:
        self._comp       = component
        self._scene_ref  = scene_ref
        self._drag_start: Optional[QPointF] = None
        self._orig_pos:   Optional[Point]   = None

        rect = self._comp_to_rect(component)
        super().__init__(rect)

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges,
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._apply_style(selected=False, hovered=False)

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self, selected: bool, hovered: bool) -> None:
        layer_hex = Colors.LAYER_COLORS[self._comp.layer % len(Colors.LAYER_COLORS)]
        base_color = QColor(layer_hex)

        if selected:
            fill = QColor(base_color)
            fill.setAlpha(90)
            pen_color = QColor(Colors.ACCENT)
            pen_width = 1.5
        elif hovered:
            fill = QColor(base_color)
            fill.setAlpha(60)
            pen_color = base_color.lighter(150)
            pen_width = 1.0
        else:
            fill = QColor(base_color)
            fill.setAlpha(40)
            pen_color = base_color
            pen_width = 0.8

        pen = QPen(pen_color, pen_width)
        pen.setCosmetic(True)  # pen width stays constant regardless of zoom
        self.setPen(pen)
        self.setBrush(QBrush(fill))

    # ── Geometry helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _comp_to_rect(comp: GDSComponent) -> QRectF:
        return QRectF(
            comp.origin.x,
            comp.origin.y,
            comp.width,
            comp.height,
        )

    def sync_from_model(self) -> None:
        """Re-read geometry from the model (after undo/redo)."""
        self.setRect(self._comp_to_rect(self._comp))

    @property
    def component(self) -> GDSComponent:
        return self._comp

    # ── Events ────────────────────────────────────────────────────────────────

    def hoverEnterEvent(self, event) -> None:
        self._apply_style(self.isSelected(), hovered=True)
        self._scene_ref.item_hovered.emit(self._comp.id)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._apply_style(self.isSelected(), hovered=False)
        self._scene_ref.item_hovered.emit("")
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.scenePos()
            self._orig_pos   = Point(self._comp.origin.x, self._comp.origin.y)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_start is not None
            and self._orig_pos is not None
        ):
            new_origin = Point(
                int(round(self.rect().x() + self.pos().x())),
                int(round(self.rect().y() + self.pos().y())),
            )
            new_snapped = self._scene_ref.snap(new_origin)

            if new_snapped != self._orig_pos:
                # Push a MoveComponent command onto the undo stack
                cmd = MoveComponent(
                    comp_id    = self._comp.id,
                    old_origin = self._orig_pos,
                    new_origin = new_snapped,
                )
                # Execute without going through the stack a second time
                # (Qt has already moved the QGraphicsItem visually)
                self._comp.origin = new_snapped
                self._scene_ref.cmd_stack._undo_stack.append(cmd)
                self._scene_ref.cmd_stack._redo_stack.clear()
                self._scene_ref.cmd_stack._on_change()

            # Sync rect to reflect snapped position
            self.setPos(0, 0)
            self.setRect(self._comp_to_rect(self._comp))
            self._drag_start = None
            self._orig_pos   = None

        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if (
            change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged
        ):
            self._apply_style(bool(value), hovered=False)
            if bool(value):
                self._scene_ref.item_selected.emit(self._comp.id)
        return super().itemChange(change, value)


# ── Canvas Scene ──────────────────────────────────────────────────────────────

class _Signals(QObject):
    item_selected = pyqtSignal(str)
    item_hovered  = pyqtSignal(str)
    cursor_moved  = pyqtSignal(float, float)   # µm coords
    scene_changed = pyqtSignal()


class CanvasScene(QGraphicsScene):
    """
    Master scene.  Owns all ComponentItems and the CommandStack.
    """

    # Re-expose signals as class attributes so the view can connect to them
    item_selected = pyqtSignal(str)
    item_hovered  = pyqtSignal(str)
    cursor_moved  = pyqtSignal(float, float)
    scene_changed = pyqtSignal()

    def __init__(self, design: DesignScene, parent=None) -> None:
        super().__init__(parent)

        self._design   = design
        self._items:    Dict[str, ComponentItem] = {}

        # Wire up the command stack to redraw on change
        self.cmd_stack = CommandStack(design, on_change=self._on_model_changed)

        # Set scene rect to ±5 mm
        ext = SCENE_EXTENT
        self.setSceneRect(-ext, -ext, ext * 2, ext * 2)

        self.setBackgroundBrush(QBrush(QColor(Colors.CANVAS_BG)))

    # ── Public API ────────────────────────────────────────────────────────────

    def place_rectangle(
        self,
        origin_x_um: float,
        origin_y_um: float,
        width_um: float,
        height_um: float,
        layer: int = 0,
    ) -> GDSComponent:
        """Create and add a rectangle via the command stack."""
        comp = GDSComponent(
            kind   = ComponentKind.RECTANGLE,
            layer  = layer,
            origin = Point.from_um(origin_x_um, origin_y_um),
            width  = um_to_dbu(width_um),
            height = um_to_dbu(height_um),
        )
        self.cmd_stack.execute(AddComponent(comp))
        return comp

    def snap(self, pt: Point) -> Point:
        """Snap a point to the minor grid."""
        g = GRID_MINOR_DBU
        return Point(
            round(pt.x / g) * g,
            round(pt.y / g) * g,
        )

    def snap_f(self, x: float, y: float) -> QPointF:
        """Snap scene (float) coords to grid, return QPointF."""
        g = float(GRID_MINOR_DBU)
        return QPointF(round(x / g) * g, round(y / g) * g)

    # ── Drawing ───────────────────────────────────────────────────────────────

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        self._draw_grid(painter, rect)
        self._draw_origin(painter)

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        # Determine visible extent
        left   = int(math.floor(rect.left()   / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        top    = int(math.floor(rect.top()    / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        right  = int(math.ceil(rect.right()   / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        bottom = int(math.ceil(rect.bottom()  / GRID_MINOR_DBU)) * GRID_MINOR_DBU

        # Cull if grid is too dense to see (less than 4px per cell on screen)
        # (the view's transform is not directly available here, so we skip
        # density culling in Phase 1 — handled via zoom limits instead)

        minor_pen = QPen(QColor(Colors.GRID_MINOR))
        minor_pen.setCosmetic(True)
        minor_pen.setWidthF(0.5)

        major_pen = QPen(QColor(Colors.GRID_MAJOR))
        major_pen.setCosmetic(True)
        major_pen.setWidthF(0.8)

        painter.save()

        x = left
        while x <= right:
            is_major = (x % GRID_MAJOR_DBU == 0)
            painter.setPen(major_pen if is_major else minor_pen)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            x += GRID_MINOR_DBU

        y = top
        while y <= bottom:
            is_major = (y % GRID_MAJOR_DBU == 0)
            painter.setPen(major_pen if is_major else minor_pen)
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            y += GRID_MINOR_DBU

        painter.restore()

    def _draw_origin(self, painter: QPainter) -> None:
        size = GRID_MAJOR_DBU * 3
        pen = QPen(QColor(Colors.GRID_ORIGIN))
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(-size, 0), QPointF(size, 0))
        painter.drawLine(QPointF(0, -size), QPointF(0, size))

    # ── Model sync ────────────────────────────────────────────────────────────

    def _on_model_changed(self) -> None:
        """Reconcile QGraphicsItems with the design model."""
        model_ids = {c.id for c in self._design.components}
        scene_ids = set(self._items.keys())

        # Add new items
        for comp in self._design.components:
            if comp.id not in self._items:
                item = ComponentItem(comp, self)
                self.addItem(item)
                self._items[comp.id] = item

        # Remove deleted items
        for dead_id in scene_ids - model_ids:
            item = self._items.pop(dead_id)
            self.removeItem(item)

        # Sync geometry for moved items
        for comp in self._design.components:
            self._items[comp.id].sync_from_model()

        self.scene_changed.emit()
        self.update()

    # ── Mouse tracking ────────────────────────────────────────────────────────

    def mouseMoveEvent(self, event) -> None:
        pos = event.scenePos()
        self.cursor_moved.emit(
            dbu_to_um(int(pos.x())),
            dbu_to_um(int(pos.y())),
        )
        super().mouseMoveEvent(event)
