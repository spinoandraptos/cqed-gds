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
    Point, dbu_to_um, um_to_dbu,
)
from core.commands import CommandStack, AddComponent, MoveComponent
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
        self._drag_start: Optional[QPointF] = None
        self._orig_pos:   Optional[Point]   = None

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
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None
                and self._orig_pos is not None):

            # self.pos() is the accumulated scene-offset Qt applied during drag.
            # The true new origin is the original origin displaced by that offset.
            offset  = self.pos()
            new_origin = Point(
                self._orig_pos.x + int(round(offset.x())),
                self._orig_pos.y + int(round(offset.y())),
            )
            snapped = self._scene_ref.snap(new_origin)

            # Reset Qt's item position BEFORE mutating the model so
            # sync_from_model() draws from the correct scene-space coordinates.
            self.setPos(0, 0)

            if snapped != self._orig_pos:
                # MoveComponent.execute() calls comp.move_by() — don't do it here.
                cmd = MoveComponent(self._comp.id, self._orig_pos, snapped)
                self._scene_ref.cmd_stack.execute(cmd)
            else:
                # No net movement — just redraw in place.
                self.sync_from_model()

            self._drag_start = None
            self._orig_pos   = None
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

    item_selected = pyqtSignal(str)
    item_hovered  = pyqtSignal(str)
    cursor_moved  = pyqtSignal(float, float)
    scene_changed = pyqtSignal()
    mode_changed  = pyqtSignal(str)   # emits PlacementMode.status_label

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

    def drop_shape(self, kind_val: int, layer: int, scene_pos) -> None:
        """
        Called by CanvasView.dropEvent when a shape is dragged from the palette.
        Stamps the shape centred on the drop position and snaps to grid.
        Polygons and paths fall back to entering placement mode so the user
        can click vertices (a full drag-out-polygon UX is out of scope here).
        """
        from core.model import ComponentKind, GDSComponent, Point
        from core.commands import AddComponent

        kind = ComponentKind(kind_val)
        snapped = self.snap_f(scene_pos.x(), scene_pos.y())
        cx, cy  = int(snapped.x()), int(snapped.y())

        if kind == ComponentKind.RECTANGLE:
            hw, hh = DEFAULT_W_DBU // 2, DEFAULT_H_DBU // 2
            comp = GDSComponent(
                kind=ComponentKind.RECTANGLE, layer=layer,
                origin=Point(cx - hw, cy - hh),
                width=DEFAULT_W_DBU, height=DEFAULT_H_DBU,
            )
            self.cmd_stack.execute(AddComponent(comp))
        else:
            # For polygon/path, enter placement mode at the drop point so the
            # user immediately starts laying vertices from there.
            self.set_mode(
                PlacementMode.PLACE_POLYGON if kind == ComponentKind.POLYGON
                else PlacementMode.PLACE_PATH,
                layer,
            )
            self._add_vertex(snapped)

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

    def snap(self, pt: Point) -> Point:
        g = GRID_MINOR_DBU
        return Point(round(pt.x / g) * g, round(pt.y / g) * g)

    def snap_f(self, x: float, y: float) -> QPointF:
        g = float(GRID_MINOR_DBU)
        return QPointF(round(x / g) * g, round(y / g) * g)

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

        self.scene_changed.emit()
        self.update()