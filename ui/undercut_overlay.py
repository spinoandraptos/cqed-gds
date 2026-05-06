"""
ui/undercut_overlay.py — Contour-following undercut ring overlay.

Public surface
--------------
UndercutOverlay(scene_ref)
    Attach one instance to a CanvasScene.  It listens to
    scene.selectionChanged and scene.scene_changed, then adds/removes
    UndercutRingItem children on the fly.

    Public methods
    ~~~~~~~~~~~~~~
    enable(on: bool)          — show / hide all rings globally.
    toggle()                  — flip the enabled flag.
    is_enabled → bool

    set_offset_um(um: float)  — change the expansion distance (default 0.8 µm).
    offset_um → float

Usage (one line in CanvasScene.__init__)
-----------------------------------------
    from ui.undercut_overlay import UndercutOverlay
    self._undercut = UndercutOverlay(self)

And in MainWindow._build_view_menu (or toolbar), wire a toggle:
    self._act_undercut = self._action(
        "Show Undercut Ring", "U", self._scene._undercut.toggle
    )
    self._act_undercut.setCheckable(True)
    self._act_undercut.setChecked(False)

Design notes
------------
• Zero modifications to existing files.
• UndercutRingItem is a non-interactive QGraphicsPathItem child of a
  ComponentItem (or of the scene root for groups — see _GroupRingItem).
• The ring is built with QPainterPath arithmetic:
      ring = stroked_outline − original_shape
  where stroked_outline = QPainterPath.strokedPath(pen width = 2×offset).
  For PATH components the inner filled silhouette is built first (stroke
  the centreline with path_width), then that silhouette is expanded by
  the offset.  This correctly grows from the physical outer edge of the
  wire, not from its centreline.
• For groups the member shapes are united in scene coordinates before
  expansion, so overlapping members produce a single smooth outer contour
  rather than multiple stacked rings.
• All items carry Z-value 5 — above fills (Z≈0) but below ports (Z=8)
  and connection indicators (Z=9).
• Rings are purely visual: NoButton, not Selectable, not Movable.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsPathItem, QGraphicsScene,
)

from core.model import ComponentKind, dbu_to_um, um_to_dbu

# ── Visual constants ──────────────────────────────────────────────────────────

_RING_COLOR      = QColor("#fb923c")   # orange-400 — distinct from any layer colour
_RING_ALPHA      = 110                 # 0-255; semi-transparent so underlying shape shows
_RING_BORDER     = QColor("#fdba74")   # orange-300 for the outer stroke
_RING_BORDER_W   = 0.8                 # cosmetic pen width (screen px)
_RING_Z          = 5                   # between fill (0) and ports (8)

_DEFAULT_OFFSET_UM = 0.8              # µm — physical undercut expansion

# ── Helpers ───────────────────────────────────────────────────────────────────

def _expansion_pen(offset_dbu: int) -> QPen:
    """Cosmetic-OFF pen used solely to drive strokedPath().

    strokedPath() interprets pen width in *item local* (scene) coordinates,
    so we must NOT make this pen cosmetic.  Width = 2×offset because
    strokedPath grows by half-width on each side.
    """
    pen = QPen(Qt.GlobalColor.black, 2.0 * offset_dbu)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    return pen


def _filled_silhouette_for_path(comp) -> QPainterPath:
    """
    Convert a PATH component to its filled silhouette (the outline of the
    stroke as a closed polygon), which is then treated like a filled shape
    for offset purposes.

    A PATH centreline with path_width W is expanded by strokedPath(W) to
    get the physical filled area of the wire.  The undercut then grows from
    the *outside* of that area.
    """
    pts = comp.points or []
    if not pts:
        return QPainterPath()

    centreline = QPainterPath(QPointF(pts[0].x, pts[0].y))
    for pt in pts[1:]:
        centreline.lineTo(pt.x, pt.y)

    pw = comp.path_width or um_to_dbu(0.5)
    fill_pen = QPen(Qt.GlobalColor.black, float(pw))
    fill_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    fill_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return centreline.strokedPath(fill_pen)


def _shape_path_for_comp(comp) -> QPainterPath:
    """
    Return the closed filled QPainterPath for a single component,
    in its own local (scene) coordinate frame.

    RECTANGLE → QRectF path
    POLYGON   → closed polygon path
    PATH      → filled silhouette of the wire (two-step expansion)
    """
    if comp.kind == ComponentKind.RECTANGLE:
        path = QPainterPath()
        path.addRect(QRectF(comp.origin.x, comp.origin.y,
                            comp.width, comp.height))
        return path

    if comp.kind == ComponentKind.POLYGON:
        pts = comp.points or []
        if not pts:
            return QPainterPath()
        path = QPainterPath(QPointF(pts[0].x, pts[0].y))
        for pt in pts[1:]:
            path.lineTo(pt.x, pt.y)
        path.closeSubpath()
        return path

    # PATH
    return _filled_silhouette_for_path(comp)


def _build_ring_path(base_path: QPainterPath, offset_dbu: int) -> QPainterPath:
    """
    Compute the annular ring path:  expanded_outline  −  original_shape.

    base_path  — filled shape in scene coordinates.
    offset_dbu — expansion in DBU units (= nm).

    Returns a QPainterPath representing only the ring annulus, ready to be
    set on a QGraphicsPathItem.  An empty path is returned on degenerate input.
    """
    if base_path.isEmpty():
        return QPainterPath()

    # Expand by offset_dbu on all sides.
    expanded = base_path.strokedPath(_expansion_pen(offset_dbu))
    # Unite the expanded shell with the original to fill any interior gaps that
    # strokedPath might introduce on non-convex shapes.
    outer = expanded.united(base_path)
    # The ring annulus is the difference: outer_hull minus the original interior.
    ring = outer.subtracted(base_path)
    return ring


# ── Ring item: single component ───────────────────────────────────────────────

class _ComponentRingItem(QGraphicsPathItem):
    """
    Non-interactive undercut ring drawn as a child of a ComponentItem.
    Inherits the parent's transform (position, rotation) automatically.
    """

    def __init__(self, parent: QGraphicsItem, offset_dbu: int) -> None:
        super().__init__(parent)
        self.setZValue(_RING_Z)
        self._configure_interaction()
        self._apply_style()
        # The path is set by the owner via rebuild().

    def _configure_interaction(self) -> None:
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _apply_style(self) -> None:
        fill = QColor(_RING_COLOR)
        fill.setAlpha(_RING_ALPHA)
        self.setBrush(QBrush(fill))
        border = QPen(_RING_BORDER, _RING_BORDER_W)
        border.setCosmetic(True)
        self.setPen(border)

    def rebuild(self, comp, offset_dbu: int) -> None:
        base   = _shape_path_for_comp(comp)
        ring   = _build_ring_path(base, offset_dbu)
        self.setPath(ring)


# ── Ring item: component group ────────────────────────────────────────────────

class _GroupRingItem(QGraphicsPathItem):
    """
    Non-interactive undercut ring for a ComponentGroup.

    Groups have no single parent QGraphicsItem that follows their geometry,
    so this item lives directly in the scene (parentItem = None) and is
    repositioned by UndercutOverlay.rebuild_for_group() after every drag.

    The union of all member shapes is computed in scene coordinates, then the
    ring is built from that union — overlapping members produce one smooth
    outer contour.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setZValue(_RING_Z)
        self._configure_interaction()
        self._apply_style()

    def _configure_interaction(self) -> None:
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _apply_style(self) -> None:
        fill = QColor(_RING_COLOR)
        fill.setAlpha(_RING_ALPHA)
        self.setBrush(QBrush(fill))
        border = QPen(_RING_BORDER, _RING_BORDER_W)
        border.setCosmetic(True)
        self.setPen(border)

    def rebuild(self, member_comps: list, offset_dbu: int) -> None:
        """
        Rebuild the ring from the union of all member component shapes
        (all expressed in scene / world coordinates).
        """
        union = QPainterPath()
        for comp in member_comps:
            shape = _shape_path_for_comp(comp)
            if not shape.isEmpty():
                union = union.united(shape)

        ring = _build_ring_path(union, offset_dbu)
        self.setPath(ring)


# ── Overlay controller ────────────────────────────────────────────────────────

class UndercutOverlay:
    """
    Manages all undercut ring items for a CanvasScene.

    Lifecycle
    ---------
    1. Constructed once in CanvasScene.__init__; receives scene_ref.
    2. Connects to selectionChanged and scene_changed.
    3. On each relevant signal, rebuilds only the rings affected by the
       current selection, minimising recomputation cost.
    4. enable(False) hides all rings without destroying them (fast toggle).

    Threading
    ---------
    All operations run on the Qt main thread via signal delivery — no
    additional locking needed.
    """

    def __init__(self, scene_ref: "QGraphicsScene") -> None:  # noqa: F821
        self._scene       = scene_ref
        self._enabled     = False
        self._offset_dbu  = um_to_dbu(_DEFAULT_OFFSET_UM)

        # comp_id → _ComponentRingItem
        self._comp_rings:  Dict[str, _ComponentRingItem]  = {}
        # group_id → _GroupRingItem
        self._group_rings: Dict[str, _GroupRingItem]      = {}

        # Connect to scene signals — both live on the scene object.
        scene_ref.selectionChanged.connect(self._on_selection_changed)
        scene_ref.scene_changed.connect(self._on_scene_changed)

    # ── Public API ────────────────────────────────────────────────────────────

    def enable(self, on: bool) -> None:
        """Show or hide all rings.  Rings are rebuilt lazily when re-enabled."""
        if self._enabled == on:
            return
        self._enabled = on
        if on:
            self._rebuild_all_selected()
        else:
            self._remove_all()

    def toggle(self) -> None:
        self.enable(not self._enabled)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_offset_um(self, um: float) -> None:
        """Change the undercut expansion distance and redraw live rings."""
        self._offset_dbu = um_to_dbu(max(0.0, um))
        if self._enabled:
            self._rebuild_all_selected()

    @property
    def offset_um(self) -> float:
        return dbu_to_um(self._offset_dbu)

    # ── Internal rebuild helpers ──────────────────────────────────────────────

    def _rebuild_all_selected(self) -> None:
        """Rebuild rings for every currently selected item."""
        # First remove stale rings for items no longer selected.
        self._remove_all()

        sel = self._scene.selectedItems()
        seen_group_ids: Set[str] = set()

        for item in sel:
            comp = getattr(item, "component", None)
            if comp is not None:
                # Check whether this component belongs to a group that is also
                # represented by a GroupItem in the selection — if so, the
                # group ring covers it; skip the per-component ring to avoid
                # double-drawing.
                if self._comp_is_covered_by_group_ring(comp, sel):
                    continue
                self._ensure_comp_ring(comp, item)
                continue

            group = getattr(item, "group", None)
            if group is not None and group.id not in seen_group_ids:
                seen_group_ids.add(group.id)
                self._ensure_group_ring(group)

    def _comp_is_covered_by_group_ring(self, comp, sel_items) -> bool:
        """
        Return True if a GroupItem in sel_items owns this component, meaning
        the group-level ring will cover it and a per-component ring is redundant.
        """
        from ui.canvas_scene import GroupItem  # local import to avoid circular
        for item in sel_items:
            if isinstance(item, GroupItem):
                if comp.id in item.group.member_ids:
                    return True
        return False

    def _ensure_comp_ring(self, comp, parent_item: QGraphicsItem) -> None:
        """Create or update the ring for a single ComponentItem."""
        ring = self._comp_rings.get(comp.id)
        if ring is None:
            ring = _ComponentRingItem(parent_item, self._offset_dbu)
            self._comp_rings[comp.id] = ring
        ring.rebuild(comp, self._offset_dbu)
        ring.setVisible(self._enabled)

    def _ensure_group_ring(self, group) -> None:
        """Create or update the ring for a group (union of all members)."""
        member_comps = self._resolve_group_members(group)
        if not member_comps:
            return

        ring = self._group_rings.get(group.id)
        if ring is None:
            ring = _GroupRingItem()
            self._scene.addItem(ring)
            self._group_rings[group.id] = ring
        ring.rebuild(member_comps, self._offset_dbu)
        ring.setVisible(self._enabled)

    def _resolve_group_members(self, group) -> list:
        """Fetch live GDSComponent objects for every member of *group*."""
        design = self._scene._design
        members = []
        for cid in group.member_ids:
            comp = design.get(cid)
            if comp is not None:
                members.append(comp)
        return members

    def _remove_all(self) -> None:
        """Detach and discard every ring item currently in the scene."""
        for ring in self._comp_rings.values():
            ring.setParentItem(None)
            if ring.scene():
                ring.scene().removeItem(ring)
        self._comp_rings.clear()

        for ring in self._group_rings.values():
            if ring.scene():
                ring.scene().removeItem(ring)
        self._group_rings.clear()

    def _remove_stale_comp_rings(self, live_ids: Set[str]) -> None:
        """Remove rings for components no longer in *live_ids*."""
        stale = [cid for cid in self._comp_rings if cid not in live_ids]
        for cid in stale:
            ring = self._comp_rings.pop(cid)
            ring.setParentItem(None)
            if ring.scene():
                ring.scene().removeItem(ring)

    def _remove_stale_group_rings(self, live_ids: Set[str]) -> None:
        """Remove rings for groups no longer in *live_ids*."""
        stale = [gid for gid in self._group_rings if gid not in live_ids]
        for gid in stale:
            ring = self._group_rings.pop(gid)
            if ring.scene():
                ring.scene().removeItem(ring)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        if not self._enabled:
            return
        self._rebuild_all_selected()

    def _on_scene_changed(self) -> None:
        """
        Called after every model mutation (add/remove/move/resize).
        Rebuilds rings for items that are still selected so geometry stays
        in sync after moves, rotations, and parameter edits.
        """
        if not self._enabled:
            return
        # Determine which rings are still alive; rebuild them in place.
        sel = self._scene.selectedItems()
        live_comp_ids:  Set[str] = set()
        live_group_ids: Set[str] = set()

        for item in sel:
            comp = getattr(item, "component", None)
            if comp is not None and comp.id in self._comp_rings:
                live_comp_ids.add(comp.id)
                ring = self._comp_rings[comp.id]
                ring.rebuild(comp, self._offset_dbu)

            group = getattr(item, "group", None)
            if group is not None and group.id in self._group_rings:
                live_group_ids.add(group.id)
                members = self._resolve_group_members(group)
                if members:
                    self._group_rings[group.id].rebuild(members, self._offset_dbu)

        self._remove_stale_comp_rings(live_comp_ids)
        self._remove_stale_group_rings(live_group_ids)