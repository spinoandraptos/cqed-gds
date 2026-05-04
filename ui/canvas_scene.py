"""
ui/canvas_scene.py — QGraphicsScene subclass.

Phase 2 additions over Phase 1:
  - PlacementMode enum: SELECT | PLACE_RECT | PLACE_POLYGON | PLACE_PATH
  - ComponentItem factory: dispatches rect/polygon/path Qt item per kind
  - Ghost preview items during placement (rubber-band rect, live polygon outline)
  - Click-to-place for rect (single click stamps it, stay in mode for rapid place)
  - Click-per-vertex for polygon (double-click or Enter closes, ESC cancels)
  - Click-per-vertex for path (Enter commits open polyline, ESC cancels)
  - mode_changed signal drives status bar text and cursor in the view

Coordinate convention (unchanged from Phase 1):
  1 scene unit = 1 DBU (nm). Y-axis NOT flipped — GDS flip at export only.
"""

from __future__ import annotations

import math
from enum import Enum, auto
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPen, QBrush, QColor, QPainter, QPolygonF, QPainterPath, QTransform
)
from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsItem,
    QGraphicsRectItem, QGraphicsPolygonItem, QGraphicsPathItem,
    QGraphicsEllipseItem, QGraphicsLineItem,
)

from dataclasses import dataclass, field

from core.model import (
    DesignScene, GDSComponent, ComponentKind,
    Point, Port, PortSide, dbu_to_um, um_to_dbu, ComponentGroup
)
from core.commands import CommandStack, AddComponent, MoveComponent, ConnectPorts, DisconnectPorts, MoveGroup, BatchCommand
from ui.theme import Colors
from core.cell_library import place_cell
from core.commands import PlaceCellCommand


# ── Constants ─────────────────────────────────────────────────────────────────

GRID_MINOR_DBU  = um_to_dbu(0.1)
GRID_MAJOR_DBU  = um_to_dbu(10)
SCENE_EXTENT    = um_to_dbu(5_000)
DEFAULT_W_DBU   = um_to_dbu(2)    # was um_to_dbu(10)
DEFAULT_H_DBU   = um_to_dbu(0.2)  # was um_to_dbu(5)
DEFAULT_PW_DBU  = um_to_dbu(1)
MIN_POLY_PTS    = 3
VERTEX_DOT_R    = um_to_dbu(0.4)
PORT_SNAP_RADIUS = um_to_dbu(8)   # snap kicks in within 8 µm

# Connection edge indicator colours
_CONN_FILL   = "#4ade80"   # green fill
_CONN_BORDER = "#166534"   # dark green border


# ── Placement FSM state ───────────────────────────────────────────────────────

@dataclass
class PlacementState:
    """
    All mutable state belonging to one placement gesture.
    Replacing with a fresh instance atomically resets everything —
    no risk of a stray ghost or dangling vertex list after cancel.
    """
    mode:        "PlacementMode" = None          # filled in after enum defined
    layer:       int             = 0
    pts:         list            = field(default_factory=list)   # List[QPointF]
    ghost_rect:  object          = None          # Optional[QGraphicsRectItem]
    ghost_poly:  object          = None          # Optional[QGraphicsPolygonItem]
    ghost_edge:  object          = None          # Optional[QGraphicsLineItem]
    vertex_dots: list            = field(default_factory=list)   # List[QGraphicsEllipseItem]


# ── Placement mode ────────────────────────────────────────────────────────────

class PlacementMode(Enum):
    SELECT        = auto()
    PLACE_RECT    = auto()
    PLACE_POLYGON = auto()
    PLACE_PATH    = auto()

    @property
    def status_label(self) -> str:
        return {
            PlacementMode.SELECT:        "SELECT",
            PlacementMode.PLACE_RECT:    "PLACE RECT  —  click to stamp  |  ESC cancel",
            PlacementMode.PLACE_POLYGON: "PLACE POLYGON  —  click vertices  |  dbl-click or Enter to close  |  ESC cancel",
            PlacementMode.PLACE_PATH:    "PLACE PATH  —  click vertices  |  Enter to commit  |  ESC cancel",
        }[self]


# ── Pen / brush helpers ───────────────────────────────────────────────────────

def _cosmetic(color: str, width: float = 1.0,
              style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(QColor(color), width, style)
    pen.setCosmetic(True)
    return pen


# ── Port Graphics Item ────────────────────────────────────────────────────────

_PORT_NORMAL  = "#38bdf8"   # teal — idle / visible
_PORT_ACTIVE  = "#4ade80"   # green — snap candidate

# Screen-space sizes (pixels).  Ports use ItemIgnoresTransformations so these
# are always exactly this many pixels on screen regardless of zoom level.
_PORT_R_PX    = 4.0    # dot radius in screen pixels
_PORT_TICK_PX = 8.0    # outward tick length in screen pixels

# Arrow tip offsets per side (unit vectors, pointing outward)
_ARROW_DIR = {
    PortSide.NORTH: (0, -1),
    PortSide.SOUTH: (0,  1),
    PortSide.EAST:  (1,  0),
    PortSide.WEST:  (-1, 0),
}


class PortItem(QGraphicsItem):
    """
    Fixed screen-size port dot — always 4 px radius, never blocked by zoom.

    Key design decisions
    --------------------
    • ItemIgnoresTransformations: the item is positioned in scene space (so it
      follows the component when it moves) but painted in screen space, so it
      never grows when the user zooms in.  No more port blobs eating the cell.

    • Visibility gated on parent state: hidden by default; shown only while the
      parent ComponentItem is hovered or selected, or while the port is the
      active snap target.  This keeps the canvas clean at a glance.

    • Zero mouse interaction: NoButton + no hover so it never steals clicks from
      the component beneath it.
    """

    def __init__(self, port: Port, origin: Point, parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._port   = port
        self._active = False

        self.setZValue(8)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        # Render at a fixed screen size — immune to zoom transforms.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        abs_pos = port.abs_pos(origin)
        self.setPos(abs_pos.x, abs_pos.y)

        # Hidden at rest; shown by ComponentItem on hover/select or snap-active.
        self.setVisible(False)

    @property
    def port(self) -> Port:
        return self._port

    def set_active(self, active: bool) -> None:
        """Highlight this port as the current snap target (shows it regardless of parent state)."""
        if active != self._active:
            self._active = active
            if active:
                self.setVisible(True)
            # Visibility when deactivating is restored by set_visible_for_state()
            self.update()

    def set_visible_for_state(self, hovered_or_selected: bool) -> None:
        """Show/hide based on parent component's hover/selection state."""
        # Always keep visible if actively snapping
        self.setVisible(hovered_or_selected or self._active)

    def boundingRect(self) -> QRectF:
        # In screen-space (ItemIgnoresTransformations), units ARE pixels.
        r    = _PORT_R_PX
        tick = _PORT_TICK_PX
        dx, dy = _ARROW_DIR[self._port.side]
        # Bounding rect must cover both the circle and the tick line.
        min_x = min(-r, dx * tick - 1)
        min_y = min(-r, dy * tick - 1)
        max_x = max( r, dx * tick + 1)
        max_y = max( r, dy * tick + 1)
        return QRectF(min_x, min_y, max_x - min_x, max_y - min_y)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        color = QColor(_PORT_ACTIVE if self._active else _PORT_NORMAL)
        r     = _PORT_R_PX

        # Filled circle with a crisp 1 px border
        pen = QPen(color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(210)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(0.0, 0.0), r, r)

        # Short outward tick — shows directionality without eating real estate
        dx, dy = _ARROW_DIR[self._port.side]
        tick_pen = QPen(color, 1.5)
        tick_pen.setCosmetic(True)
        painter.setPen(tick_pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(
            QPointF(dx * r, dy * r),                          # circle edge
            QPointF(dx * _PORT_TICK_PX, dy * _PORT_TICK_PX), # tick tip
        )


# ── Connection Edge Indicator ─────────────────────────────────────────────────

# Screen-space pip size — fixed pixels, zoom-immune, unobtrusive.
_INDICATOR_R_PX   = 3.0   # dot radius in screen pixels
_INDICATOR_GAP_PX = 6.0   # outward offset from edge so it sits just outside

_ARROW_DIR_INDICATOR = {
    PortSide.NORTH: (0, -1),
    PortSide.SOUTH: (0,  1),
    PortSide.EAST:  (1,  0),
    PortSide.WEST:  (-1, 0),
}


class EdgeIndicatorItem(QGraphicsItem):
    """
    Tiny fixed-pixel dot sitting just outside the component edge to signal
    a live connection on that side.

    Uses ItemIgnoresTransformations so it's always _INDICATOR_R_PX regardless
    of zoom — informational at a glance, never obstructing the view.
    The item is positioned at the scene-space edge midpoint so it moves with
    the parent component automatically.
    """

    def __init__(self, side: PortSide, bbox_local: QRectF,
                 parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._side = side
        self.setZValue(9)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Anchor at the scene-space midpoint of the relevant edge.
        # Paint is then done in screen-space (pixels) around (0, 0).
        cx = (bbox_local.left() + bbox_local.right())  / 2.0
        cy = (bbox_local.top()  + bbox_local.bottom()) / 2.0
        dx, dy = _ARROW_DIR_INDICATOR[side]
        if side == PortSide.NORTH:
            self.setPos(cx, bbox_local.top())
        elif side == PortSide.SOUTH:
            self.setPos(cx, bbox_local.bottom())
        elif side == PortSide.WEST:
            self.setPos(bbox_local.left(), cy)
        else:  # EAST
            self.setPos(bbox_local.right(), cy)

    def boundingRect(self) -> QRectF:
        r = _INDICATOR_R_PX + _INDICATOR_GAP_PX + 2.0
        return QRectF(-r, -r, r * 2, r * 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        dx, dy = _ARROW_DIR_INDICATOR[self._side]
        # Centre the pip just outside the edge
        cx = dx * _INDICATOR_GAP_PX
        cy = dy * _INDICATOR_GAP_PX
        r  = _INDICATOR_R_PX

        fill = QColor(_CONN_FILL)
        fill.setAlpha(180)
        border = QColor(_CONN_BORDER)

        pen = QPen(border, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(cx, cy), r, r)

# ── Group Graphics Item ───────────────────────────────────────────────────────

_GROUP_BORDER_IDLE     = "#475569"
_GROUP_BORDER_HOVER    = "#38bdf8"
_GROUP_BORDER_EDITING  = "#f59e0b"   # amber — editing mode
_GROUP_BG_ALPHA        = 18          # very faint fill so members show through


class GroupItem(QGraphicsItem):
    """
    Visual container for a ComponentGroup.

    Drag is handled MANUALLY — ItemIsMovable is never set.
    During drag we call move_by() on each member directly so they
    follow in real-time. On release we undo those live moves and
    re-apply via MoveGroup so the command stack gets one clean entry.
    """

    def __init__(self, group: "ComponentGroup", scene_ref: "CanvasScene") -> None:
        super().__init__()
        self._group      = group
        self._scene_ref  = scene_ref
        self._editing    = False

        # Drag state
        self._drag_start:    Optional[QPointF] = None
        self._last_drag_pos: Optional[QPointF] = None   # previous frame position
        self._total_dx = 0   # accumulated DBU delta this drag gesture
        self._total_dy = 0
        self._snap_adjust: Optional[tuple] = None   # (extra_dx, extra_dy) snap nudge

        self.setZValue(1)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            # NO ItemIsMovable — we drive movement ourselves
        )
        self.setAcceptHoverEvents(True)

    @property
    def group(self) -> "ComponentGroup":
        return self._group

    @property
    def is_editing(self) -> bool:
        return self._editing

    def enter_edit_mode(self) -> None:
        self._editing = True
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self._scene_ref._set_group_members_movable(self._group.id, True)
        self.update()

    def exit_edit_mode(self) -> None:
        self._editing = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self._scene_ref._set_group_members_movable(self._group.id, False)
        self.update()

    def _current_bbox(self) -> QRectF:
        bb = self._group.bbox_from(self._scene_ref._design.components)
        # No scene-space padding — the box hugs the physical cell geometry.
        # A tiny cosmetic pixel offset is added in boundingRect() for Qt's
        # dirty-region tracking only; it never inflates the visual rect.
        return QRectF(
            bb.x_min, bb.y_min,
            bb.x_max - bb.x_min,
            bb.y_max - bb.y_min,
        )

    def boundingRect(self) -> QRectF:
        # 4 extra scene-units (sub-pixel at any sensible zoom) for cosmetic pen
        return self._current_bbox().adjusted(-4, -4, 4, 4)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self._current_bbox()
        if self._editing:
            border = _GROUP_BORDER_EDITING
        elif self.isSelected():
            border = _GROUP_BORDER_HOVER
        else:
            border = _GROUP_BORDER_IDLE

        # Cosmetic pen — 1 px on screen regardless of zoom
        pen = QPen(QColor(border), 1.0)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 3])
        painter.setPen(pen)
        fill = QColor(border); fill.setAlpha(_GROUP_BG_ALPHA)
        painter.setBrush(QBrush(fill))
        # drawRect instead of drawRoundedRect — corner radius was scene-space
        # (um_to_dbu(1) = 1000 nm) which ballooned the visual box at any zoom.
        painter.drawRect(rect)

        # Label: cosmetic pixel-size font positioned just above the top edge
        name_pen = QPen(QColor(border)); name_pen.setCosmetic(True)
        painter.setPen(name_pen)
        font = painter.font()
        font.setPixelSize(10)   # fixed 10 px — readable at any zoom
        painter.setFont(font)
        # Offset in scene units must be tiny; use 1 DBU (1 nm) so the label
        # sits right on the border line rather than floating 1 µm above it.
        painter.drawText(
            QPointF(rect.left() + 4, rect.top() - 2),
            self._group.name,
        )

    def hoverEnterEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.update(); super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.unsetCursor()
        self.update(); super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._editing:
            self._scene_ref.group_selected.emit(self._group.id)
            # Select the group if not already selected
            if not self.isSelected():
                self.setSelected(True)
            # Always arm drag on the first press — don't require a second click
            self._drag_start    = event.scenePos()
            self._last_drag_pos = event.scenePos()
            self._total_dx      = 0
            self._total_dy      = 0
            self._snap_adjust   = None
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is None or self._editing:
            super().mouseMoveEvent(event)
            return

        # Incremental delta since last frame — move members live
        cur  = event.scenePos()
        dx   = int(round(cur.x() - self._last_drag_pos.x()))
        dy   = int(round(cur.y() - self._last_drag_pos.y()))

        if dx != 0 or dy != 0:
            for cid in self._group.member_ids:
                comp = self._scene_ref._design.get(cid)
                if comp:
                    comp.move_by(dx, dy)
            # Sync Qt items immediately — no full _on_model_changed needed
            for cid in self._group.member_ids:
                item = self._scene_ref._items.get(cid)
                if item:
                    item.sync_from_model()
                    item.refresh_connection_state(self._scene_ref._design)

            self._total_dx      += dx
            self._total_dy      += dy
            self._last_drag_pos  = cur

            # Invalidate group border so it redraws at new position
            self.prepareGeometryChange()

        # ── Port snap probe (runs every move, zero-cost when no near port) ────
        self._scene_ref.clear_all_port_highlights()
        snap = self._scene_ref.find_group_port_snap(self._group)
        if snap:
            self._snap_adjust = (snap[0], snap[1])   # extra_dx, extra_dy
            my_comp_id, my_port_id, their_comp_id, their_port_id = snap[2:]
            my_item    = self._scene_ref._items.get(my_comp_id)
            their_item = self._scene_ref._items.get(their_comp_id)
            if my_item:
                my_item.set_port_active(my_port_id, True)
            if their_item:
                their_item.set_port_active(their_port_id, True)
        else:
            self._snap_adjust = None

        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None
                and not self._editing):

            self._scene_ref.clear_all_port_highlights()

            total_dx = self._total_dx
            total_dy = self._total_dy

            # Incorporate any snap nudge into the total displacement
            snap_adjust = self._snap_adjust
            if snap_adjust:
                total_dx += snap_adjust[0]
                total_dy += snap_adjust[1]

            if total_dx != 0 or total_dy != 0:
                # Sever any existing connections on group members before moving.
                # This clears stale indicators whether or not a new snap is found.
                for cid in self._group.member_ids:
                    self._scene_ref.disconnect_component(cid)

                # Reverse only the live-dragged portion — snap nudge was never
                # applied to the model, so only undo _total_dx / _total_dy
                for cid in self._group.member_ids:
                    comp = self._scene_ref._design.get(cid)
                    if comp:
                        comp.move_by(-self._total_dx, -self._total_dy)

                self._scene_ref.cmd_stack.execute(
                    MoveGroup(self._group.id, total_dx, total_dy)
                )

            # Wire snapped ports via undo-aware commands
            if snap_adjust:
                self._scene_ref._try_connect_group_snap(self._group)

            self._drag_start    = None
            self._last_drag_pos = None
            self._total_dx      = 0
            self._total_dy      = 0
            self._snap_adjust   = None
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.enter_edit_mode()
        self._scene_ref.group_edit_entered.emit(self._group.id)
        event.accept()


# ── Component Graphics Item ───────────────────────────────────────────────────

class ComponentItem(QGraphicsItem):
    """
    Visual proxy for one GDSComponent.

    A thin shell that owns a child delegate item (Rect/Polygon/Path) and
    centralises all event handling here. The delegate only provides shape +
    paint. This keeps interaction logic in one place regardless of geometry type.
    """

    def __init__(self, component: GDSComponent, scene_ref: "CanvasScene") -> None:
        super().__init__()
        self._comp      = component
        self._scene_ref = scene_ref
        self._drag_start:  Optional[QPointF] = None
        self._orig_pos:    Optional[Point]   = None
        self._snap_offset: Optional[Point]   = None

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges,
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._delegate: QGraphicsItem = self._make_delegate()
        self._port_items: List[PortItem] = self._build_port_items()
        self._edge_indicators: List[EdgeIndicatorItem] = []
        self._apply_style(selected=False, hovered=False)

    # ── Delegate factory ──────────────────────────────────────────────────────

    def _make_delegate(self) -> QGraphicsItem:
        c = self._comp
        if c.kind == ComponentKind.RECTANGLE:
            return QGraphicsRectItem(c.origin.x, c.origin.y, c.width, c.height, self)
        elif c.kind == ComponentKind.POLYGON:
            return QGraphicsPolygonItem(self._build_polygon(), self)
        else:   # PATH
            return QGraphicsPathItem(self._build_path(), self)

    def _build_polygon(self) -> QPolygonF:
        pts = self._comp.points or []
        poly = QPolygonF([QPointF(p.x, p.y) for p in pts])
        if pts and pts[0] != pts[-1]:
            poly.append(QPointF(pts[0].x, pts[0].y))
        return poly

    def _build_path(self) -> QPainterPath:
        pts = self._comp.points or []
        if not pts:
            return QPainterPath()
        p = QPainterPath(QPointF(pts[0].x, pts[0].y))
        for pt in pts[1:]:
            p.lineTo(pt.x, pt.y)
        return p

    # ── Required overrides ────────────────────────────────────────────────────

    def boundingRect(self) -> QRectF:
        return self._delegate.boundingRect() if self._delegate else QRectF()

    def paint(self, painter, option, widget=None) -> None:
        pass   # delegate paints itself

    def shape(self):
        return self._delegate.shape() if self._delegate else super().shape()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self, selected: bool, hovered: bool) -> None:
        layer_color = Colors.LAYER_COLORS[self._comp.layer % len(Colors.LAYER_COLORS)]
        base = QColor(layer_color)

        if selected:
            fill_a, pen_color, pen_w = 90, Colors.ACCENT, 1.5
        elif hovered:
            fill_a, pen_color, pen_w = 65, base.lighter(150).name(), 1.0
        else:
            fill_a, pen_color, pen_w = 45, layer_color, 0.8

        fill = QColor(base); fill.setAlpha(fill_a)
        pen  = _cosmetic(pen_color, pen_w)

        if isinstance(self._delegate, QGraphicsPathItem):
            # Paths render as a thick stroked polyline with no fill
            pw     = self._comp.path_width or DEFAULT_PW_DBU
            stroke = QPen(fill, pw)
            stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self._delegate.setPen(stroke)
            self._delegate.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        else:
            self._delegate.setPen(pen)
            self._delegate.setBrush(QBrush(fill))

        # Ports only visible while hovered or selected — clean canvas at rest.
        show_ports = selected or hovered
        for pi in self._port_items:
            pi.set_visible_for_state(show_ports)

    # ── Sync ──────────────────────────────────────────────────────────────────

    def sync_from_model(self) -> None:
        c = self._comp
        if isinstance(self._delegate, QGraphicsRectItem):
            self._delegate.setRect(c.origin.x, c.origin.y, c.width, c.height)
        elif isinstance(self._delegate, QGraphicsPolygonItem):
            self._delegate.setPolygon(self._build_polygon())
        elif isinstance(self._delegate, QGraphicsPathItem):
            self._delegate.setPath(self._build_path())
        self.prepareGeometryChange()
        self.rebuild_ports()

    @property
    def component(self) -> GDSComponent:
        return self._comp

    def _build_port_items(self) -> List[PortItem]:
        return [PortItem(p, self._comp.origin, self) for p in self._comp.ports]

    # In ComponentItem — replace rebuild_ports() entirely:

    def rebuild_ports(self) -> None:
        """
        Update port positions from the current bbox WITHOUT replacing Port objects.
        Preserving port IDs is critical — Connection records store IDs, so
        regenerating them silently breaks all existing wiring.
        """
        if not self._comp.ports:
            # Sub-components of a parametric cell have _no_auto_ports=True —
            # they carry no connection points intentionally.  Only generate
            # default ports for standalone shapes that were placed directly.
            if getattr(self._comp, "_no_auto_ports", False):
                self._port_items = []
                return
            # First time only: generate ports and build items
            self._comp.build_default_ports()
            self._port_items = self._build_port_items()
            return

        # Ports already exist — recompute offsets in-place, keep IDs
        bb = self._comp.bbox
        cx = (bb.x_min + bb.x_max) // 2
        cy = (bb.y_min + bb.y_max) // 2
        ox, oy = self._comp.origin.x, self._comp.origin.y

        new_offsets = {
            "N": Point(cx - ox, bb.y_min - oy),
            "S": Point(cx - ox, bb.y_max - oy),
            "W": Point(bb.x_min - ox, cy - oy),
            "E": Point(bb.x_max - ox, cy - oy),
        }

        for port in self._comp.ports:
            if port.name in new_offsets:
                port.offset = new_offsets[port.name]

        # Reposition existing PortItems to match — no new objects, no new IDs
        for pi in self._port_items:
            abs_pos = pi.port.abs_pos(self._comp.origin)
            pi.setPos(abs_pos.x, abs_pos.y)

    def set_port_active(self, port_id: str, active: bool) -> None:
        for pi in self._port_items:
            if pi.port.id == port_id:
                pi.set_active(active)
                return

    def clear_port_highlights(self) -> None:
        is_visible = self.isSelected()
        for pi in self._port_items:
            pi.set_active(False)
            pi.set_visible_for_state(is_visible)

    @property
    def port_items(self) -> "List[PortItem]":
        return self._port_items

    def refresh_connection_state(self, design: DesignScene) -> None:
        """Rebuild edge indicator children to match current wiring."""
        for ind in self._edge_indicators:
            ind.setParentItem(None)
            if self.scene():
                self.scene().removeItem(ind)
        self._edge_indicators = []

        occupied_sides = design.connected_sides(self._comp.id)
        if not occupied_sides:
            return

        bbox = self._delegate.boundingRect()
        seen: set = set()
        for side in occupied_sides:
            if side in seen:
                continue
            seen.add(side)
            ind = EdgeIndicatorItem(side, bbox, self)
            self._edge_indicators.append(ind)

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
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # Drag is handled at the scene level so all selected items move together.
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        # Release is handled at the scene level.
        event.accept()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._apply_style(bool(value), hovered=False)
        return super().itemChange(change, value)


# ── Canvas Scene ──────────────────────────────────────────────────────────────

class CanvasScene(QGraphicsScene):
    """Master scene. Owns all ComponentItems, the CommandStack, and placement FSM."""

    item_selected      = pyqtSignal(str)
    item_hovered       = pyqtSignal(str)
    cursor_moved       = pyqtSignal(float, float)
    scene_changed      = pyqtSignal()
    mode_changed       = pyqtSignal(str)
    connections_changed = pyqtSignal()   # fired after any wiring change
    multi_selection_changed = pyqtSignal(list)  # comp_ids when >1 selected
    group_edit_entered = pyqtSignal(str)   # group_id
    group_edit_exited  = pyqtSignal()
    group_selected = pyqtSignal(str)

    def __init__(self, design: DesignScene, parent=None) -> None:
        super().__init__(parent)
        self._design = design
        self._items: Dict[str, ComponentItem] = {}
        self._group_items: Dict[str, GroupItem] = {}
        self._editing_group_id: Optional[str]  = None
        self.cmd_stack = CommandStack(design)
        self.cmd_stack.connect_change(self._on_model_changed)

        ext = SCENE_EXTENT
        self.setSceneRect(-ext, -ext, ext * 2, ext * 2)
        self.setBackgroundBrush(QBrush(QColor(Colors.CANVAS_BG)))

        # Placement FSM — all state lives in one object; reset atomically.
        self._pl = PlacementState()
        self._pl.mode = PlacementMode.SELECT

        # ── Drag state (managed at scene level) ───────────────────────────────
        self._drag_start:     Optional[QPointF] = None
        self._orig_positions: dict              = {}   # ComponentItem → Point
        self._drag_committed: bool              = False
        self._snap_offset:    Optional[Point]   = None

        # Wire Qt's built-in selection signal so the properties panel clears
        # when the user clicks empty canvas (previously this was never connected).
        self.selectionChanged.connect(self._on_selection_changed)
        self.group_edit_entered.connect(self._on_group_edit_entered)


    # ── Public placement API ──────────────────────────────────────────────────

    @property
    def mode(self) -> PlacementMode:
        return self._pl.mode

    def set_mode(self, mode: PlacementMode, layer: int = 0) -> None:
        self._clear_ghosts()
        self._pl.mode  = mode
        self._pl.layer = layer
        self.mode_changed.emit(mode.status_label)

    def cancel_placement(self) -> None:
        self._clear_ghosts()
        self._pl.mode = PlacementMode.SELECT
        self.mode_changed.emit(PlacementMode.SELECT.status_label)

    # ── Public scene API (replaces direct private-member access from outside) ──

    def refresh_item_style(self, comp_id: str) -> None:
        """Re-apply layer colour after an undo-able layer edit."""
        item = self._items.get(comp_id)
        if item:
            item._apply_style(item.isSelected(), hovered=False)

    def reset(self) -> None:
        """Clear all items and ghosts; called by MainWindow._new_design()."""
        self._clear_ghosts()
        self._on_model_changed()

    def place_rectangle(self, origin_x_um: float, origin_y_um: float,
                        width_um: float, height_um: float, layer: int = 0) -> GDSComponent:
        """Programmatic rect placement (toolbar quick-place, tests)."""
        comp = GDSComponent(
            kind   = ComponentKind.RECTANGLE, layer = layer,
            origin = Point.from_um(origin_x_um, origin_y_um),
            width  = um_to_dbu(width_um), height = um_to_dbu(height_um),
        )
        self.cmd_stack.execute(AddComponent(comp))
        return comp

    def drop_shape(self, kind_val: int, layer: int, scene_pos: QPointF) -> None:
        """
        Called by CanvasView.dropEvent after a drag from the shape palette.

        kind_val  — the raw ComponentKind enum value (int) carried in the MIME data
        layer     — the target layer number
        scene_pos — drop position in scene (DBU) coordinates, snapped to grid below
        """
        try:
            kind = ComponentKind(kind_val)
        except ValueError:
            return  # unknown kind; ignore silently rather than crash

        snapped = self.snap_f(scene_pos.x(), scene_pos.y())
        cx, cy  = int(snapped.x()), int(snapped.y())

        if kind == ComponentKind.RECTANGLE:
            hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
            comp = GDSComponent(
                kind   = ComponentKind.RECTANGLE,
                layer  = layer,
                origin = Point(cx - hw, cy - hh),
                width  = DEFAULT_W_DBU,
                height = DEFAULT_H_DBU,
            )

        elif kind == ComponentKind.POLYGON:
            # Default: equilateral-ish triangle centred on the drop point
            hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
            pts = [
                Point(cx,      cy - hh),   # top
                Point(cx + hw, cy + hh),   # bottom-right
                Point(cx - hw, cy + hh),   # bottom-left
            ]
            comp = GDSComponent(
                kind   = ComponentKind.POLYGON,
                layer  = layer,
                origin = pts[0],
                points = pts,
            )

        elif kind == ComponentKind.PATH:
            # Default: short horizontal segment
            hw = DEFAULT_W_DBU // 2
            pts = [Point(cx - hw, cy), Point(cx + hw, cy)]
            comp = GDSComponent(
                kind       = ComponentKind.PATH,
                layer      = layer,
                origin     = pts[0],
                points     = pts,
                path_width = DEFAULT_PW_DBU,
            )

        else:
            return  # future kinds; ignore

        self.cmd_stack.execute(AddComponent(comp))

    def snap(self, pt: Point) -> Point:
        g = GRID_MINOR_DBU
        return Point(round(pt.x / g) * g, round(pt.y / g) * g)

    def snap_f(self, x: float, y: float) -> QPointF:
        g = float(GRID_MINOR_DBU)
        return QPointF(round(x / g) * g, round(y / g) * g)

    def item_for(self, comp_id: str) -> Optional["ComponentItem"]:
        return self._items.get(comp_id)

    def clear_all_port_highlights(self) -> None:
        for item in self._items.values():
            item.clear_port_highlights()

    def _try_connect_snapped(self, moving_comp: GDSComponent,
                              final_origin: Point) -> None:
        """Called after snap-move committed. Find the flush port pair and wire it."""
        result = self.find_port_snap(moving_comp, final_origin)
        if result is None:
            return
        _, my_port_id, their_port_id, their_comp_id = result

        if self._design.are_connected(
            moving_comp.id, my_port_id, their_comp_id, their_port_id
        ):
            return  # already wired — idempotent

        self.cmd_stack.execute(
            ConnectPorts(moving_comp.id, my_port_id, their_comp_id, their_port_id)
        )
        self._refresh_indicators(moving_comp.id)
        self._refresh_indicators(their_comp_id)
        self.connections_changed.emit()

    def _refresh_indicators(self, comp_id: str) -> None:
        item = self._items.get(comp_id)
        if item:
            item.refresh_connection_state(self._design)

    def refresh_all_indicators(self) -> None:
        """Rebuild all edge indicators — call after undo/redo."""
        for comp_id, item in self._items.items():
            item.refresh_connection_state(self._design)

    def find_port_snap(
        self,
        moving_comp: GDSComponent,
        tentative_origin: Point,
    ) -> Optional[tuple]:
        """
        Check whether any port on *moving_comp* (placed at *tentative_origin*)
        is within PORT_SNAP_RADIUS of a compatible port on another component.

        Compatibility rule: ports must face each other
        (moving.side == stationary.side.opposite).

        Returns (snapped_origin, my_port_id, their_port_id, their_comp_id)
        or None if no snap candidate found.

        The returned snapped_origin is the exact origin that places my_port
        flush against their_port — guaranteeing perfect edge alignment.
        """
        best_dist  = PORT_SNAP_RADIUS
        best       = None

        for port in moving_comp.ports:
            # Absolute position of this port at the tentative location
            my_abs = Point(
                tentative_origin.x + port.offset.x,
                tentative_origin.y + port.offset.y,
            )

            for other_comp in self._design.components:
                if other_comp.id == moving_comp.id:
                    continue
                for other_port in other_comp.ports:
                    # Compatibility: must face each other
                    if other_port.side != port.side.opposite:
                        continue

                    their_abs = other_port.abs_pos(other_comp.origin)
                    dx = my_abs.x - their_abs.x
                    dy = my_abs.y - their_abs.y
                    dist = math.sqrt(dx * dx + dy * dy)

                    if dist < best_dist:
                        best_dist = dist
                        # Exact origin that places my port ON their port
                        snapped_origin = Point(
                            tentative_origin.x - dx,
                            tentative_origin.y - dy,
                        )
                        best = (snapped_origin, port.id,
                                other_port.id, other_comp.id)

        return best
    
    def find_group_port_snap(
        self,
        moving_group: "ComponentGroup",
    ) -> Optional[tuple]:
        """
        Check whether any port on any member of *moving_group* is within
        PORT_SNAP_RADIUS of a compatible port on a component outside the group.

        'Outside the group' means: not in moving_group.member_ids.  This
        includes both standalone components and members of other groups —
        cross-group snapping is fully supported.

        Compatibility rule: ports must face each other
        (moving.side == stationary.side.opposite).

        Returns:
            (extra_dx, extra_dy, my_comp_id, my_port_id,
             their_comp_id, their_port_id)

            extra_dx / extra_dy  — the additional DBU translation needed to
                                   place the snapping port exactly flush.
            my_comp_id           — the member of moving_group whose port snapped.

        Returns None if no snap candidate found.
        """
        member_id_set = set(moving_group.member_ids)
        best_dist = PORT_SNAP_RADIUS
        best      = None

        for my_comp in self._design.components:
            if my_comp.id not in member_id_set:
                continue
            for my_port in my_comp.ports:
                my_abs = my_port.abs_pos(my_comp.origin)

                for other_comp in self._design.components:
                    if other_comp.id in member_id_set:
                        continue   # skip own group members
                    for other_port in other_comp.ports:
                        if other_port.side != my_port.side.opposite:
                            continue

                        their_abs = other_port.abs_pos(other_comp.origin)
                        dx = my_abs.x - their_abs.x
                        dy = my_abs.y - their_abs.y
                        dist = math.sqrt(dx * dx + dy * dy)

                        if dist < best_dist:
                            best_dist = dist
                            # Nudge that would place my_port exactly on their_port
                            best = (
                                -dx, -dy,
                                my_comp.id, my_port.id,
                                other_comp.id, other_port.id,
                            )

        return best

    def _try_connect_group_snap(self, group: "ComponentGroup") -> None:
        """
        After a snapped group drop: find the flush port pair and wire it.
        Disconnects any stale wiring on the snapping member first.
        """
        snap = self.find_group_port_snap(group)
        if snap is None:
            return
        _, _, my_comp_id, my_port_id, their_comp_id, their_port_id = snap

        # Sever existing connections on the snapping member before rewiring
        self.disconnect_component(my_comp_id)

        if self._design.are_connected(
            my_comp_id, my_port_id, their_comp_id, their_port_id
        ):
            return  # already wired — idempotent

        self.cmd_stack.execute(
            ConnectPorts(my_comp_id, my_port_id, their_comp_id, their_port_id)
        )
        self._refresh_indicators(my_comp_id)
        self._refresh_indicators(their_comp_id)
        self.connections_changed.emit()

    def disconnect_component(self, comp_id: str) -> None:
        """
        Sever every connection on comp_id via undo-aware commands.
        Called before a move commits so stale wiring is never left behind.
        """
        for conn in self._design.connections_for(comp_id):
            self.cmd_stack.execute(DisconnectPorts(conn))

    # ── Mouse events ──────────────────────────────────────────────────────────

    def _hit_component_item(self, scene_pos) -> Optional["ComponentItem"]:
        """Walk the item at scene_pos up to the nearest ComponentItem, or None."""
        transform = self.views()[0].transform() if self.views() else QTransform()
        hit = self.itemAt(scene_pos, transform)
        candidate = hit
        while candidate is not None:
            if isinstance(candidate, ComponentItem):
                return candidate
            candidate = candidate.parentItem()
        return None

    def mousePressEvent(self, event) -> None:
        # ── Exit group edit on outside click ─────────────────────────────────
        if self._editing_group_id:
            group = self._design.get_group(self._editing_group_id)
            if group:
                hit_comp = getattr(self._hit_component_item(event.scenePos()), "component", None)
                is_member = hit_comp is not None and hit_comp.id in group.member_ids
                if not is_member:
                    self.exit_group_edit()
                    self.clearSelection()

        snapped = self.snap_f(event.scenePos().x(), event.scenePos().y())

        # ── Placement modes ───────────────────────────────────────────────────
        if self._pl.mode == PlacementMode.PLACE_RECT:
            if event.button() == Qt.MouseButton.LeftButton:
                self._stamp_rect(snapped)
            return

        if self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_vertex(snapped)
            elif event.button() == Qt.MouseButton.RightButton:
                self._commit_poly_or_path()
            return

        # ── Select mode ───────────────────────────────────────────────────────
        if event.button() == Qt.MouseButton.LeftButton:
            comp_item = self._hit_component_item(event.scenePos())
            if comp_item is not None:
                self._on_item_press(comp_item, event)
                # Call super() so Qt delivers the press to the item and
                # establishes a mouse grabber. Without a grabber,
                # QGraphicsView will not forward mouseMoveEvents to the scene,
                # so _on_item_move never fires. ComponentItem.mousePressEvent
                # accepts without calling its own super(), so Qt's built-in
                # selection logic does not run and our selection is preserved.
                super().mousePressEvent(event)
                return
            else:
                # Empty canvas — clear selection unless modifier held,
                # then let super() start a rubber-band drag.
                modifiers = event.modifiers()
                multi = bool(modifiers & (Qt.KeyboardModifier.ControlModifier |
                                          Qt.KeyboardModifier.ShiftModifier))
                if not multi:
                    self.clearSelection()
                super().mousePressEvent(event)
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        raw     = event.scenePos()
        snapped = self.snap_f(raw.x(), raw.y())
        self.cursor_moved.emit(dbu_to_um(int(raw.x())), dbu_to_um(int(raw.y())))

        if self._pl.mode == PlacementMode.PLACE_RECT:
            self._update_ghost_rect(snapped)
        elif self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            self._update_ghost_edge(snapped)

        # Drive multi-select drag from the scene so ALL selected items move,
        # regardless of which single item Qt delivered the press to.
        if self._drag_start is not None and self._orig_positions:
            self._on_item_move(event)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None and self._orig_positions):
            self._on_item_release(event)
            return
        super().mouseReleaseEvent(event)

    def _on_item_press(self, item: "ComponentItem", event) -> None:
        """Called by ComponentItem.mousePressEvent — handles select + drag arm."""
        modifiers = event.modifiers()
        multi = bool(modifiers & (Qt.KeyboardModifier.ControlModifier |
                                  Qt.KeyboardModifier.ShiftModifier))

        # Block Qt's selectionChanged signal while we manipulate selection
        # so _on_selection_changed doesn't fire mid-operation.
        self.blockSignals(True)
        try:
            if multi:
                item.setSelected(not item.isSelected())
            else:
                if not item.isSelected():
                    self.clearSelection()
                    item.setSelected(True)
                # already selected: keep existing multi-selection for drag
        finally:
            self.blockSignals(False)

        # Snapshot origins of everything currently selected
        self._drag_start     = event.scenePos()
        self._drag_committed = False
        self._snap_offset    = None
        self._orig_positions = {
            i: Point(i._comp.origin.x, i._comp.origin.y)
            for i in self.selectedItems()
            if isinstance(i, ComponentItem)
        }

        # Fire selection signal once, cleanly, after we're done
        comp_items = [i for i in self.selectedItems() if isinstance(i, ComponentItem)]
        if not comp_items:
            self.item_selected.emit("")
        elif len(comp_items) == 1:
            self.item_selected.emit(comp_items[0].component.id)
        else:
            self.multi_selection_changed.emit([i.component.id for i in comp_items])

    def _on_item_move(self, event) -> None:
        """Called by ComponentItem.mouseMoveEvent during drag."""
        if self._drag_start is None or not self._orig_positions:
            return
        delta = event.scenePos() - self._drag_start
        if not self._drag_committed:
            if abs(delta.x()) < 3.0 and abs(delta.y()) < 3.0:
                return
            self._drag_committed = True

        for item, orig in self._orig_positions.items():
            new_x = orig.x + int(round(delta.x()))
            new_y = orig.y + int(round(delta.y()))
            dx = new_x - item._comp.origin.x
            dy = new_y - item._comp.origin.y
            if dx != 0 or dy != 0:
                item._comp.move_by(dx, dy)
                item.sync_from_model()

        self.clear_all_port_highlights()
        self._snap_offset = None
        if len(self._orig_positions) == 1:
            item = next(iter(self._orig_positions))
            orig = self._orig_positions[item]
            tentative = Point(
                orig.x + int(round(delta.x())),
                orig.y + int(round(delta.y())),
            )
            snap_result = self.find_port_snap(item._comp, tentative)
            if snap_result:
                snap_origin, my_port_id, their_port_id, their_comp_id = snap_result
                self._snap_offset = snap_origin
                item.set_port_active(my_port_id, True)
                other = self.item_for(their_comp_id)
                if other:
                    other.set_port_active(their_port_id, True)

    def _on_item_release(self, event) -> None:
        self.clear_all_port_highlights()
        if not self._drag_committed or not self._orig_positions:
            self._drag_start     = None
            self._orig_positions = {}
            self._drag_committed = False
            self._snap_offset    = None
            return

        delta = event.scenePos() - self._drag_start

        # ── Build move commands ───────────────────────────────────────────────
        move_cmds = []
        for item, orig in self._orig_positions.items():
            if self._snap_offset is not None and len(self._orig_positions) == 1:
                final = self._snap_offset
            else:
                raw   = Point(orig.x + int(round(delta.x())),
                            orig.y + int(round(delta.y())))
                final = self.snap(raw)

            # Revert live move so MoveComponent records correct before/after
            item._comp.move_by(orig.x - item._comp.origin.x,
                            orig.y - item._comp.origin.y)

            if final != orig:
                self.disconnect_component(item._comp.id)
                move_cmds.append(MoveComponent(item._comp.id, orig, final))

            item.sync_from_model()

        # ── Push as one undo unit ─────────────────────────────────────────────
        if move_cmds:
            if len(move_cmds) == 1:
                self.cmd_stack.execute(move_cmds[0])
            else:
                # Wrap in BatchCommand so one Undo reverses all items together
                from core.commands import BatchCommand
                self.cmd_stack.execute(
                    BatchCommand(move_cmds, f"Move {len(move_cmds)} components")
                )

        if self._snap_offset is not None and len(self._orig_positions) == 1:
            item = next(iter(self._orig_positions))
            self._try_connect_snapped(item._comp, self._snap_offset)

        self._drag_start     = None
        self._orig_positions = {}
        self._drag_committed = False
        self._snap_offset    = None

        if self._snap_offset is not None and len(self._orig_positions) == 1:
            item = next(iter(self._orig_positions))
            self._try_connect_snapped(item._comp, self._snap_offset)

        self._drag_start     = None
        self._orig_positions = {}
        self._drag_committed = False
        self._snap_offset    = None

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_placement()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
                self._commit_poly_or_path()
        else:
            super().keyPressEvent(event)

    # ── Placement helpers ─────────────────────────────────────────────────────

    def _stamp_rect(self, center: QPointF) -> None:
        hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
        comp = GDSComponent(
            kind   = ComponentKind.RECTANGLE, layer = self._pl.layer,
            origin = Point(int(center.x()) - hw, int(center.y()) - hh),
            width  = DEFAULT_W_DBU, height = DEFAULT_H_DBU,
        )
        self.cmd_stack.execute(AddComponent(comp))
        # ghost stays for the next placement (mode persists)

    def _add_vertex(self, pos: QPointF) -> None:
        self._pl.pts.append(pos)
        r = float(VERTEX_DOT_R)
        dot = QGraphicsEllipseItem(pos.x() - r, pos.y() - r, r * 2, r * 2)
        dot.setPen(_cosmetic(Colors.ACCENT, 0.5))
        dot.setBrush(QBrush(QColor(Colors.ACCENT)))
        dot.setZValue(10)
        self.addItem(dot)
        self._pl.vertex_dots.append(dot)
        self._refresh_ghost_poly()

    def _update_ghost_rect(self, cursor: QPointF) -> None:
        hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
        rect   = QRectF(cursor.x() - hw, cursor.y() - hh, DEFAULT_W_DBU, DEFAULT_H_DBU)
        if self._pl.ghost_rect is None:
            self._pl.ghost_rect = QGraphicsRectItem(rect)
            c = QColor(Colors.ACCENT); c.setAlpha(25)
            self._pl.ghost_rect.setPen(_cosmetic(Colors.ACCENT, 1.0, Qt.PenStyle.DashLine))
            self._pl.ghost_rect.setBrush(QBrush(c))
            self._pl.ghost_rect.setZValue(5)
            self.addItem(self._pl.ghost_rect)
        else:
            self._pl.ghost_rect.setRect(rect)

    def _refresh_ghost_poly(self) -> None:
        pts = self._pl.pts
        if len(pts) < 2:
            return
        poly = QPolygonF(pts)
        if self._pl.ghost_poly is None:
            self._pl.ghost_poly = QGraphicsPolygonItem(poly)
            self._pl.ghost_poly.setPen(_cosmetic(Colors.ACCENT, 1.0, Qt.PenStyle.DashLine))
            self._pl.ghost_poly.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self._pl.ghost_poly.setZValue(4)
            self.addItem(self._pl.ghost_poly)
        else:
            self._pl.ghost_poly.setPolygon(poly)

    def _update_ghost_edge(self, cursor: QPointF) -> None:
        if not self._pl.pts:
            return
        last = self._pl.pts[-1]
        if self._pl.ghost_edge is None:
            self._pl.ghost_edge = QGraphicsLineItem(last.x(), last.y(), cursor.x(), cursor.y())
            self._pl.ghost_edge.setPen(_cosmetic(Colors.ACCENT_BRIGHT, 1.0, Qt.PenStyle.DotLine))
            self._pl.ghost_edge.setZValue(6)
            self.addItem(self._pl.ghost_edge)
        else:
            self._pl.ghost_edge.setLine(last.x(), last.y(), cursor.x(), cursor.y())

    def _commit_poly_or_path(self) -> None:
        pts = self._pl.pts
        min_pts = MIN_POLY_PTS if self._pl.mode == PlacementMode.PLACE_POLYGON else 2
        if len(pts) < min_pts:
            return

        model_pts = [Point(int(p.x()), int(p.y())) for p in pts]
        if self._pl.mode == PlacementMode.PLACE_POLYGON:
            comp = GDSComponent(
                kind=ComponentKind.POLYGON, layer=self._pl.layer,
                origin=model_pts[0], points=model_pts,
            )
        else:
            comp = GDSComponent(
                kind=ComponentKind.PATH, layer=self._pl.layer,
                origin=model_pts[0], points=model_pts,
                path_width=DEFAULT_PW_DBU,
            )
        self.cmd_stack.execute(AddComponent(comp))
        self._clear_ghosts()   # reset vertices; stay in same mode

    def _clear_ghosts(self) -> None:
        for item in (self._pl.ghost_rect, self._pl.ghost_poly, self._pl.ghost_edge):
            if item is not None:
                self.removeItem(item)
        for dot in self._pl.vertex_dots:
            self.removeItem(dot)
        # Atomically reset all placement state, preserving mode and layer
        mode  = self._pl.mode  if self._pl.mode  is not None else PlacementMode.SELECT
        layer = self._pl.layer
        self._pl = PlacementState()
        self._pl.mode  = mode
        self._pl.layer = layer

    def drop_cell(self, cell_id: str, scene_pos: QPointF | None = None,
                  params: dict | None = None) -> None:
        if scene_pos is None:
            # Default: centre of the current viewport
            view = self.views()[0] if self.views() else None
            if view is not None:
                vr = view.viewport().rect()
                scene_pos = view.mapToScene(vr.center())
            else:
                scene_pos = QPointF(0, 0)

        origin = Point(int(scene_pos.x()), int(scene_pos.y()))
        result = place_cell(cell_id, origin, params=params)
        self.cmd_stack.execute(PlaceCellCommand(result, cell_id=cell_id, cell_params=params or {}))


    # ── Background ────────────────────────────────────────────────────────────

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        self._draw_grid(painter, rect)
        self._draw_origin(painter)

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        left   = int(math.floor(rect.left()  / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        top    = int(math.floor(rect.top()   / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        right  = int(math.ceil(rect.right()  / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        bottom = int(math.ceil(rect.bottom() / GRID_MINOR_DBU)) * GRID_MINOR_DBU

        minor = QPen(QColor(Colors.GRID_MINOR)); minor.setCosmetic(True); minor.setWidthF(0.5)
        major = QPen(QColor(Colors.GRID_MAJOR)); major.setCosmetic(True); major.setWidthF(0.8)

        painter.save()
        x = left
        while x <= right:
            painter.setPen(major if x % GRID_MAJOR_DBU == 0 else minor)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            x += GRID_MINOR_DBU
        y = top
        while y <= bottom:
            painter.setPen(major if y % GRID_MAJOR_DBU == 0 else minor)
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            y += GRID_MINOR_DBU
        painter.restore()

    def _draw_origin(self, painter: QPainter) -> None:
        size = GRID_MAJOR_DBU * 3
        pen  = QPen(QColor(Colors.GRID_ORIGIN)); pen.setCosmetic(True); pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(-size, 0), QPointF(size, 0))
        painter.drawLine(QPointF(0, -size), QPointF(0, size))

    # ── Model sync ────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        """
        Fired by Qt on every selection change (rubber-band, programmatic).
        - GroupItem selected → do nothing; group_selected signal from GroupItem handles it.
        - 0 ComponentItems  → clear panel via item_selected("")
        - 1 ComponentItem   → show single component via item_selected(id)
        - 2+ ComponentItems → show multi-select panel via multi_selection_changed
        """
        sel = self.selectedItems()
        comp_items  = [i for i in sel if isinstance(i, ComponentItem)]
        group_items = [i for i in sel if isinstance(i, GroupItem)]

        if group_items:
            return  # GroupItem.mousePressEvent already emitted group_selected

        if not comp_items:
            self.item_selected.emit("")
        elif len(comp_items) == 1:
            self.item_selected.emit(comp_items[0].component.id)
        else:
            self.multi_selection_changed.emit([i.component.id for i in comp_items])

    def _on_group_edit_entered(self, group_id: str) -> None:
        self._editing_group_id = group_id

    def _on_model_changed(self) -> None:
        model_ids = {c.id for c in self._design.components}
        scene_ids = set(self._items.keys())

        for comp in self._design.components:
            if comp.id not in self._items:
                item = ComponentItem(comp, self)
                self.addItem(item)
                self._items[comp.id] = item

        for dead_id in scene_ids - model_ids:
            dead_item = self._items.pop(dead_id)
            dead_item.setSelected(False)
            self.removeItem(dead_item)

        for comp in self._design.components:
            self._items[comp.id].sync_from_model()

        # ── Group sync ────────────────────────────────────────────────────────
        model_gids  = {g.id for g in self._design.groups}
        scene_gids  = set(self._group_items.keys())

        for group in self._design.groups:
            if group.id not in self._group_items:
                gi = GroupItem(group, self)
                self.addItem(gi)
                self._group_items[group.id] = gi
                # Lock members immediately — they must not be individually
                # selectable/draggable until the group enters edit mode.
                # Without this, sweep-generated groups leave members selectable,
                # causing Qt to hold simultaneous selected-item references to both
                # the GroupItem and its ComponentItems during a merge, which
                # produces a segfault when the GroupItem is removed mid-selection.
                self._set_group_members_movable(group.id, False)

        for dead_id in scene_gids - model_gids:
            dead_item = self._group_items.pop(dead_id)
            dead_item.setSelected(False)
            self.removeItem(dead_item)
            # Re-enable former members so they're individually selectable
            # again now that the group is dissolved (ungroup / merge).
            for cid in dead_item.group.member_ids:
                item = self._items.get(cid)
                if item:
                    item.setSelected(False)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

        # Invalidate group geometry after any model change
        for gi in self._group_items.values():
            gi.prepareGeometryChange()

        self.refresh_all_indicators()
        self.scene_changed.emit()
        self.update()

    def _set_group_members_movable(self, group_id: str, movable: bool) -> None:
        group = self._design.get_group(group_id)
        if not group:
            return
        for cid in group.member_ids:
            item = self._items.get(cid)
            if item:
                item.setFlag(
                    QGraphicsItem.GraphicsItemFlag.ItemIsMovable, movable
                )
                item.setFlag(
                    QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, movable
                )

    def exit_group_edit(self) -> None:
        if self._editing_group_id:
            gi = self._group_items.get(self._editing_group_id)
            if gi:
                gi.exit_edit_mode()
            self._editing_group_id = None
            self.group_edit_exited.emit()