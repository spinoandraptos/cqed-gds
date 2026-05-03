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
    QPen, QBrush, QColor, QPainter, QPolygonF, QPainterPath,
)
from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsItem,
    QGraphicsRectItem, QGraphicsPolygonItem, QGraphicsPathItem,
    QGraphicsEllipseItem, QGraphicsLineItem,
)

from dataclasses import dataclass, field

from core.model import (
    DesignScene, GDSComponent, ComponentKind,
    Point, Port, PortSide, dbu_to_um, um_to_dbu,
)
from core.commands import CommandStack, AddComponent, MoveComponent, ConnectPorts, DisconnectPorts
from ui.theme import Colors


# ── Constants ─────────────────────────────────────────────────────────────────

GRID_MINOR_DBU  = um_to_dbu(1)
GRID_MAJOR_DBU  = um_to_dbu(10)
SCENE_EXTENT    = um_to_dbu(5_000)
DEFAULT_W_DBU   = um_to_dbu(10)
DEFAULT_H_DBU   = um_to_dbu(5)
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

_PORT_NORMAL  = "#38bdf8"   # teal — idle
_PORT_ACTIVE  = "#4ade80"   # green — snap candidate
_PORT_R       = um_to_dbu(0.8)   # dot radius in DBU

# Arrow tip offsets per side (in DBU, pointing outward)
_ARROW_DIR = {
    PortSide.NORTH: (0, -1),
    PortSide.SOUTH: (0,  1),
    PortSide.EAST:  (1,  0),
    PortSide.WEST:  (-1, 0),
}


class PortItem(QGraphicsItem):
    """
    Small directional dot drawn at a port's position.
    Parent is the ComponentItem so it moves for free.
    Highlights green when this port is the active snap target.
    """

    def __init__(self, port: Port, origin: Point, parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._port    = port
        self._active  = False
        self.setZValue(8)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        abs_pos = port.abs_pos(origin)
        self.setPos(abs_pos.x, abs_pos.y)

    @property
    def port(self) -> Port:
        return self._port

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.update()

    def boundingRect(self) -> QRectF:
        r = float(_PORT_R) 
        return QRectF(-r, -r, r * 2, r * 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        color = QColor(_PORT_ACTIVE if self._active else _PORT_NORMAL)
        r     = float(_PORT_R)

        # Filled circle
        painter.setPen(_cosmetic(color.name(), 0.8))
        fill = QColor(color); fill.setAlpha(200)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(0, 0), r, r)

        # Outward tick line
        dx, dy = _ARROW_DIR[self._port.side]
        tick   = r * 2.5
        painter.setPen(_cosmetic(color.name(), 1.0))
        painter.drawLine(QPointF(0, 0), QPointF(dx * tick, dy * tick))


# ── Connection Edge Indicator ─────────────────────────────────────────────────

_INDICATOR_SIZE = um_to_dbu(1.6)   # half-width of triangle base

_ARROW_DIR_INDICATOR = {
    PortSide.NORTH: (0, -1),
    PortSide.SOUTH: (0,  1),
    PortSide.EAST:  (1,  0),
    PortSide.WEST:  (-1, 0),
}


class EdgeIndicatorItem(QGraphicsItem):
    """
    Small filled triangle on an occupied edge of a ComponentItem.
    Base sits on the edge; apex points inward so it reads as 'docked here'.
    One instance per connected PortSide, parented to ComponentItem.
    """

    def __init__(self, side: PortSide, bbox_local: QRectF,
                 parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._side = side
        self.setZValue(9)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._poly = self._build_poly(side, bbox_local)

    @staticmethod
    def _build_poly(side: PortSide, r: QRectF) -> QPolygonF:
        s  = float(_INDICATOR_SIZE)
        cx = (r.left() + r.right())  / 2.0
        cy = (r.top()  + r.bottom()) / 2.0
        if side == PortSide.NORTH:
            return QPolygonF([QPointF(cx - s, r.top()),
                               QPointF(cx + s, r.top()),
                               QPointF(cx,     r.top() + s * 1.8)])
        elif side == PortSide.SOUTH:
            return QPolygonF([QPointF(cx - s, r.bottom()),
                               QPointF(cx + s, r.bottom()),
                               QPointF(cx,     r.bottom() - s * 1.8)])
        elif side == PortSide.WEST:
            return QPolygonF([QPointF(r.left(), cy - s),
                               QPointF(r.left(), cy + s),
                               QPointF(r.left() + s * 1.8, cy)])
        else:  # EAST
            return QPolygonF([QPointF(r.right(), cy - s),
                               QPointF(r.right(), cy + s),
                               QPointF(r.right() - s * 1.8, cy)])

    def boundingRect(self) -> QRectF:
        return self._poly.boundingRect()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setPen(_cosmetic(_CONN_BORDER, 0.6))
        fill = QColor(_CONN_FILL)
        fill.setAlpha(220)
        painter.setBrush(QBrush(fill))
        painter.drawPolygon(self._poly)


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
        self._snap_offset: Optional[Point]   = None   # set during drag when port snap active

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges,
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._delegate: QGraphicsItem = self._make_delegate()
        self._apply_style(selected=False, hovered=False)
        self._port_items: List[PortItem] = self._build_port_items()
        self._edge_indicators: List[EdgeIndicatorItem] = []

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
        for pi in self._port_items:
            pi.set_active(False)

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
            self._drag_start  = event.scenePos()
            self._orig_pos    = Point(self._comp.origin.x, self._comp.origin.y)
            self._snap_offset = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._drag_start is None:
            return
        delta = event.scenePos() - self._drag_start
        tentative = Point(
            self._orig_pos.x + int(round(delta.x())),
            self._orig_pos.y + int(round(delta.y())),
        )
        snap_result = self._scene_ref.find_port_snap(self._comp, tentative)
        self._scene_ref.clear_all_port_highlights()
        if snap_result:
            snap_origin, my_port_id, their_port_id, their_comp_id = snap_result
            self._snap_offset = snap_origin
            self.set_port_active(my_port_id, True)
            other_item = self._scene_ref.item_for(their_comp_id)
            if other_item:
                other_item.set_port_active(their_port_id, True)
        else:
            self._snap_offset = None

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None
                and self._orig_pos is not None):

            self._scene_ref.clear_all_port_highlights()

            if self._snap_offset is not None:
                final = self._snap_offset
            else:
                delta = event.scenePos() - self._drag_start
                raw = Point(
                    self._orig_pos.x + int(round(delta.x())),
                    self._orig_pos.y + int(round(delta.y())),
                )
                moved = abs(delta.x()) > 1.0 or abs(delta.y()) > 1.0
                final = self._scene_ref.snap(raw) if moved else self._orig_pos

            self.setPos(0, 0)

            actually_moved = final != self._orig_pos

            # ── Disconnect before any move ─────────────────────────────────────
            # Must happen regardless of whether we're re-snapping to a new port
            # or just dragging free — either way the old wiring is broken.
            if actually_moved:
                self._scene_ref.disconnect_component(self._comp.id)

            if actually_moved:
                cmd = MoveComponent(self._comp.id, self._orig_pos, final)
                self._scene_ref.cmd_stack.execute(cmd)

            if self._snap_offset is not None:
                self._scene_ref._try_connect_snapped(self._comp, final)
            elif final == self._orig_pos:
                self.sync_from_model()

            self._drag_start  = None
            self._orig_pos    = None
            self._snap_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._apply_style(bool(value), hovered=False)
            if bool(value):
                self._scene_ref.item_selected.emit(self._comp.id)
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

    def __init__(self, design: DesignScene, parent=None) -> None:
        super().__init__(parent)
        self._design = design
        self._items: Dict[str, ComponentItem] = {}
        self.cmd_stack = CommandStack(design)
        self.cmd_stack.connect_change(self._on_model_changed)

        ext = SCENE_EXTENT
        self.setSceneRect(-ext, -ext, ext * 2, ext * 2)
        self.setBackgroundBrush(QBrush(QColor(Colors.CANVAS_BG)))

        # Placement FSM — all state lives in one object; reset atomically.
        self._pl = PlacementState()
        self._pl.mode = PlacementMode.SELECT

        # Wire Qt's built-in selection signal so the properties panel clears
        # when the user clicks empty canvas (previously this was never connected).
        self.selectionChanged.connect(self._on_selection_changed)

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
    
    def disconnect_component(self, comp_id: str) -> None:
        """
        Sever every connection on comp_id via undo-aware commands.
        Called before a move commits so stale wiring is never left behind.
        """
        for conn in self._design.connections_for(comp_id):
            self.cmd_stack.execute(DisconnectPorts(conn))

    # ── Mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        snapped = self.snap_f(event.scenePos().x(), event.scenePos().y())

        if self._pl.mode == PlacementMode.PLACE_RECT:
            if event.button() == Qt.MouseButton.LeftButton:
                self._stamp_rect(snapped)
            return  # don't forward to items in placement mode

        if self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_vertex(snapped)
            elif event.button() == Qt.MouseButton.RightButton:
                self._commit_poly_or_path()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            snapped = self.snap_f(event.scenePos().x(), event.scenePos().y())
            self._add_vertex(snapped)
            self._commit_poly_or_path()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:
        raw     = event.scenePos()
        snapped = self.snap_f(raw.x(), raw.y())
        self.cursor_moved.emit(dbu_to_um(int(raw.x())), dbu_to_um(int(raw.y())))

        if self._pl.mode == PlacementMode.PLACE_RECT:
            self._update_ghost_rect(snapped)
        elif self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            self._update_ghost_edge(snapped)

        super().mouseMoveEvent(event)

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
        Emitted by Qt whenever the selection set changes.
        When nothing is selected we emit item_selected("") so the properties
        panel clears — previously it would stay populated with stale data.
        """
        sel = self.selectedItems()
        if not sel:
            self.item_selected.emit("")
        elif len(sel) == 1:
            item = sel[0]
            if hasattr(item, "component"):
                self.item_selected.emit(item.component.id)

    def _on_model_changed(self) -> None:
        model_ids = {c.id for c in self._design.components}
        scene_ids = set(self._items.keys())

        for comp in self._design.components:
            if comp.id not in self._items:
                item = ComponentItem(comp, self)
                self.addItem(item)
                self._items[comp.id] = item

        for dead_id in scene_ids - model_ids:
            self.removeItem(self._items.pop(dead_id))

        for comp in self._design.components:
            self._items[comp.id].sync_from_model()

        self.refresh_all_indicators()
        self.scene_changed.emit()
        self.update()