"""
ui/undercut_overlay.py — Contour-following undercut ring overlay.

Public surface
--------------
UndercutOverlay(scene_ref)
    Attach one instance to a CanvasScene.  It listens to
    scene.scene_changed, then adds/removes UndercutRingItem children on
    the fly.

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
• Rings are persistent — they are shown for ALL components on the canvas
  while the overlay is enabled, regardless of selection state.
  Deselecting a component does NOT remove its ring.
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
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QPainterPathStroker
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

# Only components on this app-layer contribute geometry to undercut rings.
# Must stay in sync with UNDERCUT_SOURCE_LAYER in core/exporter.py.
_UNDERCUT_SOURCE_LAYER = 1

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
    pts = comp.points or []
    if not pts:
        return QPainterPath()

    centreline = QPainterPath(QPointF(pts[0].x, pts[0].y))
    for pt in pts[1:]:
        centreline.lineTo(pt.x, pt.y)

    pw = comp.path_width or um_to_dbu(0.5)
    
    # Use stroker for the centerline expansion
    stroker = QPainterPathStroker()
    stroker.setWidth(float(pw))
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    
    return stroker.createStroke(centreline)


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
    if base_path.isEmpty():
        return QPainterPath()

    # Create the stroker utility
    stroker = QPainterPathStroker()
    stroker.setWidth(2.0 * offset_dbu)
    stroker.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    stroker.setCapStyle(Qt.PenCapStyle.FlatCap)

    # Use the stroker to create the expanded outline
    expanded = stroker.createStroke(base_path)
    
    # Rest of your logic remains the same
    outer = expanded.united(base_path)
    ring = outer.subtracted(base_path)
    return ring


def _taper_quad_ring(l1_comp, offset_dbu: int, l11_comp=None) -> "QPainterPath | None":
    """
    Build the undercut ring for a tapered-lead L1 body polygon.

    The ring uses the standard stroker expansion (offset_dbu) on all sides
    EXCEPT the narrow-end face, where instead of expanding outward by offset_dbu
    the outer edge is placed at the midpoint of the L11 clip polygon —
    i.e. it extends inward by  clip_length / 2  from the clip boundary face.

    This means the undercut ring ends exactly halfway through the L11 clip,
    matching the physical undercut at the narrow tip.

    l11_comp : the L11 sibling GDSComponent (optional). When supplied its
               geometry is used to compute clip_length and the direction toward
               the narrow tip.  When None, falls back to uniform expansion.

    Returns a QPainterPath or None (caller falls back to _build_ring_path).
    """
    import math

    if l11_comp is None:
        return None

    # ── Identify the shared clip-boundary face ────────────────────────────────
    # The L1 body and L11 clip share an edge at the clip boundary.
    # L11 vertices: two at the clip boundary, two at the narrow tip.
    # L1  vertices: two at the clip boundary, two at the wide end.
    #
    # We find the clip-boundary vertices of L11 (the pair that are shared with L1),
    # then work out the inward direction (toward the narrow tip) and clip_length.

    l11_pts = l11_comp.points or []
    unique11 = list(l11_pts)
    if len(unique11) > 1 and unique11[-1] == unique11[0]:
        unique11 = unique11[:-1]
    if len(unique11) != 4:
        return None

    def _dist(a, b):
        return math.hypot(b.x - a.x, b.y - a.y)

    # Edges of L11 sorted by length: 2 short (end-faces) + 2 long (flanks)
    edges11 = [(i, (i+1)%4, _dist(unique11[i], unique11[(i+1)%4])) for i in range(4)]
    by_len11 = sorted(edges11, key=lambda e: e[2])
    face11   = by_len11[:2]   # two end-faces

    # The wider face of L11 = clip boundary (shared with L1)
    # The narrower face of L11 = narrow tip
    clip_face  = max(face11, key=lambda e: e[2])   # wider = clip boundary
    narrow_face = min(face11, key=lambda e: e[2])  # narrower = tip

    ci, cj = clip_face[0], clip_face[1]            # clip-boundary vertex indices in L11
    clip_pt_a = unique11[ci]
    clip_pt_b = unique11[cj]

    ni, nj = narrow_face[0], narrow_face[1]        # narrow-tip vertex indices in L11
    tip_pt_a = unique11[ni]
    tip_pt_b = unique11[nj]

    # Unit vector from clip boundary midpoint toward narrow tip midpoint
    clip_mid_x = (clip_pt_a.x + clip_pt_b.x) / 2.0
    clip_mid_y = (clip_pt_a.y + clip_pt_b.y) / 2.0
    tip_mid_x  = (tip_pt_a.x  + tip_pt_b.x)  / 2.0
    tip_mid_y  = (tip_pt_a.y  + tip_pt_b.y)  / 2.0

    dx = tip_mid_x - clip_mid_x
    dy = tip_mid_y - clip_mid_y
    clip_length = math.hypot(dx, dy)
    if clip_length == 0:
        return None

    # Unit vector pointing from clip boundary → narrow tip (inward direction)
    ux, uy = dx / clip_length, dy / clip_length

    # How far inward the ring extends at the narrow end = half the clip length
    inward_offset = clip_length / 2.0

    # ── Build the ring as a QPainterPath using the standard stroker for all
    #    sides except the narrow-end face, which gets a custom inward cap. ──────
    #
    # Strategy: build the ring shape as an explicit 6-vertex polygon per flank,
    # then add a cap across the narrow face.
    #
    # The standard ring = outer_shape - inner_shape where outer is L1 expanded
    # by offset_dbu on all sides.  We instead build a custom outer shape that
    # matches the standard expansion everywhere EXCEPT the narrow face, where
    # we substitute the inward-offset cap.
    #
    # Simpler equivalent: use the standard stroker-based ring, then SUBTRACT
    # the region beyond the inward cap (i.e. clip everything on the narrow-tip
    # side of the cap plane to zero).
    #
    # The cap plane passes through:
    #   clip_pt_a + ux*inward_offset, clip_pt_b + ux*inward_offset
    # and is perpendicular to the travel axis.
    #
    # We define a large clipping rectangle on the narrow-tip side of the cap
    # and subtract it from the standard ring.

    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath, QPainterPathStroker
    from PyQt6.QtCore import Qt

    # ── Standard ring from L1 body ────────────────────────────────────────────
    l1_pts = l1_comp.points or []
    unique1 = list(l1_pts)
    if len(unique1) > 1 and unique1[-1] == unique1[0]:
        unique1 = unique1[:-1]
    if len(unique1) != 4:
        return None

    base = QPainterPath(QPointF(unique1[0].x, unique1[0].y))
    for p in unique1[1:]:
        base.lineTo(p.x, p.y)
    base.closeSubpath()

    stroker = QPainterPathStroker()
    stroker.setWidth(2.0 * offset_dbu)
    stroker.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
    expanded = stroker.createStroke(base)
    outer    = expanded.united(base)
    ring     = outer.subtracted(base)

    # ── Clip rectangle: covers the region on the narrow-tip side of the cap ───
    # The cap line runs through two points offset inward from the clip boundary.
    cap_ax = clip_pt_a.x + ux * inward_offset
    cap_ay = clip_pt_a.y + uy * inward_offset
    cap_bx = clip_pt_b.x + ux * inward_offset
    cap_by = clip_pt_b.y + uy * inward_offset

    # Build a large rectangle on the narrow-tip side:
    # extend very far in the ux,uy direction and wide enough to cover the ring.
    LARGE = float(offset_dbu * 20)
    # Perpendicular to travel: uy, -ux (or -uy, ux)
    px_, py_ = -uy, ux   # one perpendicular direction

    # Four corners: cap line ± LARGE*perp, then push LARGE in inward direction
    c0x, c0y = cap_ax + px_*LARGE,         cap_ay + py_*LARGE
    c1x, c1y = cap_ax - px_*LARGE,         cap_ay - py_*LARGE
    c2x, c2y = c1x    + ux*LARGE,          c1y    + uy*LARGE
    c3x, c3y = c0x    + ux*LARGE,          c0y    + uy*LARGE

    clip_rect = QPainterPath(QPointF(c0x, c0y))
    clip_rect.lineTo(c1x, c1y)
    clip_rect.lineTo(c2x, c2y)
    clip_rect.lineTo(c3x, c3y)
    clip_rect.closeSubpath()

    result = ring.subtracted(clip_rect)
    return result if not result.isEmpty() else None


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
    2. Connects to scene_changed only (selection is irrelevant).
    3. On each scene_changed, syncs rings for ALL components and groups in
       the design — adding new ones, updating moved ones, removing deleted ones.
    4. enable(False) hides all rings without destroying them (fast toggle).

    Persistence
    -----------
    Rings are shown for every component on the canvas while the overlay is
    enabled.  Selecting or deselecting objects has NO effect on ring visibility.
    Only adding/removing components from the scene changes which rings exist.

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

        # IDs (comp or group) for which the ring is individually suppressed.
        # When the global overlay is ON, items in this set stay hidden.
        self._excluded_ids: Set[str] = set()

        # All comp/group IDs ever registered.  Used to distinguish "brand new,
        # never seen → default to excluded" from "seen before, user may have
        # explicitly included → do not touch _excluded_ids".
        self._known_ids: Set[str] = set()

        # obj_id → list of QPainterPath masks.  Each mask is subtracted from
        # the ring geometry before display, allowing arbitrary "eraser" regions
        # to be cut out of a ring without touching the model.
        self._masks: Dict[str, list] = {}

        # IDs of L2 narrow-undercut flank components that have been absorbed
        # into their group's unified ring path.  While absorbed their
        # ComponentItem is hidden so only the merged orange ring is visible.
        # Cleared / restored whenever the overlay is disabled or a group is removed.
        self._absorbed_ids: Set[str] = set()

        # scene_changed covers all model mutations (add/remove/move/resize).
        # selectionChanged is intentionally NOT connected — rings are persistent.
        scene_ref.scene_changed.connect(self._on_scene_changed)

    # ── Public API ────────────────────────────────────────────────────────────

    def enable(self, on: bool) -> None:
        """Show or hide all rings.  Rings are rebuilt lazily when re-enabled."""
        if self._enabled == on:
            return
        self._enabled = on
        if on:
            self._rebuild_all()
        else:
            self._remove_all()

    def toggle(self) -> None:
        self.enable(not self._enabled)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_offset_um(self, um: float) -> None:
        """Change the undercut expansion distance and redraw all live rings."""
        self._offset_dbu = um_to_dbu(max(0.0, um))
        if self._enabled:
            self._rebuild_all()

    @property
    def offset_um(self) -> float:
        return dbu_to_um(self._offset_dbu)

    # ── Mask API ──────────────────────────────────────────────────────────────

    def add_mask(self, obj_id: str, mask_path: "QPainterPath") -> None:
        """
        Subtract *mask_path* (in scene / world DBU coordinates) from the ring
        for *obj_id* (a component id or group id).  Stacks with any existing
        masks — each call appends one more erasure region.

        The ring is immediately redrawn if the overlay is currently enabled.
        """
        self._masks.setdefault(obj_id, []).append(mask_path)
        if self._enabled:
            self._rebuild_one(obj_id)

    def clear_masks(self, obj_id: str) -> None:
        """Remove all mask regions for *obj_id* and redraw the ring."""
        if obj_id in self._masks:
            del self._masks[obj_id]
        if self._enabled:
            self._rebuild_one(obj_id)

    def has_masks(self, obj_id: str) -> bool:
        """Return True if *obj_id* has at least one mask applied."""
        return bool(self._masks.get(obj_id))

    def _apply_masks(self, obj_id: str, ring_path: "QPainterPath") -> "QPainterPath":
        """Subtract all stored mask paths from *ring_path* and return the result."""
        masks = self._masks.get(obj_id)
        if not masks:
            return ring_path
        result = ring_path
        for mask in masks:
            result = result.subtracted(mask)
        return result

    def _rebuild_one(self, obj_id: str) -> None:
        """Rebuild the ring for a single comp or group id (used after mask changes)."""
        design = self._scene._design
        # Try as a group first, then as a component.
        group = design.get_group(obj_id)
        if group is not None:
            self._ensure_group_ring(group)
            return
        comp = design.get(obj_id)
        if comp is not None:
            parent_item = self._scene._items.get(obj_id)
            if parent_item is not None:
                self._ensure_comp_ring(comp, parent_item)

    # ── Per-object exclusion API ──────────────────────────────────────────────

    def set_excluded(self, obj_id: str, excluded: bool) -> None:
        """
        Show or hide the ring for a single component or group ID, independently
        of the global enable flag.  The global toggle must still be ON for any
        ring to be visible; this method only provides a per-object override.
        """
        if excluded:
            self._excluded_ids.add(obj_id)
        else:
            self._excluded_ids.discard(obj_id)

        # Apply immediately to any live ring items.
        if ring := self._comp_rings.get(obj_id):
            ring.setVisible(self._enabled and not excluded)
        if ring := self._group_rings.get(obj_id):
            ring.setVisible(self._enabled and not excluded)

    def is_excluded(self, obj_id: str) -> bool:
        """Return True if the ring for this object is individually suppressed."""
        return obj_id in self._excluded_ids

    # ── Internal rebuild helpers ──────────────────────────────────────────────

    def _rebuild_all(self) -> None:
        """
        Sync rings with the current design state:
        - Create/update a ring for every component not inside a group.
        - Create/update a group ring for every group (union of members).
        - Remove rings for components/groups no longer in the design.

        Components that belong to a group are covered by the group ring and
        do not get individual rings, avoiding double-drawing.
        """
        design = self._scene._design

        # Collect all group member IDs so we can skip them for per-comp rings.
        grouped_ids: Set[str] = set()
        for group in design.groups:
            grouped_ids.update(group.member_ids)

        # Pre-register any brand-new component or group IDs as excluded BEFORE
        # creating ring items, so is_excluded() always returns the correct
        # "off by default" state.  _known_ids tracks every id ever seen, so we
        # only default-exclude ids that are genuinely new — never re-excluding
        # an id the user has explicitly enabled.
        for comp in design.components:
            if comp.id not in grouped_ids and comp.id not in self._known_ids:
                self._known_ids.add(comp.id)
                self._excluded_ids.add(comp.id)
        for group in design.groups:
            if group.id not in self._known_ids:
                self._known_ids.add(group.id)
                self._excluded_ids.add(group.id)

        # ── Per-component rings (ungrouped components only) ───────────────────
        live_comp_ids: Set[str] = set()
        for comp in design.components:
            if comp.id in grouped_ids:
                continue  # covered by the group ring below
            live_comp_ids.add(comp.id)
            parent_item = self._scene._items.get(comp.id)
            if parent_item is None:
                continue
            self._ensure_comp_ring(comp, parent_item)

        # ── Group rings ───────────────────────────────────────────────────────
        live_group_ids: Set[str] = set()
        for group in design.groups:
            live_group_ids.add(group.id)
            self._ensure_group_ring(group)

        # ── Prune stale rings ─────────────────────────────────────────────────
        self._remove_stale_comp_rings(live_comp_ids)
        self._remove_stale_group_rings(live_group_ids)

    def _ensure_comp_ring(self, comp, parent_item: QGraphicsItem) -> None:
        """Create or update the ring for a single ComponentItem."""
        # Only Layer-1 components get an undercut ring.
        if comp.layer != _UNDERCUT_SOURCE_LAYER:
            # Remove any stale ring that may exist from before a layer change.
            if comp.id in self._comp_rings:
                ring = self._comp_rings.pop(comp.id)
                ring.setParentItem(None)
                if ring.scene():
                    ring.scene().removeItem(ring)
            return

        ring = self._comp_rings.get(comp.id)
        if ring is None:
            ring = _ComponentRingItem(parent_item, self._offset_dbu)
            self._comp_rings[comp.id] = ring
        elif ring.parentItem() is not parent_item:
            # Parent changed (e.g. after undo/redo recreated the item).
            ring.setParentItem(parent_item)
        base = _shape_path_for_comp(comp)
        # For tapered-lead polygons on L1 (trapezoids), use a variable-thickness
        # ring: narrow_width/2 at the narrow end, offset_dbu at the wide end.
        # We find the narrow width by locating the L11 sibling in the same group.
        # _taper_quad_ring returns None for non-trapezoid shapes (e.g. regular
        # polygons on L1), in which case we fall back to the standard uniform ring.
        if comp.kind == ComponentKind.POLYGON:
            l11 = self._l11_sibling_for(comp)
            raw = _taper_quad_ring(comp, self._offset_dbu, l11)
            if raw is None:
                raw = _build_ring_path(base, self._offset_dbu)
        else:
            raw = _build_ring_path(base, self._offset_dbu)
        masked = self._apply_masks(comp.id, raw)
        ring.setPath(masked)
        ring.setVisible(self._enabled and comp.id not in self._excluded_ids)

    def _ensure_group_ring(self, group) -> None:
        """Create or update the ring for a group (union of Layer-1 members only)."""
        member_comps = self._resolve_group_members(group)
        # Only Layer-1 members contribute geometry to the ring.
        layer1_comps = [c for c in member_comps if c.layer == _UNDERCUT_SOURCE_LAYER]
        if not layer1_comps:
            # No Layer-1 members — remove any stale ring and bail out.
            if group.id in self._group_rings:
                ring = self._group_rings.pop(group.id)
                if ring.scene():
                    ring.scene().removeItem(ring)
            return

        ring = self._group_rings.get(group.id)
        if ring is None:
            ring = _GroupRingItem()
            self._scene.addItem(ring)
            self._group_rings[group.id] = ring

        # Build the ring from Layer-1 members.
        # For taper-lead groups (one L1 polygon + one L11 clip sibling) use
        # _taper_quad_ring so the narrow-end cap is limited to clip_length/2
        # instead of the full offset expansion.  Fall back to the uniform union
        # ring for all other group shapes (multiple L1 members, rectangles, etc.).
        from core.cell_library import LAYER_NARROW_END, LAYER_UNDERCUT_RING
        all_members = self._resolve_group_members(group)
        l11_comps = [c for c in all_members if c.layer == LAYER_NARROW_END]
        l1_polys  = [c for c in layer1_comps if c.kind == ComponentKind.POLYGON]

        raw = None
        if len(l1_polys) == 1 and len(l11_comps) == 1:
            # Single taper-lead L1 body with its L11 narrow-tip clip — use the
            # variable-thickness ring that terminates at clip_length/2 inward.
            raw = _taper_quad_ring(l1_polys[0], self._offset_dbu, l11_comps[0])

        if raw is None:
            # Fallback: uniform expansion of the union of all L1 members.
            union = QPainterPath()
            for comp in layer1_comps:
                shape = _shape_path_for_comp(comp)
                if not shape.isEmpty():
                    union = union.united(shape)
            raw = _build_ring_path(union, self._offset_dbu)

        # ── Merge narrow-undercut L2 flank polygons into the ring ────────────
        # When build_taper_segment emits narrow_undercut=True it adds two L2
        # POLYGON components (top + bottom flank) as group members.  When the
        # overlay is on we absorb their shapes into the unified orange ring and
        # hide those ComponentItems — no L2 colour bleed, no double outline.
        # IDs are tracked in _absorbed_ids for clean restoration on disable.
        l2_flanks = [
            c for c in all_members
            if c.layer == LAYER_UNDERCUT_RING and c.kind == ComponentKind.POLYGON
        ]
        new_absorbed: Set[str] = set()
        for flank in l2_flanks:
            flank_path = _shape_path_for_comp(flank)
            if not flank_path.isEmpty():
                raw = raw.united(flank_path)
            new_absorbed.add(flank.id)
            item = self._scene._items.get(flank.id)
            if item is not None:
                item.setVisible(False)

        # Restore any previously absorbed flanks that are no longer in this
        # group (e.g. after undo removed narrow_undercut from the cell).
        group_member_ids = {c.id for c in all_members}
        stale_absorbed = {cid for cid in self._absorbed_ids
                          if cid not in group_member_ids}
        for cid in stale_absorbed:
            item = self._scene._items.get(cid)
            if item is not None:
                item.setVisible(True)
            self._absorbed_ids.discard(cid)

        self._absorbed_ids.update(new_absorbed)

        masked = self._apply_masks(group.id, raw)
        ring.setPath(masked)
        ring.setVisible(self._enabled and group.id not in self._excluded_ids)


    def _l11_sibling_for(self, comp):
        """
        Return the L11 GDSComponent that belongs to the same group as *comp*,
        or None if not found.  Used to locate the narrow-tip clip polygon so
        _taper_quad_ring can compute where to terminate the undercut ring.
        """
        from core.cell_library import LAYER_NARROW_END
        design = self._scene._design
        group = design.group_of(comp.id)
        if group is None:
            return None
        for cid in group.member_ids:
            if cid == comp.id:
                continue
            sibling = design.get(cid)
            if sibling is not None and sibling.layer == LAYER_NARROW_END:
                return sibling
        return None

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

        # Restore visibility of any L2 narrow-undercut flanks that were
        # absorbed into their group ring while the overlay was active.
        for cid in self._absorbed_ids:
            item = self._scene._items.get(cid)
            if item is not None:
                item.setVisible(True)
        self._absorbed_ids.clear()

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
            # Restore any absorbed L2 flank components that belonged to this
            # now-deleted group so they become visible again (e.g. after undo).
            design = self._scene._design
            still_alive = {c.id for c in design.components}
            released = {cid for cid in self._absorbed_ids if cid not in still_alive or
                        design.group_of(cid) is None}
            for cid in released:
                item = self._scene._items.get(cid)
                if item is not None:
                    item.setVisible(True)
                self._absorbed_ids.discard(cid)

    # ── Signal handler ────────────────────────────────────────────────────────

    def _on_scene_changed(self) -> None:
        """
        Called after every model mutation (add/remove/move/resize).
        Syncs all rings to the current design state so geometry stays in sync
        after moves, parameter edits, undo/redo, and component deletion.
        selectionChanged is intentionally NOT handled — rings are persistent.
        """
        # Always pre-register any new component/group ids as excluded so that
        # the Properties panel's is_excluded() call always returns the correct
        # "off by default" state even when the overlay is globally disabled.
        # _known_ids ensures we never re-exclude an id the user has enabled.
        design = self._scene._design
        grouped_ids: Set[str] = set()
        for group in design.groups:
            grouped_ids.update(group.member_ids)
        for comp in design.components:
            if comp.id not in grouped_ids and comp.id not in self._known_ids:
                self._known_ids.add(comp.id)
                self._excluded_ids.add(comp.id)
        for group in design.groups:
            if group.id not in self._known_ids:
                self._known_ids.add(group.id)
                self._excluded_ids.add(group.id)

        if not self._enabled:
            return
        self._rebuild_all()