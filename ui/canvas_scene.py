"""
ui/canvas_scene.py — QGraphicsScene subclass.

Coordinate convention: 1 scene unit = 1 DBU (nm). Y-axis is NOT flipped here;
the GDS flip happens at export only.

Placement modes
---------------
SELECT        — default; clicks select / drag items.
PLACE_RECT    — single click stamps a rectangle; mode persists for rapid placement.
PLACE_POLYGON — click per vertex; double-click or Enter closes; ESC cancels.
PLACE_PATH    — click per vertex; Enter commits open polyline; ESC cancels.

Drag system
-----------
A single unified drag handler (_arm_unified_drag / _on_unified_move /
_on_unified_release) covers standalone components, multi-component, single
groups, multi-group, and mixed component+group selections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF, QTransform,
)
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
    QGraphicsPathItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene,
)

from core.cell_library import place_cell
from core.clipboard import Clipboard
from core.commands import (
    AddComponent, BatchCommand, CommandStack, ConnectPorts, DisconnectPorts,
    MoveComponent, MoveGroup, PasteComponents, PlaceCellCommand,
    RotateComponent, RotateGroup,
)
from core.model import (
    ComponentGroup, ComponentKind, DesignScene, GDSComponent,
    Point, Port, PortSide, dbu_to_um, um_to_dbu,
)
from ui.theme import Colors
from ui.undercut_overlay import UndercutOverlay
from ui.ruler_overlay import RulerItem


# ── Scene constants ────────────────────────────────────────────────────────────

GRID_MINOR_DBU   = um_to_dbu(0.1)
GRID_MAJOR_DBU   = um_to_dbu(10)
SCENE_EXTENT     = um_to_dbu(5_000)
DEFAULT_W_DBU    = um_to_dbu(2)
DEFAULT_H_DBU    = um_to_dbu(0.2)
DEFAULT_PW_DBU   = um_to_dbu(1)
MIN_POLY_PTS     = 3
VERTEX_DOT_R     = um_to_dbu(0.4)
PORT_SNAP_RADIUS = um_to_dbu(0.5)
PASTE_OFFSET_DBU = um_to_dbu(10)

# Connection indicator colours
_CONN_FILL   = "#4ade80"
_CONN_BORDER = "#166534"


# ── Placement FSM ─────────────────────────────────────────────────────────────

@dataclass
class PlacementState:
    """
    All mutable state for one placement gesture.
    Replacing with a fresh instance atomically resets everything —
    no risk of a stray ghost or dangling vertex list after cancel.
    """
    mode:        "PlacementMode" = None
    layer:       int             = 0
    pts:         list            = field(default_factory=list)
    ghost_rect:  object          = None
    ghost_poly:  object          = None
    ghost_edge:  object          = None
    vertex_dots: list            = field(default_factory=list)


class PlacementMode(Enum):
    SELECT        = auto()
    PLACE_RECT    = auto()
    PLACE_POLYGON = auto()
    PLACE_PATH    = auto()
    RULER         = auto()
    MASK_UNDERCUT = auto()

    @property
    def status_label(self) -> str:
        return {
            PlacementMode.SELECT:        "SELECT",
            PlacementMode.PLACE_RECT:    "PLACE RECT  —  click to stamp  |  ESC cancel",
            PlacementMode.PLACE_POLYGON: "PLACE POLYGON  —  click vertices  |  dbl-click or Enter to close  |  ESC cancel",
            PlacementMode.PLACE_PATH:    "PLACE PATH  —  click vertices  |  Enter to commit  |  ESC cancel",
            PlacementMode.RULER:         "RULER  —  click-drag to measure  |  ESC to clear",
            PlacementMode.MASK_UNDERCUT: "ERASE UNDERCUT  —  click-drag rectangle to erase  |  ESC cancel",
        }[self]


# ── Pen helper ────────────────────────────────────────────────────────────────

def _cosmetic(color: str, width: float = 1.0,
              style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(QColor(color), width, style)
    pen.setCosmetic(True)
    return pen


# ── Port graphics item ────────────────────────────────────────────────────────

_PORT_NORMAL  = "#38bdf8"
_PORT_ACTIVE  = "#4ade80"
_PORT_R_PX    = 4.0    # dot radius in screen pixels
_PORT_TICK_PX = 8.0    # outward tick length in screen pixels

_ARROW_DIR: dict[PortSide, tuple[int, int]] = {
    PortSide.NORTH: (0, -1),
    PortSide.SOUTH: (0,  1),
    PortSide.EAST:  (1,  0),
    PortSide.WEST:  (-1, 0),
}


class PortItem(QGraphicsItem):
    """
    Fixed screen-size port dot — always _PORT_R_PX radius, zoom-immune.

    Key design decisions
    --------------------
    • ItemIgnoresTransformations: positioned in scene space (moves with
      the component) but painted in screen space (never grows on zoom-in).
    • Hidden by default; shown only while the parent ComponentItem is hovered
      or selected, or while the port is the active snap target.
    • Zero mouse interaction: NoButton so it never steals clicks from the
      component beneath it.
    """

    def __init__(self, port: Port, origin: Point, parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._port   = port
        self._active = False

        self.setZValue(8)
        self.setAcceptHoverEvents(False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)

        abs_pos = port.abs_pos(origin)
        self.setPos(abs_pos.x, abs_pos.y)
        self.setVisible(False)

    @property
    def port(self) -> Port:
        return self._port

    def set_active(self, active: bool) -> None:
        """Highlight as the current snap target; forces visibility while active."""
        if active != self._active:
            self._active = active
            if active:
                self.setVisible(True)
            self.update()

    def set_visible_for_state(self, hovered_or_selected: bool) -> None:
        """Show/hide based on parent component hover/selection state."""
        self.setVisible(hovered_or_selected or self._active)

    def boundingRect(self) -> QRectF:
        r, tick = _PORT_R_PX, _PORT_TICK_PX
        dx, dy  = _ARROW_DIR[self._port.side]
        return QRectF(
            min(-r, dx * tick - 1),
            min(-r, dy * tick - 1),
            max(r, dx * tick + 1) - min(-r, dx * tick - 1),
            max(r, dy * tick + 1) - min(-r, dy * tick - 1),
        )

    def paint(self, painter: QPainter, option, widget=None) -> None:
        color = QColor(_PORT_ACTIVE if self._active else _PORT_NORMAL)
        r     = _PORT_R_PX

        fill = QColor(color)
        fill.setAlpha(210)
        pen = QPen(color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(0.0, 0.0), r, r)

        dx, dy   = _ARROW_DIR[self._port.side]
        tick_pen = QPen(color, 1.5)
        tick_pen.setCosmetic(True)
        painter.setPen(tick_pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(
            QPointF(dx * r,         dy * r),
            QPointF(dx * _PORT_TICK_PX, dy * _PORT_TICK_PX),
        )


# ── Connection edge indicator ─────────────────────────────────────────────────

_INDICATOR_R_PX   = 3.0
_INDICATOR_GAP_PX = 6.0


class EdgeIndicatorItem(QGraphicsItem):
    """
    Tiny fixed-pixel dot just outside the component edge, signalling a live
    connection on that side.  Uses ItemIgnoresTransformations — always the same
    screen size regardless of zoom.
    """

    def __init__(self, side: PortSide, bbox_local: QRectF,
                 parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self._side = side
        self.setZValue(9)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)

        cx = (bbox_local.left()  + bbox_local.right())  / 2.0
        cy = (bbox_local.top()   + bbox_local.bottom()) / 2.0
        pos = {
            PortSide.NORTH: QPointF(cx,              bbox_local.top()),
            PortSide.SOUTH: QPointF(cx,              bbox_local.bottom()),
            PortSide.WEST:  QPointF(bbox_local.left(), cy),
            PortSide.EAST:  QPointF(bbox_local.right(), cy),
        }[side]
        self.setPos(pos)

    def boundingRect(self) -> QRectF:
        r = _INDICATOR_R_PX + _INDICATOR_GAP_PX + 2.0
        return QRectF(-r, -r, r * 2, r * 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        dx, dy = _ARROW_DIR[self._side]
        cx, cy = dx * _INDICATOR_GAP_PX, dy * _INDICATOR_GAP_PX
        r      = _INDICATOR_R_PX

        fill = QColor(_CONN_FILL)
        fill.setAlpha(180)
        pen = QPen(QColor(_CONN_BORDER), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(cx, cy), r, r)


# ── Group graphics item ───────────────────────────────────────────────────────

_GROUP_BORDER_IDLE    = "#475569"
_GROUP_BORDER_HOVER   = "#38bdf8"
_GROUP_BORDER_EDITING = "#f59e0b"
_GROUP_BG_ALPHA       = 18


class GroupItem(QGraphicsItem):
    """
    Visual container for a ComponentGroup.

    Drag is driven manually — ItemIsMovable is never set.  During drag we call
    move_by() on each member directly so they follow in real-time.  On release
    we revert those live moves and re-apply via MoveGroup so the command stack
    gets one clean, undo-able entry.
    """

    def __init__(self, group: ComponentGroup, scene_ref: "CanvasScene") -> None:
        super().__init__()
        self._group     = group
        self._scene_ref = scene_ref
        self._editing   = False
        self._cached_bbox: Optional[QRectF] = None   # invalidated by invalidate_bbox()

        self.setZValue(1)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)

    def invalidate_bbox(self) -> None:
        """Clear the cached bounding rect so it is recomputed on next access."""
        self._cached_bbox = None

    @property
    def group(self) -> ComponentGroup:
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
        if self._cached_bbox is None:
            bb = self._group.bbox_from(self._scene_ref._design.components)
            self._cached_bbox = QRectF(bb.x_min, bb.y_min,
                                       bb.x_max - bb.x_min, bb.y_max - bb.y_min)
        return self._cached_bbox

    def boundingRect(self) -> QRectF:
        return self._current_bbox().adjusted(-4, -4, 4, 4)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Derive the inner rect from boundingRect() (which Qt already called for
        # clipping) rather than querying _current_bbox() a second time.  A second
        # live model read here could return a different rect if the model changed
        # between Qt's boundingRect() call and paint(), causing the border to be
        # drawn outside the clipped region and appear to jump.
        rect = self.boundingRect().adjusted(4, 4, -4, -4)

        if self._editing:
            border = _GROUP_BORDER_EDITING
        elif self.isSelected():
            border = _GROUP_BORDER_HOVER
        else:
            border = _GROUP_BORDER_IDLE

        pen = QPen(QColor(border), 1.0)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 3])
        painter.setPen(pen)
        fill = QColor(border)
        fill.setAlpha(_GROUP_BG_ALPHA)
        painter.setBrush(QBrush(fill))
        painter.drawRect(rect)

        name_pen = QPen(QColor(border))
        name_pen.setCosmetic(True)
        painter.setPen(name_pen)
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.drawText(QPointF(rect.left() + 4, rect.top() - 2), self._group.name)

    def hoverEnterEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.unsetCursor()
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._editing:
            self._scene_ref.group_selected.emit(self._group.id)
            multi = bool(event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier |
                Qt.KeyboardModifier.ShiftModifier
            ))
            if multi:
                self.setSelected(not self.isSelected())
            elif not self.isSelected():
                self._scene_ref.blockSignals(True)
                self._scene_ref.clearSelection()
                self._scene_ref.blockSignals(False)
                self.setSelected(True)
            self._scene_ref._on_group_press(self, event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._scene_ref._unified_drag_active:
            self._scene_ref._on_unified_move(event)
            event.accept()
            return
        if self._editing:
            super().mouseMoveEvent(event)
            return
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._scene_ref._unified_drag_active:
            self._scene_ref._on_unified_release(event)
            event.accept()
            return
        if self._editing:
            super().mouseReleaseEvent(event)
            return
        event.accept()



# ── Component graphics item ───────────────────────────────────────────────────

class ComponentItem(QGraphicsItem):
    """
    Visual proxy for one GDSComponent.

    A thin shell that owns a child delegate item (Rect/Polygon/Path) and
    centralises all event handling.  The delegate provides shape + paint only;
    interaction logic lives here regardless of geometry type.
    """

    def __init__(self, component: GDSComponent, scene_ref: "CanvasScene") -> None:
        super().__init__()
        self._comp      = component
        self._scene_ref = scene_ref

        # ItemIsMovable is intentionally NOT set here.
        # - Standalone components are moved by _on_unified_move calling comp.move_by()
        #   directly, then sync_from_model() repaints — Qt's built-in move is bypassed.
        # - Group members have ItemIsMovable toggled on/off by _set_group_members_movable
        #   only when the group enters/exits edit mode.
        # Adding ItemIsMovable globally would let Qt move items independently of the
        # model, producing positions that disagree with comp.origin and causing jumps.
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges,
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._delegate:        QGraphicsItem          = self._make_delegate()
        self._port_items:      List[PortItem]         = self._build_port_items()
        self._edge_indicators: List[EdgeIndicatorItem] = []
        self._panel_highlighted: bool                  = False   # beacon from Properties panel hover
        self._apply_style(selected=False, hovered=False)

    # ── Delegate factory ──────────────────────────────────────────────────────

    def _make_delegate(self) -> QGraphicsItem:
        c = self._comp
        if c.kind == ComponentKind.RECTANGLE:
            return QGraphicsRectItem(c.origin.x, c.origin.y, c.width, c.height, self)
        if c.kind == ComponentKind.POLYGON:
            return QGraphicsPolygonItem(self._build_polygon(), self)
        return QGraphicsPathItem(self._build_path(), self)   # PATH

    def _build_polygon(self) -> QPolygonF:
        pts  = self._comp.points or []
        poly = QPolygonF([QPointF(p.x, p.y) for p in pts])
        if pts and pts[0] != pts[-1]:
            poly.append(QPointF(pts[0].x, pts[0].y))
        return poly

    def _build_path(self) -> QPainterPath:
        pts = self._comp.points or []
        if not pts:
            return QPainterPath()
        path = QPainterPath(QPointF(pts[0].x, pts[0].y))
        for pt in pts[1:]:
            path.lineTo(pt.x, pt.y)
        return path

    # ── Required QGraphicsItem overrides ──────────────────────────────────────

    def boundingRect(self) -> QRectF:
        return self._delegate.boundingRect() if self._delegate else QRectF()

    def paint(self, painter, option, widget=None) -> None:
        pass   # delegate paints itself

    def shape(self):
        return self._delegate.shape() if self._delegate else super().shape()

    # ── Style ─────────────────────────────────────────────────────────────────

    def set_panel_highlight(self, active: bool) -> None:
        """
        Activate/deactivate the Properties-panel hover beacon.
        Renders a high-visibility outline + fill independent of selection state
        so users can instantly identify which canvas shape a card refers to.
        """
        if active == self._panel_highlighted:
            return
        self._panel_highlighted = active
        self._apply_style(self.isSelected(), hovered=False)
        # Float above neighbours while highlighted so the outline is never obscured
        self.setZValue(20 if active else 0)

    def _apply_style(self, selected: bool, hovered: bool) -> None:
        layer_color = Colors.LAYER_COLORS[self._comp.layer % len(Colors.LAYER_COLORS)]
        base        = QColor(layer_color)

        if self._panel_highlighted:
            # Beacon style: bright amber border, high-opacity fill
            fill_a, pen_color, pen_w = 160, "#facc15", 2.5
        elif selected:
            fill_a, pen_color, pen_w = 90, Colors.ACCENT, 1.5
        elif hovered:
            fill_a, pen_color, pen_w = 65, base.lighter(150).name(), 1.0
        else:
            fill_a, pen_color, pen_w = 45, layer_color, 0.8

        fill = QColor(base)
        fill.setAlpha(fill_a)
        pen  = _cosmetic(pen_color, pen_w)

        if isinstance(self._delegate, QGraphicsPathItem):
            pw     = self._comp.path_width or DEFAULT_PW_DBU
            stroke = QPen(fill, pw)
            stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self._delegate.setPen(stroke)
            self._delegate.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        else:
            self._delegate.setPen(pen)
            self._delegate.setBrush(QBrush(fill))

        show_ports = selected or hovered
        for pi in self._port_items:
            pi.set_visible_for_state(show_ports)

    # ── Sync ──────────────────────────────────────────────────────────────────

    def sync_from_model(self) -> None:
        c = self._comp

        # ── Delegate type may have changed (rect→polygon after first rotation) ──
        # _rotate_component_in_place promotes RECTANGLE→POLYGON in the model.
        # If the delegate is still a QGraphicsRectItem but the model is now a
        # POLYGON (or PATH), we must replace the delegate so geometry is painted
        # correctly and the bbox used for port recomputation is valid.
        needs_new_delegate = (
            (c.kind == ComponentKind.POLYGON and isinstance(self._delegate, QGraphicsRectItem))
            or (c.kind == ComponentKind.PATH   and isinstance(self._delegate, QGraphicsRectItem))
            or (c.kind == ComponentKind.RECTANGLE and not isinstance(self._delegate, QGraphicsRectItem))
        )
        if needs_new_delegate:
            # Detach old delegate from the scene/parent before replacing it
            old = self._delegate
            old.setParentItem(None)
            if self.scene():
                self.scene().removeItem(old)
            self._delegate = self._make_delegate()
            # Re-apply the current visual style to the fresh delegate
            self._apply_style(self.isSelected(), hovered=False)

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

    def rebuild_ports(self) -> None:
        """
        Sync PortItem positions to match the current model state after any
        geometry change (move, resize, rotation).

        Two cases:
          1. Standalone shape — ports have canonical names N/S/E/W (auto-generated
             by build_default_ports()).  Recompute offsets from current bbox using
             the port's current *side* (which has been rotated correctly by
             _rotate_component_in_place) so they stay on the correct edge after
             rotation.  Port names are NOT used for mapping — they are stale after
             rotation (e.g. a port named "N" may now face WEST after a 90° turn).
          2. Cell anchor — ports have semantic names with explicit offsets set by
             the cell builder.  Do NOT overwrite those offsets; only reposition
             the PortItems to where the model already says.

        Port IDs are never changed — Connection records store IDs and must never
        be invalidated by a geometry update.
        """
        if not self._comp.ports:
            if getattr(self._comp, "_no_auto_ports", False):
                self._port_items = []
                return
            self._comp.build_default_ports()
            self._port_items = self._build_port_items()
            return

        auto_names = {"N", "S", "E", "W"}
        is_auto    = {p.name for p in self._comp.ports} <= auto_names

        if is_auto:
            bb = self._comp.bbox
            cx = (bb.x_min + bb.x_max) // 2
            cy = (bb.y_min + bb.y_max) // 2
            ox, oy = self._comp.origin.x, self._comp.origin.y
            # Map each port to the edge-centre that matches its current *side*
            # (which has already been updated by _rotate_component_in_place).
            # Using port.side — not port.name — is critical after rotation: a port
            # named "N" may face WEST after a 90° CCW turn, and it must sit on
            # the WEST edge of the current bbox, not the top edge.
            side_to_offset = {
                PortSide.NORTH: Point(cx - ox, bb.y_min - oy),
                PortSide.SOUTH: Point(cx - ox, bb.y_max - oy),
                PortSide.WEST:  Point(bb.x_min - ox, cy - oy),
                PortSide.EAST:  Point(bb.x_max - ox, cy - oy),
            }
            for port in self._comp.ports:
                if port.side in side_to_offset:
                    port.offset = side_to_offset[port.side]

        for pi in self._port_items:
            abs_pos = pi.port.abs_pos(self._comp.origin)
            pi.setPos(abs_pos.x, abs_pos.y)

    def set_port_active(self, port_id: str, active: bool) -> None:
        for pi in self._port_items:
            if pi.port.id == port_id:
                pi.set_active(active)
                return

    def clear_port_highlights(self) -> None:
        visible = self.isSelected()
        for pi in self._port_items:
            pi.set_active(False)
            pi.set_visible_for_state(visible)

    @property
    def port_items(self) -> List[PortItem]:
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
        seen: set[PortSide] = set()
        for side in occupied_sides:
            if side not in seen:
                seen.add(side)
                self._edge_indicators.append(EdgeIndicatorItem(side, bbox, self))

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
        event.accept()   # drag handled at scene level

    def mouseReleaseEvent(self, event) -> None:
        event.accept()   # release handled at scene level

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._apply_style(bool(value), hovered=False)
        return super().itemChange(change, value)


# ── Canvas scene ──────────────────────────────────────────────────────────────

class CanvasScene(QGraphicsScene):
    """Master scene. Owns all ComponentItems, the CommandStack, and placement FSM."""

    item_selected           = pyqtSignal(str)
    item_hovered            = pyqtSignal(str)
    cursor_moved            = pyqtSignal(float, float)
    scene_changed           = pyqtSignal()
    mode_changed            = pyqtSignal(str)
    connections_changed     = pyqtSignal()
    multi_selection_changed = pyqtSignal(list)
    group_edit_entered      = pyqtSignal(str)
    group_edit_exited       = pyqtSignal()
    group_selected          = pyqtSignal(str)

    def __init__(self, design: DesignScene, parent=None) -> None:
        super().__init__(parent)
        self._design      = design
        self._items:       Dict[str, ComponentItem] = {}
        self._group_items: Dict[str, GroupItem]     = {}
        self._editing_group_id: Optional[str]       = None

        self.cmd_stack = CommandStack(design)
        self.cmd_stack.connect_change(self._on_model_changed)

        ext = SCENE_EXTENT
        self.setSceneRect(-ext, -ext, ext * 2, ext * 2)
        self.setBackgroundBrush(QBrush(QColor(Colors.CANVAS_BG)))

        self._pl      = PlacementState()
        self._pl.mode = PlacementMode.SELECT

        # Last known cursor scene position — used by paste() to place at cursor.
        self._cursor_scene_pos: QPointF = QPointF(0, 0)

        # Unified drag state (components + groups move together).
        self._drag_start:            Optional[QPointF] = None
        self._drag_last:             Optional[QPointF] = None
        self._drag_committed:        bool              = False
        self._snap_offset:           Optional[object]  = None
        self._orig_comp_positions:   dict              = {}   # ComponentItem → Point
        self._orig_group_positions:  dict              = {}   # group_id → {comp_id: Point}

        self.selectionChanged.connect(self._on_selection_changed)
        self.group_edit_entered.connect(self._on_group_edit_entered)

        # Undercut ring overlay — zero-config; wired to selectionChanged +
        # scene_changed internally.  Disabled by default; toggle via
        # self._undercut.toggle() or self._undercut.enable(True).
        self._undercut = UndercutOverlay(self)

        # The obj_id (comp or group) whose ring is being masked in
        # MASK_UNDERCUT mode.  Empty string when not in mask mode.
        self._mask_target_id:  str            = ""
        self._erase_drag_start: Optional[QPointF] = None
        self._erase_ghost:      Optional[object]  = None

        # Ruler state
        self._ruler_item:  Optional[RulerItem] = None
        self._ruler_dragging: bool             = False

    # ── Public placement API ──────────────────────────────────────────────────

    @property
    def mode(self) -> PlacementMode:
        return self._pl.mode

    def set_mode(self, mode: PlacementMode, layer: int = 0) -> None:
        self._clear_ghosts()
        self._pl.mode  = mode
        self._pl.layer = layer
        self.mode_changed.emit(mode.status_label)

    def enter_mask_undercut_mode(self, target_id: str) -> None:
        """
        Activate the undercut-mask eraser for *target_id* (a comp or group id).
        The user click-drags a rectangle; on release the rect is subtracted from
        that object's ring via UndercutOverlay.add_mask().  Purely visual.
        """
        self._mask_target_id  = target_id
        self._erase_drag_start: Optional[QPointF] = None
        self._erase_ghost:      Optional[object]  = None
        self._clear_ghosts()
        self._pl.mode = PlacementMode.MASK_UNDERCUT
        self.mode_changed.emit(PlacementMode.MASK_UNDERCUT.status_label)

    def cancel_placement(self) -> None:
        self._clear_ghosts()
        self.clear_ruler()
        self._mask_target_id = ""
        self._erase_drag_start = None
        if self._erase_ghost is not None:
            self.removeItem(self._erase_ghost)
            self._erase_ghost = None
        self._pl.mode = PlacementMode.SELECT
        self.mode_changed.emit(PlacementMode.SELECT.status_label)

    def clear_ruler(self) -> None:
        """Remove any active ruler from the scene."""
        if self._ruler_item is not None:
            self.removeItem(self._ruler_item)
            self._ruler_item = None
        self._ruler_dragging = False

    # ── Public scene API ──────────────────────────────────────────────────────

    def refresh_item_style(self, comp_id: str) -> None:
        """Re-apply layer colour after an undo-able layer edit."""
        item = self._items.get(comp_id)
        if item:
            item._apply_style(item.isSelected(), hovered=False)

    def reset(self) -> None:
        """Clear all items and ghosts — called by MainWindow on new design."""
        self._clear_ghosts()
        self._on_model_changed()

    def place_rectangle(self, origin_x_um: float, origin_y_um: float,
                        width_um: float, height_um: float, layer: int = 0) -> GDSComponent:
        """Programmatic rectangle placement (tests, toolbar quick-place)."""
        comp = GDSComponent(
            kind   = ComponentKind.RECTANGLE,
            layer  = layer,
            origin = Point.from_um(origin_x_um, origin_y_um),
            width  = um_to_dbu(width_um),
            height = um_to_dbu(height_um),
        )
        self.cmd_stack.execute(AddComponent(comp))
        return comp

    def drop_shape(self, kind_val: int, layer: int, scene_pos: QPointF) -> None:
        """
        Called by CanvasView.dropEvent after a drag from the shape palette.
        kind_val — raw ComponentKind int value from MIME data.
        """
        try:
            kind = ComponentKind(kind_val)
        except ValueError:
            return

        snapped = self.snap_f(scene_pos.x(), scene_pos.y())
        cx, cy  = int(snapped.x()), int(snapped.y())
        hw, hh  = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2

        if kind == ComponentKind.RECTANGLE:
            comp = GDSComponent(
                kind=ComponentKind.RECTANGLE, layer=layer,
                origin=Point(cx - hw, cy - hh),
                width=DEFAULT_W_DBU, height=DEFAULT_H_DBU,
            )
        elif kind == ComponentKind.POLYGON:
            pts  = [Point(cx, cy - hh), Point(cx + hw, cy + hh), Point(cx - hw, cy + hh)]
            comp = GDSComponent(kind=ComponentKind.POLYGON, layer=layer,
                                origin=pts[0], points=pts)
        elif kind == ComponentKind.PATH:
            pts  = [Point(cx - hw, cy), Point(cx + hw, cy)]
            comp = GDSComponent(kind=ComponentKind.PATH, layer=layer,
                                origin=pts[0], points=pts, path_width=DEFAULT_PW_DBU)
        else:
            return

        self.cmd_stack.execute(AddComponent(comp))

    def snap(self, pt: Point) -> Point:
        g = GRID_MINOR_DBU
        return Point(round(pt.x / g) * g, round(pt.y / g) * g)

    def snap_f(self, x: float, y: float) -> QPointF:
        g = float(GRID_MINOR_DBU)
        return QPointF(round(x / g) * g, round(y / g) * g)

    def item_for(self, comp_id: str) -> Optional[ComponentItem]:
        return self._items.get(comp_id)

    # ── Properties-panel hover beacon ─────────────────────────────────────────

    _highlighted_comp_id: str = ""

    def highlight_component(self, comp_id: str) -> None:
        """
        Highlight the ComponentItem for *comp_id* with a high-visibility beacon
        style (amber outline, bright fill) to help users identify which canvas
        shape corresponds to a row in the Properties panel.

        Pass an empty string (or a non-existent ID) to clear any current highlight.
        Any previously highlighted item is always cleared first.
        """
        # Clear previous highlight
        if self._highlighted_comp_id:
            prev = self._items.get(self._highlighted_comp_id)
            if prev is not None:
                prev.set_panel_highlight(False)

        self._highlighted_comp_id = comp_id

        if comp_id:
            item = self._items.get(comp_id)
            if item is not None:
                item.set_panel_highlight(True)
                # Ensure the highlighted item is visible in the viewport —
                # scroll to it if it is off-screen (non-destructive to zoom).
                views = self.views()
                if views:
                    views[0].ensureVisible(item, 60, 60)

    def clear_all_port_highlights(self) -> None:
        for item in self._items.values():
            item.clear_port_highlights()

    # ── Port snap ─────────────────────────────────────────────────────────────

    def find_port_snap(self, moving_comp: GDSComponent,
                       tentative_origin: Point) -> Optional[tuple]:
        """
        Check whether any port on *moving_comp* (placed at *tentative_origin*)
        is within PORT_SNAP_RADIUS of a compatible port on another component.
        Snapping is proximity-only — no side-compatibility check.

        Returns (snapped_origin, my_port_id, their_port_id, their_comp_id)
        or None.  snapped_origin is the exact origin placing my_port flush
        against their_port.
        """
        best_dist = PORT_SNAP_RADIUS
        best      = None

        for port in moving_comp.ports:
            my_abs = Point(
                tentative_origin.x + port.offset.x,
                tentative_origin.y + port.offset.y,
            )
            for other_comp in self._design.components:
                if other_comp.id == moving_comp.id:
                    continue
                for other_port in other_comp.ports:
                    their_abs = other_port.abs_pos(other_comp.origin)
                    dx   = my_abs.x - their_abs.x
                    dy   = my_abs.y - their_abs.y
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist < best_dist:
                        best_dist = dist
                        snapped   = Point(tentative_origin.x - dx, tentative_origin.y - dy)
                        best      = (snapped, port.id, other_port.id, other_comp.id)

        return best

    def find_group_port_snap(self, moving_group: ComponentGroup) -> Optional[tuple]:
        """
        Check whether any port on any member of *moving_group* is within
        PORT_SNAP_RADIUS of any port outside the group.

        Returns (extra_dx, extra_dy, my_comp_id, my_port_id,
                 their_comp_id, their_port_id) or None.
        """
        member_ids = set(moving_group.member_ids)
        best_dist  = PORT_SNAP_RADIUS
        best       = None

        for my_comp in self._design.components:
            if my_comp.id not in member_ids:
                continue
            for my_port in my_comp.ports:
                my_abs = my_port.abs_pos(my_comp.origin)
                for other_comp in self._design.components:
                    if other_comp.id in member_ids:
                        continue
                    for other_port in other_comp.ports:
                        their_abs = other_port.abs_pos(other_comp.origin)
                        dx   = my_abs.x - their_abs.x
                        dy   = my_abs.y - their_abs.y
                        dist = math.sqrt(dx * dx + dy * dy)
                        if dist < best_dist:
                            best_dist = dist
                            best      = (-dx, -dy,
                                         my_comp.id, my_port.id,
                                         other_comp.id, other_port.id)
        return best

    def _try_connect_snapped(self, moving_comp: GDSComponent,
                              final_origin: Point) -> None:
        """Wire the snapped port pair after a component move commits."""
        result = self.find_port_snap(moving_comp, final_origin)
        if result is None:
            return
        _, my_port_id, their_port_id, their_comp_id = result
        if not self._design.are_connected(
            moving_comp.id, my_port_id, their_comp_id, their_port_id
        ):
            self.cmd_stack.execute(
                ConnectPorts(moving_comp.id, my_port_id, their_comp_id, their_port_id)
            )
        self._refresh_indicators(moving_comp.id)
        self._refresh_indicators(their_comp_id)
        self.connections_changed.emit()

    def _try_connect_group_snap(self, group: ComponentGroup) -> None:
        """Wire the snapped port pair after a group move commits."""
        snap = self.find_group_port_snap(group)
        if snap is None:
            # Even with no new snap, all group members had their connections
            # cleared in _on_unified_release — refresh their indicators so
            # the edge-indicator arrows don't linger in a stale state.
            for cid in group.member_ids:
                self._refresh_indicators(cid)
            return
        _, _, my_comp_id, my_port_id, their_comp_id, their_port_id = snap
        # Disconnect only external connections on the snapping component so
        # internal group connections are not inadvertently severed.
        group_member_ids = set(group.member_ids)
        for conn in self._design.connections_for(my_comp_id):
            other_id = (conn.comp_b if conn.comp_a == my_comp_id else conn.comp_a)
            if other_id not in group_member_ids:
                self.cmd_stack.execute(DisconnectPorts(conn))
        if not self._design.are_connected(
            my_comp_id, my_port_id, their_comp_id, their_port_id
        ):
            self.cmd_stack.execute(
                ConnectPorts(my_comp_id, my_port_id, their_comp_id, their_port_id)
            )
        # Refresh all group members (their connections were cleared on drag start)
        # and the external target component.
        for cid in group.member_ids:
            self._refresh_indicators(cid)
        self._refresh_indicators(their_comp_id)
        self.connections_changed.emit()

    def disconnect_component(self, comp_id: str) -> None:
        """Sever every connection on comp_id via undo-aware commands."""
        for conn in self._design.connections_for(comp_id):
            self.cmd_stack.execute(DisconnectPorts(conn))

    def _refresh_indicators(self, comp_id: str) -> None:
        item = self._items.get(comp_id)
        if item:
            item.refresh_connection_state(self._design)

    def refresh_all_indicators(self) -> None:
        """Rebuild all edge indicators — call after undo/redo."""
        for item in self._items.values():
            item.refresh_connection_state(self._design)

    # ── Mouse events ──────────────────────────────────────────────────────────

    def _hit_item(self, scene_pos, item_type: type) -> Optional[object]:
        """Walk the item hierarchy at scene_pos and return the nearest item of item_type, or None."""
        transform = self.views()[0].transform() if self.views() else QTransform()
        candidate = self.itemAt(scene_pos, transform)
        while candidate is not None:
            if isinstance(candidate, item_type):
                return candidate
            candidate = candidate.parentItem()
        return None

    def _hit_component_item(self, scene_pos) -> Optional[ComponentItem]:
        return self._hit_item(scene_pos, ComponentItem)

    def _hit_group_item(self, scene_pos) -> Optional[GroupItem]:
        return self._hit_item(scene_pos, GroupItem)

    def mousePressEvent(self, event) -> None:
        # Exit group edit when clicking outside the group.
        if self._editing_group_id:
            group    = self._design.get_group(self._editing_group_id)
            hit_comp = getattr(self._hit_component_item(event.scenePos()), "component", None)
            if group and not (hit_comp and hit_comp.id in group.member_ids):
                self.exit_group_edit()
                self.clearSelection()

        snapped = self.snap_f(event.scenePos().x(), event.scenePos().y())

        # Placement modes intercept all clicks.
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

        if self._pl.mode == PlacementMode.MASK_UNDERCUT:
            if event.button() == Qt.MouseButton.LeftButton:
                self._erase_drag_start = event.scenePos()
            return

        if self._pl.mode == PlacementMode.RULER:
            if event.button() == Qt.MouseButton.LeftButton:
                # Replace any existing ruler with a fresh one starting here.
                self.clear_ruler()
                self._ruler_item = RulerItem(snapped, self)
                self.addItem(self._ruler_item)
                self._ruler_dragging = True
            return

        # Select mode.
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        comp_item = self._hit_component_item(event.scenePos())
        if comp_item is not None:
            self._on_item_press(comp_item, event)
            return

        group_item = self._hit_group_item(event.scenePos())
        if group_item is not None and not group_item.is_editing:
            # Let GroupItem.mousePressEvent own selection + drag.
            super().mousePressEvent(event)
            return

        # Empty canvas — clear selection unless a modifier is held.
        if not bool(event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        )):
            self.clearSelection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        raw = event.scenePos()
        self._cursor_scene_pos = raw
        self.cursor_moved.emit(dbu_to_um(int(raw.x())), dbu_to_um(int(raw.y())))

        snapped = self.snap_f(raw.x(), raw.y())
        if self._pl.mode == PlacementMode.PLACE_RECT:
            self._update_ghost_rect(snapped)
        elif self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
            self._update_ghost_edge(snapped)
        elif self._pl.mode == PlacementMode.MASK_UNDERCUT and self._erase_drag_start is not None:
            self._update_erase_ghost(event.scenePos())
        elif self._pl.mode == PlacementMode.RULER and self._ruler_dragging and self._ruler_item:
            self._ruler_item.update_end(snapped)

        if self._unified_drag_active and (self._orig_comp_positions or self._orig_group_positions):
            self._on_unified_move(event)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._pl.mode == PlacementMode.MASK_UNDERCUT
                and self._erase_drag_start is not None):
            self._commit_erase_rect(event.scenePos())
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self._unified_drag_active
                and (self._orig_comp_positions or self._orig_group_positions)):
            self._on_unified_release(event)
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self._pl.mode == PlacementMode.RULER
                and self._ruler_dragging
                and self._ruler_item is not None):
            snapped = self.snap_f(event.scenePos().x(), event.scenePos().y())
            self._ruler_item.commit(snapped)
            self._ruler_dragging = False
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        key  = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if key == Qt.Key.Key_Escape:
            self.cancel_placement()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._pl.mode in (PlacementMode.PLACE_POLYGON, PlacementMode.PLACE_PATH):
                self._commit_poly_or_path()
        elif key == Qt.Key.Key_C and ctrl:
            self.copy_selection()
        elif key == Qt.Key.Key_V and ctrl:
            self.paste()
        elif key == Qt.Key.Key_D and ctrl:
            self.duplicate_selection()
        elif key == Qt.Key.Key_R and self._pl.mode == PlacementMode.SELECT:
            self.rotate_selection(ccw=bool(mods & Qt.KeyboardModifier.ShiftModifier))
        else:
            super().keyPressEvent(event)

    # ── Unified drag ──────────────────────────────────────────────────────────

    @property
    def _unified_drag_active(self) -> bool:
        return self._drag_start is not None

    def _on_item_press(self, item: ComponentItem, event) -> None:
        """Called from mousePressEvent — handles select + drag arm."""
        multi = bool(event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ))
        if multi:
            item.setSelected(not item.isSelected())
        elif not item.isSelected():
            # Block signals during clearSelection so the intermediate
            # "0 items selected" state never fires selectionChanged and
            # clears the properties panel before setSelected re-fills it.
            self.blockSignals(True)
            self.clearSelection()
            self.blockSignals(False)
            item.setSelected(True)   # fires selectionChanged once, with final state

        self._arm_unified_drag(event)

    def _on_group_press(self, gi: GroupItem, event) -> None:
        """Called by GroupItem.mousePressEvent — arms unified drag from a group press."""
        self._arm_unified_drag(event)

    def _arm_unified_drag(self, event) -> None:
        """Snapshot origins for ALL currently selected items."""
        self._drag_start     = event.scenePos()
        self._drag_last      = event.scenePos()
        self._drag_committed = False
        self._snap_offset    = None

        # Collect all component IDs that belong to a selected non-editing group.
        # These must NOT also appear in _orig_comp_positions — if they did, each
        # component would be moved twice during the drag (once via the group path
        # and once via the standalone-component path), causing a jump on release.
        group_member_ids: set[str] = set()
        self._orig_group_positions = {}
        for gi in self.selectedItems():
            if not isinstance(gi, GroupItem) or gi.is_editing:
                continue
            group_member_ids.update(gi.group.member_ids)
            self._orig_group_positions[gi.group.id] = {
                cid: Point(comp.origin.x, comp.origin.y)
                for cid in gi.group.member_ids
                if (comp := self._design.get(cid)) is not None
            }

        self._orig_comp_positions = {
            i: Point(i._comp.origin.x, i._comp.origin.y)
            for i in self.selectedItems()
            if isinstance(i, ComponentItem)
            and i._comp.id not in group_member_ids
        }

    # Legacy aliases kept for any external call sites.
    def _on_item_move(self, event) -> None:
        self._on_unified_move(event)

    def _on_item_release(self, event) -> None:
        self._on_unified_release(event)

    def _on_unified_move(self, event) -> None:
        """
        Move ALL selected items (ComponentItems and GroupItems) together.
        Called from scene.mouseMoveEvent and forwarded here from
        GroupItem.mouseMoveEvent.
        """
        if self._drag_start is None:
            return
        delta = event.scenePos() - self._drag_start
        if not self._drag_committed:
            if abs(delta.x()) < 3.0 and abs(delta.y()) < 3.0:
                return
            self._drag_committed = True

        # Move standalone ComponentItems.
        for item, orig in self._orig_comp_positions.items():
            new_x = orig.x + int(round(delta.x()))
            new_y = orig.y + int(round(delta.y()))
            dx    = new_x - item._comp.origin.x
            dy    = new_y - item._comp.origin.y
            if dx or dy:
                item._comp.move_by(dx, dy)
                item.sync_from_model()

        # Move GroupItem members.
        for group_id, origins in self._orig_group_positions.items():
            group = self._design.get_group(group_id)
            if not group:
                continue
            for cid, orig in origins.items():
                comp = self._design.get(cid)
                if comp:
                    dx = orig.x + int(round(delta.x())) - comp.origin.x
                    dy = orig.y + int(round(delta.y())) - comp.origin.y
                    if dx or dy:
                        comp.move_by(dx, dy)
                        item = self._items.get(cid)
                        if item:
                            item.sync_from_model()
                            item.refresh_connection_state(self._design)
            gi = self._group_items.get(group_id)
            if gi:
                gi.invalidate_bbox()
                gi.prepareGeometryChange()

        self.clear_all_port_highlights()
        self._snap_offset = None

        # Port snap: single component drag.
        if len(self._orig_comp_positions) == 1 and not self._orig_group_positions:
            item   = next(iter(self._orig_comp_positions))
            orig   = self._orig_comp_positions[item]
            tentative = Point(orig.x + int(round(delta.x())),
                              orig.y + int(round(delta.y())))
            snap = self.find_port_snap(item._comp, tentative)
            if snap:
                snap_origin, my_port_id, their_port_id, their_comp_id = snap
                self._snap_offset = snap_origin
                item.set_port_active(my_port_id, True)
                other = self.item_for(their_comp_id)
                if other:
                    other.set_port_active(their_port_id, True)

        # Port snap: single group drag.
        elif len(self._orig_group_positions) == 1 and not self._orig_comp_positions:
            group_id = next(iter(self._orig_group_positions))
            group    = self._design.get_group(group_id)
            if group:
                snap = self.find_group_port_snap(group)
                if snap:
                    extra_dx, extra_dy, my_comp_id, my_port_id, their_comp_id, their_port_id = snap
                    self._snap_offset = ("group", group_id, extra_dx, extra_dy)

                    # Apply the snap correction visually so the group jumps flush
                    # to the target port during drag — matching single-component
                    # snap behaviour.  The live model positions are already at the
                    # tentative drag location; we nudge by the residual gap only.
                    if extra_dx or extra_dy:
                        for cid in group.member_ids:
                            comp = self._design.get(cid)
                            if comp:
                                comp.move_by(extra_dx, extra_dy)
                                snap_item_vis = self._items.get(cid)
                                if snap_item_vis:
                                    snap_item_vis.sync_from_model()
                        gi = self._group_items.get(group_id)
                        if gi:
                            gi.invalidate_bbox()
                            gi.prepareGeometryChange()

                    my_item = self.item_for(my_comp_id)
                    if my_item:
                        my_item.set_port_active(my_port_id, True)
                    other = self.item_for(their_comp_id)
                    if other:
                        other.set_port_active(their_port_id, True)

    def _on_unified_release(self, event) -> None:
        """
        Commit the drag as one BatchCommand so a single Undo reverses everything.
        """
        self.clear_all_port_highlights()
        has_comps  = bool(self._orig_comp_positions)
        has_groups = bool(self._orig_group_positions)

        if not self._drag_committed or (not has_comps and not has_groups):
            self._reset_drag_state()
            return

        delta     = event.scenePos() - self._drag_start
        move_cmds = []

        # Component move commands.
        snap_item = None
        for item, orig in self._orig_comp_positions.items():
            if (isinstance(self._snap_offset, Point)
                    and len(self._orig_comp_positions) == 1
                    and not has_groups):
                final     = self._snap_offset
                snap_item = item
            else:
                raw   = Point(orig.x + int(round(delta.x())),
                              orig.y + int(round(delta.y())))
                final = self.snap(raw)

            # Revert live move so MoveComponent records correct before/after.
            item._comp.move_by(orig.x - item._comp.origin.x,
                               orig.y - item._comp.origin.y)
            if final != orig:
                self.disconnect_component(item._comp.id)
                move_cmds.append(MoveComponent(item._comp.id, orig, final))
            item.sync_from_model()

        # Group move commands.
        snapped_group_id = None
        for group_id, origins in self._orig_group_positions.items():
            group = self._design.get_group(group_id)
            if not group:
                continue

            is_snapped = (
                isinstance(self._snap_offset, tuple)
                and self._snap_offset[0] == "group"
                and self._snap_offset[1] == group_id
            )
            if is_snapped:
                snapped_group_id = group_id

            # Derive total displacement from the live model position of the first
            # member rather than from delta+extra_snap.  The visual snap nudge in
            # _on_unified_move already applied extra_dx/dy to comp.origin, so
            # reading the live position gives the exact committed displacement
            # without any risk of applying the snap correction twice.
            first_cid  = next(iter(origins), None)
            first_comp = self._design.get(first_cid) if first_cid else None
            if first_comp and first_cid in origins:
                total_dx = first_comp.origin.x - origins[first_cid].x
                total_dy = first_comp.origin.y - origins[first_cid].y
            else:
                total_dx, total_dy = int(round(delta.x())), int(round(delta.y()))

            # Revert live move so MoveGroup records correct before/after.
            for cid, orig in origins.items():
                comp = self._design.get(cid)
                if comp:
                    comp.move_by(orig.x - comp.origin.x, orig.y - comp.origin.y)

            if total_dx or total_dy:
                # Disconnect only EXTERNAL connections (one end inside the group,
                # the other outside).  Internal connections between group members
                # must be preserved — they don't need to be re-snapped after a
                # move because the relative positions of members don't change.
                member_id_set = set(group.member_ids)
                for cid in group.member_ids:
                    for conn in self._design.connections_for(cid):
                        other_id = (conn.comp_b if conn.comp_a == cid
                                    else conn.comp_a)
                        if other_id not in member_id_set:
                            self.cmd_stack.execute(DisconnectPorts(conn))
                move_cmds.append(MoveGroup(group_id, total_dx, total_dy))

        if move_cmds:
            cmd = (move_cmds[0] if len(move_cmds) == 1
                   else BatchCommand(move_cmds, f"Move {len(move_cmds)} items"))
            self.cmd_stack.execute(cmd)

        if snap_item is not None and isinstance(self._snap_offset, Point):
            self._try_connect_snapped(snap_item._comp, self._snap_offset)

        if snapped_group_id is not None:
            group = self._design.get_group(snapped_group_id)
            if group:
                self._try_connect_group_snap(group)

        self._reset_drag_state()

    def _reset_drag_state(self) -> None:
        self._drag_start           = None
        self._drag_last            = None
        self._drag_committed       = False
        self._snap_offset          = None
        self._orig_comp_positions  = {}
        self._orig_group_positions = {}

    # ── Rotation ──────────────────────────────────────────────────────────────

    def rotate_selection(self, ccw: bool = False) -> None:
        """
        Rotate all selected items by 90°.

        ccw=False → 90° CW (default; matches most EDA tools).
        ccw=True  → 90° CCW.

        Groups rotate as a unit around their shared bbox centre.
        Loose components rotate around their own bbox centre.
        Mixed selections rotate each item around its own centre independently.
        The entire operation is one BatchCommand so Ctrl+Z is atomic.
        """
        steps = 1 if ccw else 3   # 3 ≡ −1 (mod 4) → 90° CW

        sel         = self.selectedItems()
        comp_items  = [i for i in sel if isinstance(i, ComponentItem)]
        group_items = [i for i in sel if isinstance(i, GroupItem)]

        if not comp_items and not group_items:
            return

        cmds: List = []
        grouped_ids: set[str] = set()

        for gi in group_items:
            member_comps = [
                c for c in self._design.components if c.id in gi.group.member_ids
            ]
            grouped_ids.update(gi.group.member_ids)
            if member_comps:
                cmds.append(RotateGroup(gi.group, member_comps, steps=steps))

        for ci in comp_items:
            if ci.component.id not in grouped_ids:
                cmds.append(RotateComponent(ci.component, steps=steps))

        if not cmds:
            return

        if len(cmds) == 1:
            self.cmd_stack.execute(cmds[0])
        else:
            deg = steps * 90 % 360
            self.cmd_stack.execute(
                BatchCommand(cmds, f"Rotate {len(cmds)} item(s) {deg}°")
            )

        # Rebuild delegates for any rect→polygon type promotion from rotation.
        for ci in comp_items:
            if ci.component.id not in grouped_ids:
                self._rebuild_delegate_if_needed(ci)
        for gi in group_items:
            for cid in gi.group.member_ids:
                item = self._items.get(cid)
                if item:
                    self._rebuild_delegate_if_needed(item)

    def _rebuild_delegate_if_needed(self, item: ComponentItem) -> None:
        """
        Replace the Qt delegate if a rectangle was promoted to a polygon by
        rotation (kind changed from RECTANGLE → POLYGON).
        """
        if (isinstance(item._delegate, QGraphicsRectItem)
                and item._comp.kind != ComponentKind.RECTANGLE):
            item._delegate.setParentItem(None)
            item._delegate = item._make_delegate()
            item._apply_style(item.isSelected(), hovered=False)
            item.prepareGeometryChange()
        else:
            item.sync_from_model()

    # ── Placement helpers ─────────────────────────────────────────────────────

    def _stamp_rect(self, center: QPointF) -> None:
        hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
        comp = GDSComponent(
            kind=ComponentKind.RECTANGLE, layer=self._pl.layer,
            origin=Point(int(center.x()) - hw, int(center.y()) - hh),
            width=DEFAULT_W_DBU, height=DEFAULT_H_DBU,
        )
        self.cmd_stack.execute(AddComponent(comp))

    def _add_vertex(self, pos: QPointF) -> None:
        self._pl.pts.append(pos)
        r   = float(VERTEX_DOT_R)
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
            ghost = QGraphicsRectItem(rect)
            c     = QColor(Colors.ACCENT)
            c.setAlpha(25)
            ghost.setPen(_cosmetic(Colors.ACCENT, 1.0, Qt.PenStyle.DashLine))
            ghost.setBrush(QBrush(c))
            ghost.setZValue(5)
            self.addItem(ghost)
            self._pl.ghost_rect = ghost
        else:
            self._pl.ghost_rect.setRect(rect)

    def _refresh_ghost_poly(self) -> None:
        pts = self._pl.pts
        if len(pts) < 2:
            return
        poly = QPolygonF(pts)
        if self._pl.ghost_poly is None:
            ghost = QGraphicsPolygonItem(poly)
            ghost.setPen(_cosmetic(Colors.ACCENT, 1.0, Qt.PenStyle.DashLine))
            ghost.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ghost.setZValue(4)
            self.addItem(ghost)
            self._pl.ghost_poly = ghost
        else:
            self._pl.ghost_poly.setPolygon(poly)

    def _update_ghost_edge(self, cursor: QPointF) -> None:
        if not self._pl.pts:
            return
        last = self._pl.pts[-1]
        if self._pl.ghost_edge is None:
            ghost = QGraphicsLineItem(last.x(), last.y(), cursor.x(), cursor.y())
            ghost.setPen(_cosmetic(Colors.ACCENT_BRIGHT, 1.0, Qt.PenStyle.DotLine))
            ghost.setZValue(6)
            self.addItem(ghost)
            self._pl.ghost_edge = ghost
        else:
            self._pl.ghost_edge.setLine(last.x(), last.y(), cursor.x(), cursor.y())

    def _commit_poly_or_path(self) -> None:
        pts     = self._pl.pts
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
        self._clear_ghosts()

    def _update_erase_ghost(self, current: QPointF) -> None:
        """Draw/update a dashed orange rectangle showing the erase area."""
        if self._erase_drag_start is None:
            return
        rect = QRectF(self._erase_drag_start, current).normalized()
        if self._erase_ghost is None:
            ghost = QGraphicsRectItem(rect)
            c = QColor("#fb923c")   # orange — matches ring colour
            c.setAlpha(40)
            ghost.setPen(_cosmetic("#fb923c", 1.5, Qt.PenStyle.DashLine))
            ghost.setBrush(QBrush(c))
            ghost.setZValue(20)
            self.addItem(ghost)
            self._erase_ghost = ghost
        else:
            self._erase_ghost.setRect(rect)

    def _commit_erase_rect(self, end: QPointF) -> None:
        """
        On mouse release: subtract the dragged rectangle from the target ring
        and return to SELECT mode.  If the drag was too small (< 2 px), ignore.
        """
        start = self._erase_drag_start
        # Clean up ghost first.
        if self._erase_ghost is not None:
            self.removeItem(self._erase_ghost)
            self._erase_ghost = None
        self._erase_drag_start = None

        if start is None:
            return
        rect = QRectF(start, end).normalized()
        if rect.width() < 2.0 or rect.height() < 2.0:
            # Accidental click — stay in erase mode for next drag.
            return

        if self._mask_target_id:
            mask_path = QPainterPath()
            mask_path.addRect(rect)
            self._undercut.add_mask(self._mask_target_id, mask_path)
            self._design.is_dirty = True          # ← add this one line
            self.scene_changed.emit()             # ← triggers title update
        # Stay in MASK_UNDERCUT mode so multiple rectangles can be drawn
        # without re-pressing X each time.  ESC returns to SELECT.

    def _clear_ghosts(self) -> None:
        for item in (self._pl.ghost_rect, self._pl.ghost_poly, self._pl.ghost_edge):
            if item is not None:
                self.removeItem(item)
        for dot in self._pl.vertex_dots:
            self.removeItem(dot)
        mode  = self._pl.mode  if self._pl.mode  is not None else PlacementMode.SELECT
        layer = self._pl.layer
        self._pl       = PlacementState()
        self._pl.mode  = mode
        self._pl.layer = layer

    # ── Copy / paste / duplicate ──────────────────────────────────────────────

    def copy_selection(self) -> None:
        """
        Copy selected items to the Clipboard.

        - Single GroupItem selected → copy all its members + group metadata.
        - Multiple GroupItems selected → merge members, drop group metadata.
        - Loose ComponentItems only → copy them; carry group metadata if they
          all belong to exactly one shared group.
        """
        sel         = self.selectedItems()
        group_items = [i for i in sel if isinstance(i, GroupItem)]
        comp_items  = [i for i in sel if isinstance(i, ComponentItem)]

        components: list[GDSComponent] = []
        group: Optional[ComponentGroup] = None

        if group_items:
            if len(group_items) == 1:
                gi         = group_items[0]
                group      = gi.group
                components = [c for c in self._design.components
                              if c.id in group.member_ids]
            else:
                seen_ids: set[str] = set()
                for gi in group_items:
                    for cid in gi.group.member_ids:
                        if cid not in seen_ids:
                            comp = self._design.get(cid)
                            if comp:
                                components.append(comp)
                                seen_ids.add(cid)
        else:
            if not comp_items:
                return
            components = [i.component for i in comp_items]
            if len(comp_items) > 1:
                groups = {self._design.group_of(c.id) for c in components}
                groups.discard(None)
                if len(groups) == 1:
                    group = next(iter(groups))

        if not components:
            return

        cb = Clipboard.instance()
        # Collect all connections that are fully internal to the copied set so
        # they can be remapped and restored on paste.
        comp_ids = {c.id for c in components}
        intra_connections = [
            cn for cn in self._design.connections
            if cn.comp_a in comp_ids and cn.comp_b in comp_ids
        ]
        cb.copy(components, group, intra_connections)
        cb.reset_paste_count()

    def paste(self) -> None:
        """
        Paste from the Clipboard centred on the current cursor position.
        Falls back to viewport centre if the cursor hasn't moved over the canvas.
        Consecutive pastes are staggered by PASTE_OFFSET_DBU × paste_count.
        Newly pasted items are selected immediately.
        """
        cb = Clipboard.instance()
        if cb.is_empty:
            return

        sp = self._cursor_scene_pos
        if sp.isNull() and self.views():
            vr = self.views()[0].viewport().rect()
            sp = self.views()[0].mapToScene(vr.center())
        target_center = (int(sp.x()), int(sp.y()))

        components, group, connections = cb.paste(
            base_offset_dbu=PASTE_OFFSET_DBU,
            target_center=target_center,
        )
        self.cmd_stack.execute(PasteComponents(components, group, connections))

        self.clearSelection()
        for comp in components:
            item = self._items.get(comp.id)
            if item:
                item.setSelected(True)

    def duplicate_selection(self) -> None:
        """Copy + paste in one gesture (Ctrl+D). Paste lands at +1 × PASTE_OFFSET."""
        self.copy_selection()
        self.paste()

    def drop_cell(self, cell_id: str, scene_pos: Optional[QPointF] = None,
                  params: Optional[dict] = None) -> None:
        if scene_pos is None:
            view      = self.views()[0] if self.views() else None
            scene_pos = view.mapToScene(view.viewport().rect().center()) if view else QPointF(0, 0)

        origin = Point(int(scene_pos.x()), int(scene_pos.y()))
        result = place_cell(cell_id, origin, params=params)
        self.cmd_stack.execute(PlaceCellCommand(result, cell_id=cell_id, cell_params=params or {}, cell_origin=origin))

    # ── Background ────────────────────────────────────────────────────────────

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        self._draw_grid(painter, rect)
        self._draw_origin(painter)

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        left   = int(math.floor(rect.left()   / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        top    = int(math.floor(rect.top()    / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        right  = int(math.ceil(rect.right()   / GRID_MINOR_DBU)) * GRID_MINOR_DBU
        bottom = int(math.ceil(rect.bottom()  / GRID_MINOR_DBU)) * GRID_MINOR_DBU

        minor_pen = QPen(QColor(Colors.GRID_MINOR)); minor_pen.setCosmetic(True); minor_pen.setWidthF(0.5)
        major_pen = QPen(QColor(Colors.GRID_MAJOR)); major_pen.setCosmetic(True); major_pen.setWidthF(0.8)

        painter.save()
        x = left
        while x <= right:
            painter.setPen(major_pen if x % GRID_MAJOR_DBU == 0 else minor_pen)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            x += GRID_MINOR_DBU
        y = top
        while y <= bottom:
            painter.setPen(major_pen if y % GRID_MAJOR_DBU == 0 else minor_pen)
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            y += GRID_MINOR_DBU
        painter.restore()

    def _draw_origin(self, painter: QPainter) -> None:
        size = GRID_MAJOR_DBU * 3
        pen  = QPen(QColor(Colors.GRID_ORIGIN))
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(-size, 0), QPointF(size, 0))
        painter.drawLine(QPointF(0, -size), QPointF(0, size))

    # ── Model sync ────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        """
        Fired by Qt on every selection change.
        - GroupItem(s) selected  → group_selected signal already handled by GroupItem.
        - 0 ComponentItems       → clear panel via item_selected("").
        - 1 ComponentItem        → show single component.
        - 2+ ComponentItems      → show multi-select panel.
        """
        sel         = self.selectedItems()
        comp_items  = [i for i in sel if isinstance(i, ComponentItem)]
        group_items = [i for i in sel if isinstance(i, GroupItem)]

        if group_items:
            return

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

        # Collect IDs of components that are locked inside a non-editing group.
        # Their geometry is managed entirely by the group drag path — calling
        # sync_from_model() on them here would produce a redundant repaint from
        # a potentially stale model snapshot mid-drag, causing visible flicker.
        locked_member_ids: set[str] = set()
        for gi in self._group_items.values():
            if not gi.is_editing:
                locked_member_ids.update(gi.group.member_ids)

        for comp in self._design.components:
            if comp.id not in locked_member_ids:
                self._items[comp.id].sync_from_model()

        # Group sync.
        model_gids = {g.id for g in self._design.groups}
        scene_gids = set(self._group_items.keys())

        for group in self._design.groups:
            if group.id not in self._group_items:
                gi = GroupItem(group, self)
                self.addItem(gi)
                self._group_items[group.id] = gi
                # Lock members immediately — must not be individually selectable
                # until the group enters edit mode.
                self._set_group_members_movable(group.id, False)

        for dead_id in scene_gids - model_gids:
            dead_item = self._group_items.pop(dead_id)
            dead_item.setSelected(False)
            self.removeItem(dead_item)
            # Re-enable former members so they're individually selectable again.
            for cid in dead_item.group.member_ids:
                item = self._items.get(cid)
                if item:
                    item.setSelected(False)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

        for gi in self._group_items.values():
            gi.invalidate_bbox()
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
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,    movable)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, movable)

    def exit_group_edit(self) -> None:
        if self._editing_group_id:
            gi = self._group_items.get(self._editing_group_id)
            if gi:
                gi.exit_edit_mode()
            self._editing_group_id = None
            self.group_edit_exited.emit()

    # ── Legacy ────────────────────────────────────────────────────────────────

    def _selected_group_count(self) -> int:
        """Return the number of non-editing GroupItems currently selected."""
        return sum(1 for i in self.selectedItems()
                   if isinstance(i, GroupItem) and not i.is_editing)