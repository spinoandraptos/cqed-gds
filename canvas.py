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
    UNDERCUT_RING_LAYER, UNDERCUT_RING_THICKNESS,
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
        self._z_order: int = 0   # logical stacking order; higher = in front

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
            s = self._scene.snap_um
            self.inst.x = snap(px_to_um(px), s)
            self.inst.y = snap(-px_to_um(py), s)
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
        # Signal that a drag may be starting so the app can snapshot undo state
        if event.button() == Qt.MouseButton.LeftButton:
            self._scene.component_drag_started.emit(self.inst.inst_id)

    def contextMenuEvent(self, event):
        """Right-click menu with z-order actions."""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e2530;
                color: #cdd6f4;
                border: 1px solid #3d4555;
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item { padding: 5px 20px 5px 12px; border-radius: 3px; }
            QMenu::item:selected { background-color: #313244; }
            QMenu::separator { background: #3d4555; height: 1px; margin: 3px 8px; }
        """)
        act_front  = menu.addAction("⬆  Bring to Front       ]")
        act_fwd    = menu.addAction("↑  Bring Forward        [")
        menu.addSeparator()
        act_back   = menu.addAction("↓  Send Backward        {")
        act_to_back = menu.addAction("⬇  Send to Back         }")

        chosen = menu.exec(event.screenPos())
        if chosen == act_front:
            self._scene.bring_to_front(self.inst.inst_id)
        elif chosen == act_fwd:
            self._scene.bring_forward(self.inst.inst_id)
        elif chosen == act_back:
            self._scene.send_backward(self.inst.inst_id)
        elif chosen == act_to_back:
            self._scene.send_to_back(self.inst.inst_id)
        event.accept()

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


# ── Undercut ring editor overlay ──────────────────────────────────────────────

# Colours for the interactive ring segments
_RING_ACTIVE  = QColor("#C060FF")   # segment present (purple)
_RING_DELETED = QColor("#444466")   # segment toggled off
_RING_HOVER   = QColor("#E090FF")   # mouse-over highlight

SIDE_NAMES = ("top", "bottom", "left", "right")


class UnderCutSegmentItem(QGraphicsItem):
    """
    One clickable side-rectangle of the undercut ring.
    Clicking toggles whether this segment is included.
    """

    def __init__(self, side: str, rect: QRectF, parent: "UnderCutEditItem"):
        super().__init__(parent)
        self.side    = side
        self._rect   = rect
        self._active = True
        self._hovered = False
        self.setAcceptHoverEvents(True)
        self.setZValue(20)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def toggle(self):
        self._active = not self._active
        self.update()

    @property
    def active(self) -> bool:
        return self._active

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-2, -2, 2, 2)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._active:
            base = _RING_HOVER if self._hovered else _RING_ACTIVE
        else:
            base = QColor("#666688") if self._hovered else _RING_DELETED

        fill = QColor(base)
        fill.setAlpha(160 if self._active else 60)
        painter.setBrush(QBrush(fill))
        pen_col = QColor(base)
        pen_col.setAlpha(230)
        painter.setPen(QPen(pen_col, 1.2))
        painter.drawRect(self._rect)

        # Label
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
        font = QFont("monospace", 7)
        painter.setFont(font)
        painter.drawText(self._rect, Qt.AlignmentFlag.AlignCenter, self.side)

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
            # Notify parent to update status
            self.parentItem()._on_segment_clicked()
        event.accept()


class UnderCutEditItem(QGraphicsItem):
    """
    Temporary overlay placed over a component while the user selects
    which ring segments to keep.  Contains four UnderCutSegmentItem
    children (top/bottom/left/right) and a Confirm button.

    When confirmed, emits a dict of side→bool via the callback passed in.
    """

    def __init__(
        self,
        bbox_px: QRectF,        # bounding box in scene pixels (outer edge of ring)
        on_confirm,             # callable(sides: dict[str,bool])
        on_cancel,              # callable()
        scene: "GDSScene",
    ):
        super().__init__()
        self._bbox   = bbox_px
        self._on_confirm = on_confirm
        self._on_cancel  = on_cancel
        self._scene  = scene

        t = UNDERCUT_RING_THICKNESS * PIXELS_PER_UM   # ring thickness in px

        # Outer rect (ring outer edge)
        ox0 = bbox_px.left()
        oy0 = bbox_px.top()
        ox1 = bbox_px.right()
        oy1 = bbox_px.bottom()

        # Four segment rects (same geometry as render_instance)
        # Note: canvas y is flipped — top in µm = smaller scene-y
        seg_rects = {
            "top":    QRectF(ox0 - t, oy0 - t, (ox1 - ox0) + 2 * t, t),
            "bottom": QRectF(ox0 - t, oy1,     (ox1 - ox0) + 2 * t, t),
            "left":   QRectF(ox0 - t, oy0,     t, oy1 - oy0),
            "right":  QRectF(ox1,     oy0,     t, oy1 - oy0),
        }

        self._segments: dict[str, UnderCutSegmentItem] = {}
        for side, rect in seg_rects.items():
            seg = UnderCutSegmentItem(side, rect, self)
            self._segments[side] = seg

        # Bounding rect covers everything including ring
        self._bounding = QRectF(
            ox0 - t - 4, oy0 - t - 4,
            (ox1 - ox0) + 2 * t + 8,
            (oy1 - oy0) + 2 * t + 8,
        )

        self.setZValue(15)
        self._build_buttons()

    def _build_buttons(self):
        """Add Confirm / Cancel text labels as child items."""
        cx = (self._bbox.left() + self._bbox.right()) / 2
        # Place below the ring
        t  = UNDERCUT_RING_THICKNESS * PIXELS_PER_UM
        by = self._bbox.bottom() + t + 8

        self._confirm_rect = QRectF(cx - 40, by, 78, 20)
        self._cancel_rect  = QRectF(cx - 40, by + 24, 78, 20)

        # Extend bounding to include buttons
        self._bounding = self._bounding.united(
            QRectF(cx - 42, by - 2, 82, 50)
        )

    def _on_segment_clicked(self):
        active = sum(1 for s in self._segments.values() if s.active)
        self._scene.status_message.emit(
            f"Undercut ring: {active}/4 sides active — click Confirm or Cancel"
        )
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounding

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Dim overlay inside the ring bbox
        dimmer = QColor(0, 0, 0, 60)
        painter.setBrush(QBrush(dimmer))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(self._bbox)

        # Dashed bbox outline
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.setPen(QPen(QColor("#C060FF"), 1.0, Qt.PenStyle.DashLine))
        painter.drawRect(self._bbox)

        # ── Confirm button ────────────────────────────────────────────────
        painter.setBrush(QBrush(QColor("#1a3a1a")))
        painter.setPen(QPen(QColor("#40bb40"), 1.2))
        painter.drawRoundedRect(self._confirm_rect, 4, 4)
        painter.setPen(QPen(QColor("#80ee80"), 1))
        painter.setFont(QFont("sans-serif", 8, QFont.Weight.Bold))
        painter.drawText(self._confirm_rect, Qt.AlignmentFlag.AlignCenter, "✓ Confirm")

        # ── Cancel button ─────────────────────────────────────────────────
        painter.setBrush(QBrush(QColor("#3a1a1a")))
        painter.setPen(QPen(QColor("#bb4040"), 1.2))
        painter.drawRoundedRect(self._cancel_rect, 4, 4)
        painter.setPen(QPen(QColor("#ee8080"), 1))
        painter.drawText(self._cancel_rect, Qt.AlignmentFlag.AlignCenter, "✕ Cancel")

    def mousePressEvent(self, event):
        pos = event.pos()
        if self._confirm_rect.contains(pos):
            sides = {side: seg.active for side, seg in self._segments.items()}
            self._on_confirm(sides)
            event.accept()
            return
        if self._cancel_rect.contains(pos):
            self._on_cancel()
            event.accept()
            return
        # Don't consume — let children handle segment clicks
        super().mousePressEvent(event)


ERASE_RECT_COLOR = QColor("#FF4444")


class EraseRectItem(QGraphicsRectItem):
    """
    Translucent red rectangle shown while the user drags an erase selection.
    Not interactive — purely visual feedback.
    """

    def __init__(self):
        super().__init__()
        pen = QPen(ERASE_RECT_COLOR, 1.5, Qt.PenStyle.DashLine)
        fill = QColor(ERASE_RECT_COLOR)
        fill.setAlpha(40)
        self.setPen(pen)
        self.setBrush(QBrush(fill))
        self.setZValue(30)
        self.hide()

    def set_rect(self, p1: QPointF, p2: QPointF):
        x0 = min(p1.x(), p2.x())
        y0 = min(p1.y(), p2.y())
        w  = abs(p2.x() - p1.x())
        h  = abs(p2.y() - p1.y())
        self.setRect(QRectF(x0, y0, w, h))
        self.show()


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
    component_moved          = pyqtSignal(int)   # inst_id
    component_drag_started   = pyqtSignal(int)   # inst_id — fired on press, before drag
    selection_changed_signal = pyqtSignal(int)
    wire_connected           = pyqtSignal(int, str, int, str)
    status_message           = pyqtSignal(str)
    merge_requested          = pyqtSignal(list)  # list[int] of selected inst_ids
    undercut_confirmed       = pyqtSignal(int, dict)  # source inst_id, sides dict
    erase_applied            = pyqtSignal(int)   # inst_id of ring that was modified

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setBackgroundBrush(QBrush(CANVAS_BG))
        self._component_items: dict[int, ComponentItem] = {}

        # Wire drawing state
        self._wire_mode   = False
        self._wire_start_port: PortItem | None = None
        self._wire_preview: QGraphicsLineItem | None = None

        # Snap grid (µm) — adjustable at runtime
        self.snap_um: float = SNAP_UM

        # Undercut ring edit state
        self._undercut_edit_item: UnderCutEditItem | None = None
        self._undercut_source_id: int | None = None

        # Erase mode state
        self._erase_mode = False

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
        self._restack()
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

    # ── Undercut ring editing ─────────────────────────────────────────────────

    def begin_undercut_edit(self, inst_id: int) -> bool:
        """
        Start the interactive undercut-ring editor for the given component.
        Returns False if the component isn't found or another edit is in progress.
        """
        if self._undercut_edit_item is not None:
            self.status_message.emit(
                "Finish or cancel the current undercut edit first"
            )
            return False

        item = self._component_items.get(inst_id)
        if item is None:
            return False

        # Compute scene-space bounding box of the component polygons
        # (item.boundingRect() is in item-local coords; map to scene)
        scene_rect = item.mapToScene(item.boundingRect()).boundingRect()

        self._undercut_source_id = inst_id

        edit = UnderCutEditItem(
            bbox_px=scene_rect,
            on_confirm=self._on_undercut_confirm,
            on_cancel=self._on_undercut_cancel,
            scene=self,
        )
        self.addItem(edit)
        self._undercut_edit_item = edit

        # Lock the source component so it can't be accidentally moved
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

        active_count = 4
        self.status_message.emit(
            f"Undercut ring: {active_count}/4 sides active — "
            "click segments to remove, then Confirm"
        )
        return True

    def _on_undercut_confirm(self, sides: dict):
        """Called by UnderCutEditItem when user clicks Confirm."""
        src_id = self._undercut_source_id
        self._finish_undercut_edit()
        if src_id is not None:
            self.undercut_confirmed.emit(src_id, sides)

    def _on_undercut_cancel(self):
        """Called by UnderCutEditItem when user clicks Cancel."""
        self._finish_undercut_edit()
        self.status_message.emit("Undercut ring cancelled")

    def _finish_undercut_edit(self):
        """Remove the overlay and restore movability of the source component."""
        if self._undercut_edit_item is not None:
            self.removeItem(self._undercut_edit_item)
            self._undercut_edit_item = None

        if self._undercut_source_id is not None:
            item = self._component_items.get(self._undercut_source_id)
            if item:
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
            self._undercut_source_id = None

    # ── Erase mode ────────────────────────────────────────────────────────────

    def set_erase_mode(self, enabled: bool):
        self._erase_mode = enabled

    def erase_undercut_in_rect(self, scene_rect: QRectF) -> int:
        """
        Subtract the given scene-space rectangle from every undercut_ring
        instance whose geometry overlaps it.

        Returns the number of rings that were modified.
        """
        from component_model import clip_undercut_ring_by_rect

        # Convert scene pixels → world µm (y-flipped)
        x0_um = px_to_um(scene_rect.left())
        x1_um = px_to_um(scene_rect.right())
        # Scene y is flipped: larger scene-y = smaller world-y
        y0_um = -px_to_um(scene_rect.bottom())
        y1_um = -px_to_um(scene_rect.top())

        # Ensure ordering
        if x0_um > x1_um:
            x0_um, x1_um = x1_um, x0_um
        if y0_um > y1_um:
            y0_um, y1_um = y1_um, y0_um

        rect_um = (x0_um, y0_um, x1_um, y1_um)

        count = 0
        for inst_id, item in list(self._component_items.items()):
            if item.inst.type_id != "undercut_ring":
                continue
            changed = clip_undercut_ring_by_rect(item.inst, rect_um)
            if changed:
                item._rebuild()
                self.erase_applied.emit(inst_id)
                count += 1

        if count:
            self.status_message.emit(
                f"Erased undercut geometry from {count} ring(s)"
            )
        else:
            self.status_message.emit("Erase rect did not overlap any undercut ring")

        return count

    # ── Z-order (send to back / bring to front) ───────────────────────────────

    def _restack(self):
        """Apply logical _z_order values as Qt ZValues for all component items."""
        items = list(self._component_items.values())
        # Sort by _z_order so we can assign dense ZValues 1, 2, 3 …
        items.sort(key=lambda i: i._z_order)
        for rank, item in enumerate(items):
            item._z_order = rank          # normalise to 0-based dense ints
            item.setZValue(1 + rank)      # ZValue 1+ keeps components above wires (0.5)

    def _selected_component_items(self) -> list:
        return [
            item for item in self.selectedItems()
            if isinstance(item, ComponentItem)
        ]

    def bring_to_front(self, inst_id: int | None = None):
        """Move component(s) to the very top of the stack."""
        targets = (
            [self._component_items[inst_id]]
            if inst_id is not None and inst_id in self._component_items
            else self._selected_component_items()
        )
        if not targets:
            return
        max_z = max(i._z_order for i in self._component_items.values())
        for item in targets:
            item._z_order = max_z + 1
        self._restack()
        self.status_message.emit("Brought to front")

    def bring_forward(self, inst_id: int | None = None):
        """Move component(s) one step toward the front."""
        targets = (
            [self._component_items[inst_id]]
            if inst_id is not None and inst_id in self._component_items
            else self._selected_component_items()
        )
        if not targets:
            return
        all_items = sorted(self._component_items.values(), key=lambda i: i._z_order)
        for target in targets:
            idx = all_items.index(target)
            if idx < len(all_items) - 1:
                # Swap with the item directly above
                neighbor = all_items[idx + 1]
                target._z_order, neighbor._z_order = neighbor._z_order, target._z_order
        self._restack()
        self.status_message.emit("Brought forward")

    def send_to_back(self, inst_id: int | None = None):
        """Move component(s) to the very bottom of the stack."""
        targets = (
            [self._component_items[inst_id]]
            if inst_id is not None and inst_id in self._component_items
            else self._selected_component_items()
        )
        if not targets:
            return
        min_z = min(i._z_order for i in self._component_items.values())
        for item in targets:
            item._z_order = min_z - 1
        self._restack()
        self.status_message.emit("Sent to back")

    def send_backward(self, inst_id: int | None = None):
        """Move component(s) one step toward the back."""
        targets = (
            [self._component_items[inst_id]]
            if inst_id is not None and inst_id in self._component_items
            else self._selected_component_items()
        )
        if not targets:
            return
        all_items = sorted(self._component_items.values(), key=lambda i: i._z_order)
        for target in targets:
            idx = all_items.index(target)
            if idx > 0:
                neighbor = all_items[idx - 1]
                target._z_order, neighbor._z_order = neighbor._z_order, target._z_order
        self._restack()
        self.status_message.emit("Sent backward")

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
            if self._undercut_edit_item is not None:
                self._on_undercut_cancel()
            else:
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
        elif event.key() == Qt.Key.Key_M:
            # M → merge selected components
            selected_ids = [
                item.inst.inst_id
                for item in self.selectedItems()
                if isinstance(item, ComponentItem)
            ]
            if len(selected_ids) >= 2:
                self.merge_requested.emit(selected_ids)
            else:
                self.status_message.emit(
                    "Select 2 or more components to merge  [M]"
                )
        elif event.key() == Qt.Key.Key_BracketRight:
            # ] → Bring to Front
            self.bring_to_front()
        elif event.key() == Qt.Key.Key_BracketLeft:
            # [ → Bring Forward
            self.bring_forward()
        elif event.key() == Qt.Key.Key_BraceRight:
            # } → Send to Back
            self.send_to_back()
        elif event.key() == Qt.Key.Key_BraceLeft:
            # { → Send Backward
            self.send_backward()
        super().keyPressEvent(event)


# ── View ──────────────────────────────────────────────────────────────────────

class GDSView(QGraphicsView):
    coord_changed = pyqtSignal(float, float)   # µm x, µm y

    def __init__(self, scene: GDSScene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
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

        # Erase drag state
        self._erase_origin: QPointF | None = None
        self._erase_rect_item = EraseRectItem()
        scene.addItem(self._erase_rect_item)

    def set_pan_mode(self, enabled: bool):
        self._pan_mode = enabled
        if enabled:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def set_erase_mode(self, enabled: bool):
        gds_scene = self.scene()
        if hasattr(gds_scene, "set_erase_mode"):
            gds_scene.set_erase_mode(enabled)
        if enabled:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self._erase_rect_item.hide()
            self._erase_origin = None
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def mousePressEvent(self, event):
        gds_scene = self.scene()
        if (getattr(gds_scene, "_erase_mode", False)
                and event.button() == Qt.MouseButton.LeftButton):
            self._erase_origin = self.mapToScene(event.pos())
            self._erase_rect_item.set_rect(self._erase_origin, self._erase_origin)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        sp   = self.mapToScene(event.pos())
        um_x = px_to_um(sp.x())
        um_y = -px_to_um(sp.y())
        self.coord_changed.emit(um_x, um_y)

        gds_scene = self.scene()
        if (getattr(gds_scene, "_erase_mode", False)
                and self._erase_origin is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._erase_rect_item.set_rect(self._erase_origin, sp)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        gds_scene = self.scene()
        if (getattr(gds_scene, "_erase_mode", False)
                and event.button() == Qt.MouseButton.LeftButton
                and self._erase_origin is not None):
            end = self.mapToScene(event.pos())
            rect = self._erase_rect_item.rect()
            self._erase_rect_item.hide()
            self._erase_origin = None
            if rect.width() > 1 and rect.height() > 1:
                gds_scene.erase_undercut_in_rect(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self._zoom  = max(0.05, min(self._zoom, 50.0))
        self.scale(factor, factor)

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