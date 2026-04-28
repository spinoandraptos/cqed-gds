"""
canvas.py — GDS canvas: grid, pan/zoom, component placement, port snapping,
            wire drawing, and live polygon rendering.
"""

from __future__ import annotations
import math
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsItem,
    QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsPolygonItem, QGraphicsTextItem,
)
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal, QLineF
from PyQt6.QtGui import (
    QPen, QBrush, QColor, QPainter, QPolygonF, QFont, QCursor,
)

from config import Config
from component_model import (
    ComponentInstance, COMPONENT_TYPES, LAYER_COLORS,
    render_instance, Port,
)

if TYPE_CHECKING:
    pass


# ── Constants ─────────────────────────────────────────────────────────────────

PIXELS_PER_UM = 20.0        # initial scale: 20 px = 1 µm
GRID_UM       = 0.5         # major grid spacing in µm
SNAP_UM       = 0.05         # snap grid in µm
PORT_SNAP_UM  = 0.8         # distance to snap to a port (µm)
PORT_RADIUS   = 4           # visual port dot radius (px)

CANVAS_BG      = QColor("#0d1117")
GRID_COLOR     = QColor(255, 255, 255, 20)
GRID_MAJOR     = QColor(255, 255, 255, 45)
SELECTION_COLOR = QColor("#4fc3f7")
PORT_COLOR      = QColor("#4fc3f7")
PORT_HOVER_COLOR = QColor("#ffffff")
WIRE_PREVIEW_COLOR = QColor("#4fc3f7")


# ── Helpers ───────────────────────────────────────────────────────────────────

def snap(val: float, grid: float) -> float:
    return round(val / grid) * grid


def um_to_px(um: float) -> float:
    return um * PIXELS_PER_UM


def px_to_um(px: float) -> float:
    return px / PIXELS_PER_UM


# ── Port graphics item ────────────────────────────────────────────────────────

class PortItem(QGraphicsEllipseItem):
    def __init__(self, port: Port, owner: "ComponentItem"):
        r = PORT_RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.port  = port
        self.owner = owner
        self.setPos(um_to_px(port.x), -um_to_px(port.y))   # y flipped
        self.setPen(QPen(PORT_COLOR, 1.0))
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        self.setZValue(10)
        self.setAcceptHoverEvents(True)
        self._hovered = False

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.setBrush(QBrush(PORT_HOVER_COLOR))
        self.update()

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        self.update()

    def world_pos(self) -> QPointF:
        return self.mapToScene(QPointF(0, 0))


# ── Component graphics item ───────────────────────────────────────────────────

class ComponentItem(QGraphicsItem):
    """
    Renders a ComponentInstance as a group of filled polygons (one per GDS layer)
    with port dots overlaid.
    """

    def __init__(self, inst: ComponentInstance, cfg: Config,
                 scene: "GDSScene"):
        super().__init__()
        self.inst   = inst
        self.cfg    = cfg
        self._scene = scene
        self._polys: list[tuple[int, list[QPointF]]] = []
        self._port_items: list[PortItem] = []
        self._selected = False
        self._bounding = QRectF(-5, -5, 10, 10)

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges,
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

        # Position in scene (pixels), y-flipped
        self.setPos(um_to_px(inst.x), -um_to_px(inst.y))
        self._rebuild()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _rebuild(self):
        """Re-render GDS polygons and rebuild port items."""
        # Notify Qt that the bounding rect is about to change so it can
        # fully repaint the old area — prevents ghost rendering on rotation.
        self.prepareGeometryChange()

        # Remove old port items from scene
        for pi in self._port_items:
            if pi.scene():
                pi.scene().removeItem(pi)
        self._port_items.clear()

        # Render polygons (in local coords, offset by -inst.x / -inst.y)
        raw = render_instance(self.inst, self.cfg)
        self._polys = []
        all_pts: list[QPointF] = []
        ox, oy = self.inst.x, self.inst.y

        for layer, pts in raw:
            qpts = [QPointF(um_to_px(x - ox), -um_to_px(y - oy)) for x, y in pts]
            self._polys.append((layer, qpts))
            all_pts.extend(qpts)

        # Bounding rect
        if all_pts:
            xs = [p.x() for p in all_pts]
            ys = [p.y() for p in all_pts]
            margin = 4
            self._bounding = QRectF(
                min(xs) - margin, min(ys) - margin,
                max(xs) - min(xs) + 2 * margin,
                max(ys) - min(ys) + 2 * margin,
            )
        else:
            self._bounding = QRectF(-10, -10, 20, 20)

        # Port items
        for port in self.inst.get_ports(self.cfg):
            pi = PortItem(port, self)
            pi.setParentItem(self)
            self._port_items.append(pi)

        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounding

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for layer, qpts in self._polys:
            color_str = LAYER_COLORS.get(layer, "#888888")
            color = QColor(color_str)
            fill  = QColor(color)
            fill.setAlpha(120)
            poly  = QPolygonF(qpts)
            painter.setBrush(QBrush(fill))
            pen_color = QColor(color)
            pen_color.setAlpha(200)
            painter.setPen(QPen(pen_color, 0.8))
            painter.drawPolygon(poly)

        # Selection outline
        if self.isSelected():
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.setPen(QPen(SELECTION_COLOR, 1.5, Qt.PenStyle.DashLine))
            painter.drawRect(self._bounding.adjusted(2, 2, -2, -2))

    # ── Interaction ───────────────────────────────────────────────────────────

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # Sync model position
            px = self.pos().x()
            py = self.pos().y()
            self.inst.x = snap(px_to_um(px), SNAP_UM)
            self.inst.y = snap(-px_to_um(py), SNAP_UM)
            # Snap item to grid
            self.setPos(um_to_px(self.inst.x), -um_to_px(self.inst.y))
            self._scene.component_moved.emit(self.inst.inst_id)
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._rebuild()
        self._scene.selection_changed_signal.emit(self.inst.inst_id)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._scene.selection_changed_signal.emit(self.inst.inst_id)

    def nearest_port(self, scene_pos: QPointF) -> PortItem | None:
        """Return closest port within PORT_SNAP_UM, or None."""
        best_dist = PORT_SNAP_UM * PIXELS_PER_UM
        best: PortItem | None = None
        for pi in self._port_items:
            wp = pi.world_pos()
            d  = math.hypot(scene_pos.x() - wp.x(), scene_pos.y() - wp.y())
            if d < best_dist:
                best_dist = d
                best = pi
        return best


# ── Wire item ─────────────────────────────────────────────────────────────────

class WireItem(QGraphicsItem):
    """An orthogonal wire connecting two ports."""

    def __init__(self, p1: QPointF, p2: QPointF, layer: int = 5):
        super().__init__()
        self.p1    = p1
        self.p2    = p2
        self.layer = layer
        self.setZValue(0.5)

    def set_endpoints(self, p1: QPointF, p2: QPointF):
        self.prepareGeometryChange()
        self.p1, self.p2 = p1, p2
        self.update()

    def boundingRect(self) -> QRectF:
        x1, y1 = self.p1.x(), self.p1.y()
        x2, y2 = self.p2.x(), self.p2.y()
        return QRectF(
            min(x1, x2) - 3, min(y1, y2) - 3,
            abs(x2 - x1) + 6, abs(y2 - y1) + 6,
        )

    def paint(self, painter, option, widget=None):
        color = QColor(LAYER_COLORS.get(self.layer, "#5DCAA5"))
        color.setAlpha(200)
        pen = QPen(color, 1.5)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

        # Orthogonal routing: horizontal then vertical
        mid_x = self.p2.x()
        mid_y = self.p1.y()
        painter.drawLine(self.p1, QPointF(mid_x, mid_y))
        painter.drawLine(QPointF(mid_x, mid_y), self.p2)


# ── Scene ─────────────────────────────────────────────────────────────────────

class GDSScene(QGraphicsScene):
    component_moved       = pyqtSignal(int)   # inst_id
    selection_changed_signal = pyqtSignal(int)
    wire_connected        = pyqtSignal(int, str, int, str)  # id,port,id,port
    status_message        = pyqtSignal(str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setBackgroundBrush(QBrush(CANVAS_BG))
        self._component_items: dict[int, ComponentItem] = {}

        # Wire drawing state
        self._wire_mode   = False
        self._wire_start_port: PortItem | None = None
        self._wire_preview: QGraphicsLineItem | None = None

        # Layer visibility
        self.layer_visible: dict[int, bool] = {l: True for l in LAYER_COLORS}

    # ── Grid ──────────────────────────────────────────────────────────────────

    def drawBackground(self, painter: QPainter, rect: QRectF):
        super().drawBackground(painter, rect)

        grid_px  = GRID_UM * PIXELS_PER_UM
        major_px = grid_px * 5

        left  = math.floor(rect.left()  / grid_px) * grid_px
        top   = math.floor(rect.top()   / grid_px) * grid_px
        right = rect.right()
        bot   = rect.bottom()

        painter.setPen(QPen(GRID_COLOR, 0.5))
        x = left
        while x <= right:
            painter.drawLine(QLineF(x, top, x, bot))
            x += grid_px

        y = top
        while y <= bot:
            painter.drawLine(QLineF(left, y, right, y))
            y += grid_px

        painter.setPen(QPen(GRID_MAJOR, 0.8))
        x = math.floor(rect.left() / major_px) * major_px
        while x <= right:
            painter.drawLine(QLineF(x, top, x, bot))
            x += major_px

        y = math.floor(rect.top() / major_px) * major_px
        while y <= bot:
            painter.drawLine(QLineF(left, y, right, y))
            y += major_px

    # ── Component management ──────────────────────────────────────────────────

    def add_component(self, inst: ComponentInstance) -> ComponentItem:
        item = ComponentItem(inst, self.cfg, self)
        self.addItem(item)
        self._component_items[inst.inst_id] = item
        self._apply_layer_visibility()
        return item

    def remove_component(self, inst_id: int):
        if inst_id in self._component_items:
            self.removeItem(self._component_items.pop(inst_id))

    def select_component(self, inst_id: int) -> None:
        """Programmatically select a component by inst_id, deselecting all others."""
        self.clearSelection()
        item = self._component_items.get(inst_id)
        if item:
            item.setSelected(True)

    def rebuild_component(self, inst_id: int):
        if inst_id in self._component_items:
            item = self._component_items[inst_id]
            item._rebuild()

    def all_instances(self) -> list[ComponentInstance]:
        return [item.inst for item in self._component_items.values()]

    def set_layer_visible(self, layer: int, visible: bool):
        self.layer_visible[layer] = visible
        self._apply_layer_visibility()

    def _apply_layer_visibility(self):
        # Rebuild items with layer filter applied through opacity
        for item in self._component_items.values():
            item.update()

    # ── Wire mode ─────────────────────────────────────────────────────────────

    def set_wire_mode(self, enabled: bool):
        self._wire_mode = enabled
        if not enabled:
            self._cancel_wire()

    def _cancel_wire(self):
        if self._wire_preview:
            self.removeItem(self._wire_preview)
            self._wire_preview = None
        self._wire_start_port = None

    def _nearest_port_in_scene(self, scene_pos: QPointF) -> PortItem | None:
        best_dist = PORT_SNAP_UM * PIXELS_PER_UM * 2
        best: PortItem | None = None
        for item in self._component_items.values():
            pi = item.nearest_port(scene_pos)
            if pi:
                wp = pi.world_pos()
                d  = math.hypot(scene_pos.x() - wp.x(), scene_pos.y() - wp.y())
                if d < best_dist:
                    best_dist = d
                    best = pi
        return best

    def mousePressEvent(self, event):
        if self._wire_mode and event.button() == Qt.MouseButton.LeftButton:
            pos  = event.scenePos()
            port = self._nearest_port_in_scene(pos)
            if port:
                if self._wire_start_port is None:
                    # Start a wire
                    self._wire_start_port = port
                    wp = port.world_pos()
                    self._wire_preview = QGraphicsLineItem(
                        QLineF(wp, wp)
                    )
                    self._wire_preview.setPen(
                        QPen(WIRE_PREVIEW_COLOR, 1.5, Qt.PenStyle.DashLine)
                    )
                    self.addItem(self._wire_preview)
                    self.status_message.emit("Wire started — click destination port")
                else:
                    # Finish wire
                    src = self._wire_start_port
                    dst = port
                    if src.owner.inst.inst_id != dst.owner.inst.inst_id:
                        wire = WireItem(src.world_pos(), dst.world_pos())
                        self.addItem(wire)
                        # Record logical connection
                        src.owner.inst.connections[src.port.name] = (
                            dst.owner.inst.inst_id, dst.port.name
                        )
                        dst.owner.inst.connections[dst.port.name] = (
                            src.owner.inst.inst_id, src.port.name
                        )
                        self.wire_connected.emit(
                            src.owner.inst.inst_id, src.port.name,
                            dst.owner.inst.inst_id, dst.port.name,
                        )
                    self._cancel_wire()
                    self.status_message.emit("Wire connected")
                return
            else:
                self._cancel_wire()
                self.status_message.emit("No port found — wire cancelled")
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._wire_mode and self._wire_preview and self._wire_start_port:
            wp  = self._wire_start_port.world_pos()
            cur = event.scenePos()
            self._wire_preview.setLine(QLineF(wp, cur))
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._cancel_wire()
            self.status_message.emit("Cancelled")
        elif event.key() == Qt.Key.Key_Delete:
            for item in self.selectedItems():
                if isinstance(item, ComponentItem):
                    self.remove_component(item.inst.inst_id)
        elif event.key() == Qt.Key.Key_R:
            # R → rotate 90° clockwise
            for item in self.selectedItems():
                if isinstance(item, ComponentItem):
                    item.inst.rotate_cw()
                    item._rebuild()
                    self.component_moved.emit(item.inst.inst_id)
                    self.selection_changed_signal.emit(item.inst.inst_id)
                    self.status_message.emit(f"Rotated CW → {item.inst.rotation}°")
        elif event.key() == Qt.Key.Key_E:
            # E → rotate 90° counter-clockwise
            for item in self.selectedItems():
                if isinstance(item, ComponentItem):
                    item.inst.rotate_ccw()
                    item._rebuild()
                    self.component_moved.emit(item.inst.inst_id)
                    self.selection_changed_signal.emit(item.inst.inst_id)
                    self.status_message.emit(f"Rotated CCW → {item.inst.rotation}°")
        super().keyPressEvent(event)


# ── View ──────────────────────────────────────────────────────────────────────

class GDSView(QGraphicsView):
    coord_changed = pyqtSignal(float, float)   # µm x, µm y

    def __init__(self, scene: GDSScene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSceneRect(-5000, -5000, 10000, 10000)
        self._pan_active  = False
        self._pan_origin  = None
        self._zoom        = 1.0
        self._pan_mode    = False

    def set_pan_mode(self, enabled: bool):
        self._pan_mode = enabled
        if enabled:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self._zoom  = max(0.05, min(self._zoom, 50.0))
        self.scale(factor, factor)

    def mouseMoveEvent(self, event):
        sp   = self.mapToScene(event.pos())
        um_x = px_to_um(sp.x())
        um_y = -px_to_um(sp.y())
        self.coord_changed.emit(um_x, um_y)
        super().mouseMoveEvent(event)

    def zoom_fit(self):
        items = self.scene().items()
        if not items:
            return
        rect = self.scene().itemsBoundingRect()
        self.fitInView(rect.adjusted(-20, -20, 20, 20),
                       Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_reset(self):
        self.resetTransform()
        self._zoom = 1.0