"""
core/exporter.py — GDS export via gdstk.

Design rules:
  - Zero Qt dependency. Pure data-in / file-out.
  - DBU = nm throughout. gdstk works in µm, so every coordinate is divided
    by DBU_PER_UM (1000) before being handed to gdstk.
  - Layer mapping: the app uses a single integer "layer" field.
    GDS format has (layer, datatype) pairs. The caller supplies a
    LayerMap dict[int -> (int, int)] that translates app-layer → (gds_layer, datatype).
    Any app-layer not in the map falls back to (app_layer, 0).
  - One gdstk Cell named after DesignScene.name contains all shapes.
  - Rectangles  → gdstk.rectangle
  - Polygons    → gdstk.Polygon
  - Paths       → gdstk.FlexPath  (round caps/joins, width in µm)

Group merging (new):
  Components that belong to the same ComponentGroup AND share the same
  app-layer are boolean-unioned (OR) into a single merged polygon before
  being written to the GDS cell.  Ungrouped components are written
  individually, exactly as before.

Verification:
  After writing, re-read the file with gdstk and return a summary dict so
  the caller can show a confirmation dialog without re-importing the file.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

import gdstk

from core.model import (
    DesignScene, GDSComponent, ComponentGroup, ComponentKind,
    dbu_to_um, DBU_PER_UM,
)

# (gds_layer, datatype)
LayerMap = Dict[int, Tuple[int, int]]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _um(dbu: int) -> float:
    """Convert DBU (nm) → µm for gdstk."""
    return dbu / DBU_PER_UM


def _pts_um(points) -> List[Tuple[float, float]]:
    return [(_um(p.x), -_um(p.y)) for p in points]   # negate Y


def _resolve(layer: int, layer_map: LayerMap) -> Tuple[int, int]:
    return layer_map.get(layer, (layer, 0))


# ── Component → gdstk polygon list (without adding to a cell) ─────────────────

def _comp_to_gdstk_polys(
    comp: GDSComponent,
    gds_layer: int,
    datatype: int,
) -> List[gdstk.Polygon]:
    """
    Convert a GDSComponent to a list of gdstk.Polygon objects.

    FlexPath is converted via get_polygons() so that all geometry is
    in a uniform polygon representation suitable for boolean operations.
    Returns an empty list if the component has degenerate geometry.
    """
    if comp.kind == ComponentKind.RECTANGLE:
        x0 =  _um(comp.origin.x)
        y0 = -_um(comp.origin.y)
        x1 = x0 + _um(comp.width)
        y1 = y0 - _um(comp.height)
        rect = gdstk.rectangle(
            (x0, y0), (x1, y1),
            layer=gds_layer, datatype=datatype,
        )
        return [rect]

    elif comp.kind == ComponentKind.POLYGON:
        if not comp.points or len(comp.points) < 3:
            return []
        pts = _pts_um(comp.points)
        return [gdstk.Polygon(pts, layer=gds_layer, datatype=datatype)]

    elif comp.kind == ComponentKind.PATH:
        if not comp.points or len(comp.points) < 2:
            return []
        pts   = _pts_um(comp.points)
        width = _um(comp.path_width) if comp.path_width else 0.001
        fp    = gdstk.FlexPath(
            pts[0], width,
            layer=gds_layer, datatype=datatype,
        )
        for pt in pts[1:]:
            fp.segment(pt)
        # Flatten FlexPath → plain Polygon list for boolean ops
        polys = fp.get_polygons()
        for p in polys:
            p.layer    = gds_layer
            p.datatype = datatype
        return list(polys)

    return []


# ── Core export ───────────────────────────────────────────────────────────────

class ExportError(Exception):
    pass


def export_gds(
    design: DesignScene,
    path: str | Path,
    layer_map: LayerMap,
    overlay=None,
    precision: float = 1e-9,   # 1 nm
    unit: float = 1e-6,        # µm
) -> dict:
    """
    Write *design* to a GDS-II file at *path*.

    Components that belong to the same group and share the same app-layer
    are boolean-unioned into a single merged shape.  All other components
    are written individually.

    L2 (UNDERCUT_RING_LAYER) merge pass:
        After all geometry is written to the cell, every polygon on L2 is
        collected and boolean-ORed into the minimum set of non-overlapping
        polygons.  This ensures that narrow-end undercut shapes emitted by
        taper_segment cells (also on L2) merge seamlessly with any adjacent
        outer undercut rings, producing one smooth unified outline in the GDS
        regardless of how the individual components were placed.

    Returns a verification summary dict:
        {
            "path":       str,
            "cell":       str,
            "shapes":     int,
            "layers":     list[tuple[int,int]],
            "bbox_um":    (x_min, y_min, x_max, y_max) in µm,
        }

    Raises ExportError on any failure so the UI can show a clean message.
    """
    path = Path(path)

    if not design.components:
        raise ExportError("Nothing to export — the design is empty.")

    try:
        lib  = gdstk.Library(unit=unit, precision=precision)
        cell = lib.new_cell(design.name)

        # ── Partition components into grouped vs ungrouped ────────────────────
        grouped_ids: Set[str] = set()
        for group in design.groups:
            grouped_ids.update(group.member_ids)

        comp_map = {c.id: c for c in design.components}

        # ── Emit merged shapes for each (group × layer) bucket ───────────────
        for group in design.groups:
            _emit_merged_group(cell, group, comp_map, layer_map)

        # ── Emit ungrouped components individually (unchanged behaviour) ──────
        for comp in design.components:
            if comp.id not in grouped_ids:
                gds_layer, datatype = _resolve(comp.layer, layer_map)
                _add_component(cell, comp, gds_layer, datatype)

        # ── Emit undercut rings if overlay is active ──────────────────────────
        if overlay is not None:
            export_undercut_rings(cell, design, overlay, layer_map)

        # ── L2 merge pass: union all undercut-ring polygons ───────────────────
        # Narrow-end undercut shapes (from taper_segment cells with
        # narrow_undercut=True) and outer undercut rings both live on L2.
        # Boolean-OR the entire L2 layer so coincident or touching shapes
        # unify into one smooth outline before the file is written.
        _merge_undercut_layer(cell, layer_map)

        lib.write_gds(str(path))

    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"gdstk error: {exc}") from exc

    return _verify(path, design.name)


# ── Group merger ──────────────────────────────────────────────────────────────

def _emit_merged_group(
    cell,
    group: ComponentGroup,
    comp_map: Dict[str, GDSComponent],
    layer_map: LayerMap,
) -> None:
    """
    For each unique app-layer present among the group's members, collect all
    polygon geometry and boolean-union it into one (or more) merged polygon(s).
    The result is added directly to *cell*.
    """
    # Bucket member components by app-layer
    by_layer: Dict[int, List[GDSComponent]] = defaultdict(list)
    for cid in group.member_ids:
        comp = comp_map.get(cid)
        if comp is not None:
            by_layer[comp.layer].append(comp)

    for app_layer, comps in by_layer.items():
        gds_layer, datatype = _resolve(app_layer, layer_map)

        # Gather all polygon geometry for this (group, layer) bucket
        all_polys: List[gdstk.Polygon] = []
        for comp in comps:
            all_polys.extend(_comp_to_gdstk_polys(comp, gds_layer, datatype))

        if not all_polys:
            continue

        if len(all_polys) == 1:
            # Nothing to merge — emit as-is
            cell.add(all_polys[0])
        else:
            # Boolean OR → unified outline, no interior seams
            merged = gdstk.boolean(
                all_polys, [],
                operation="or",
                layer=gds_layer,
                datatype=datatype,
            )
            if merged:
                cell.add(*merged)
            else:
                # Fallback: union returned nothing (degenerate case), emit raw
                cell.add(*all_polys)


# ── Individual component emitter (ungrouped path, unchanged) ──────────────────

def _add_component(cell, comp: GDSComponent, gds_layer: int, datatype: int) -> None:
    if comp.kind == ComponentKind.RECTANGLE:
        x0 =  _um(comp.origin.x)
        y0 = -_um(comp.origin.y)           # negate Y
        x1 = x0 + _um(comp.width)
        y1 = y0 - _um(comp.height)         # negate Y
        cell.add(gdstk.rectangle(
            (x0, y0), (x1, y1),
            layer=gds_layer, datatype=datatype,
        ))

    elif comp.kind == ComponentKind.POLYGON:
        if not comp.points or len(comp.points) < 3:
            return
        pts = _pts_um(comp.points)
        cell.add(gdstk.Polygon(pts, layer=gds_layer, datatype=datatype))

    elif comp.kind == ComponentKind.PATH:
        if not comp.points or len(comp.points) < 2:
            return
        pts   = _pts_um(comp.points)
        width = _um(comp.path_width) if comp.path_width else 0.001
        fp    = gdstk.FlexPath(
            pts[0], width,
            layer=gds_layer, datatype=datatype,
        )
        for pt in pts[1:]:
            fp.segment(pt)
        cell.add(fp)


# ── Undercut ring export ──────────────────────────────────────────────────────

# GDS layer written for undercut rings (matches the visual L2 convention).
UNDERCUT_RING_LAYER = 2
UNDERCUT_RING_DATATYPE = 0

# Only components on this app-layer contribute geometry to undercut rings.
# Components on other layers are ignored when building the ring base shape.
UNDERCUT_SOURCE_LAYER = 1


def _l11_tip_half_poly(l11_comp) -> "gdstk.Polygon | None":
    """
    Return a gdstk.Polygon rectangle (in µm, GDS Y-negated) that covers the
    tip half of an L11 clip component — from the midpoint of L11 to the
    narrow tip face.  This is subtracted from the undercut ring so the ring
    cap sits exactly at clip_length/2 inward from the clip boundary, matching
    the cap-plane clipping applied visually by _taper_quad_ring.

    Returns None if L11 geometry is degenerate.
    """
    import math

    pts = l11_comp.points or []
    unique = list(pts)
    if len(unique) > 1 and unique[-1] == unique[0]:
        unique = unique[:-1]
    if len(unique) != 4:
        return None

    def _dist(a, b):
        return math.hypot(b.x - a.x, b.y - a.y)

    edges    = [(i, (i+1)%4, _dist(unique[i], unique[(i+1)%4])) for i in range(4)]
    by_len   = sorted(edges, key=lambda e: e[2])
    face     = by_len[:2]   # two shortest = end-faces

    clip_face   = max(face, key=lambda e: e[2])   # wider = clip boundary
    narrow_face = min(face, key=lambda e: e[2])   # narrower = tip

    ci, cj = clip_face[0],   clip_face[1]
    ni, nj = narrow_face[0], narrow_face[1]
    clip_a  = unique[ci];  clip_b  = unique[cj]
    tip_a   = unique[ni];  tip_b   = unique[nj]

    clip_mid_x = (clip_a.x + clip_b.x) / 2.0
    clip_mid_y = (clip_a.y + clip_b.y) / 2.0
    tip_mid_x  = (tip_a.x  + tip_b.x)  / 2.0
    tip_mid_y  = (tip_a.y  + tip_b.y)  / 2.0

    dx = tip_mid_x - clip_mid_x
    dy = tip_mid_y - clip_mid_y
    clip_length = math.hypot(dx, dy)
    if clip_length == 0:
        return None

    ux, uy = dx / clip_length, dy / clip_length   # boundary → tip unit vector
    half = clip_length / 2.0

    # Cap plane passes through clip_a + ux*half, clip_b + ux*half
    cap_ax = clip_a.x + ux * half;  cap_ay = clip_a.y + uy * half
    cap_bx = clip_b.x + ux * half;  cap_by = clip_b.y + uy * half

    # Large rectangle on the tip side of the cap plane (DBU coords)
    LARGE = clip_length * 4.0
    px_, py_ = -uy, ux   # perpendicular direction

    c0x, c0y = cap_ax + px_*LARGE, cap_ay + py_*LARGE
    c1x, c1y = cap_ax - px_*LARGE, cap_ay - py_*LARGE
    c2x, c2y = c1x + ux*LARGE,     c1y + uy*LARGE
    c3x, c3y = c0x + ux*LARGE,     c0y + uy*LARGE

    # Convert DBU → µm and negate Y for GDS convention
    def to_um(x, y):
        return (x / 1000.0, -y / 1000.0)

    pts_um = [to_um(c0x, c0y), to_um(c1x, c1y),
              to_um(c2x, c2y), to_um(c3x, c3y)]
    return gdstk.Polygon(pts_um, layer=0, datatype=0)


def export_undercut_rings(
    cell,
    design: DesignScene,
    overlay,
    layer_map: LayerMap,
    gds_layer: int = UNDERCUT_RING_LAYER,
    datatype: int = UNDERCUT_RING_DATATYPE,
) -> int:
    """
    Compute and emit undercut ring polygons for all non-excluded objects.

    Only geometry on UNDERCUT_SOURCE_LAYER (app-layer 1) contributes to the
    ring base shape — components on other layers are ignored.  For groups this
    means only the Layer-1 members are unioned; if a group has no Layer-1
    members at all, no ring is emitted for it.  For ungrouped components, only
    those on Layer 1 receive a ring.

    The ring is built the same way as the visual overlay:
      1. Collect the Layer-1 filled shape polygon(s) for the object.
      2. Offset outward by overlay.offset_um using gdstk.offset().
      3. Boolean-subtract the original shape → hollow ring.

    Ungrouped components and groups are handled separately so that group
    members are unioned first (matching the visual overlay behaviour).

    Returns the number of ring shapes added to the cell.
    """
    if not overlay.is_enabled:
        return 0

    offset_um = overlay.offset_um
    added = 0

    from core.cell_library import LAYER_NARROW_END

    grouped_ids: Set[str] = set()
    for group in design.groups:
        grouped_ids.update(group.member_ids)

    comp_map = {c.id: c for c in design.components}

    # ── Groups ────────────────────────────────────────────────────
    for group in design.groups:
        if overlay.is_excluded(group.id):
            continue

        members = [comp_map[cid] for cid in group.member_ids if cid in comp_map]
        if not members:
            continue

        # Only Layer-1 members contribute geometry to the ring base.
        base_polys: list[gdstk.Polygon] = []
        for comp in members:
            if comp.layer != UNDERCUT_SOURCE_LAYER:
                continue
            base_polys.extend(_comp_to_gdstk_polys(comp, 0, 0))

        if not base_polys:
            continue

        # Build tip-half clip rectangles from L11 members.  Each rectangle
        # covers the half of L11 from the midpoint to the narrow tip,
        # matching the cap-plane clipping done by _taper_quad_ring visually.
        clip_polys: list[gdstk.Polygon] = []
        for comp in members:
            if comp.layer == LAYER_NARROW_END:
                tip_rect = _l11_tip_half_poly(comp)
                if tip_rect is not None:
                    clip_polys.append(tip_rect)

        masks_um = overlay.get_masks_um(group.id)
        ring_polys = _build_gdstk_ring(
            base_polys, offset_um, gds_layer, datatype,
            masks_um=masks_um, clip_polys=clip_polys or None,
        )
        if ring_polys:
            cell.add(*ring_polys)
            added += len(ring_polys)

    # ── Ungrouped components ───────────────────────────────────────
    for comp in design.components:
        if comp.id in grouped_ids:
            continue
        if overlay.is_excluded(comp.id):
            continue
        if comp.layer != UNDERCUT_SOURCE_LAYER:
            continue

        base_polys = _comp_to_gdstk_polys(comp, 0, 0)
        masks_um = overlay.get_masks_um(comp.id)
        ring_polys = _build_gdstk_ring(
            base_polys, offset_um, gds_layer, datatype,
            masks_um=masks_um,
        )
        if ring_polys:
            cell.add(*ring_polys)
            added += len(ring_polys)

    return added


def _build_gdstk_ring(
    base_polys: list,
    offset_um: float,
    gds_layer: int,
    datatype: int,
    masks_um: list | None = None,
    clip_polys: list | None = None,
) -> list:
    """
    Given a list of gdstk.Polygon objects representing the filled base shape,
    return the ring = expanded_outline − base_shape as a list of Polygons.

    *masks_um* is an optional list of (x_min, y_min, x_max, y_max) tuples
    in µm (GDS Y convention, already negated) from UndercutOverlay.get_masks_um().
    Each rectangle is subtracted from the ring after it is built, exactly
    matching what the visual eraser removed on screen.

    *clip_polys* is an optional list of gdstk.Polygon objects (e.g. L11 narrow-
    tip clip shapes) that are subtracted from the ring so the expansion never
    bleeds into those regions.  This mirrors the QPainterPath subtraction done
    by _taper_quad_ring in the visual overlay.
    """
    if not base_polys:
        return []

    # Step 1: union the base shapes so overlapping members don't double-expand.
    if len(base_polys) > 1:
        unioned = gdstk.boolean(base_polys, [], "or", layer=0, datatype=0)
    else:
        unioned = list(base_polys)

    if not unioned:
        return []

    # Step 2: offset outward by offset_um.
    expanded = gdstk.offset(unioned, offset_um, join="miter", tolerance=0.01)
    if not expanded:
        return []

    # Step 3: subtract original → hollow ring.
    ring = gdstk.boolean(expanded, unioned, "not", layer=gds_layer, datatype=datatype)
    if not ring:
        return []

    # Step 4: subtract L11 clip polygons so the ring never overlaps the narrow
    # tip region — mirrors _taper_quad_ring's QPainterPath subtraction.
    if clip_polys:
        ring = gdstk.boolean(ring, clip_polys, "not",
                             layer=gds_layer, datatype=datatype)
        if not ring:
            return []

    # Step 5: subtract each user-drawn mask rectangle.
    if masks_um:
        mask_polys = []
        for (x0, y0, x1, y1) in masks_um:
            mask_polys.append(gdstk.rectangle(
                (x0, y0), (x1, y1), layer=0, datatype=0
            ))
        ring = gdstk.boolean(ring, mask_polys, "not",
                             layer=gds_layer, datatype=datatype)

    return ring if ring else []


# ── L2 merge pass ─────────────────────────────────────────────────────────────

def _merge_undercut_layer(cell, layer_map: LayerMap) -> None:
    """
    Boolean-OR all polygons on UNDERCUT_RING_LAYER (app-layer 2) that are
    already in *cell*, replace them with the unified result.

    This is called as the final step of export_gds(), after both the regular
    component geometry AND any overlay-derived undercut ring polygons have been
    added.  It ensures that:

      • Narrow-end undercut polygons emitted by taper_segment cells (L2) merge
        with outer undercut rings (also L2) that overlap or touch them.
      • Any other coincident L2 shapes (e.g. from overlapping group rings) are
        also cleaned up — no duplicate geometry in the final file.

    Only the resolved (gds_layer, datatype) pair that corresponds to app-layer 2
    is processed.  All other layers are left untouched.
    """
    gds_layer, datatype = _resolve(UNDERCUT_RING_LAYER, layer_map)

    # Collect every polygon on the target (gds_layer, datatype) pair.
    # cell.polygons returns all Polygon objects currently in the cell.
    l2_polys = [
        p for p in cell.polygons
        if p.layer == gds_layer and p.datatype == datatype
    ]

    if len(l2_polys) < 2:
        return  # nothing to merge — zero or one polygon, no-op

    # Remove the originals from the cell so we can replace them.
    for p in l2_polys:
        cell.remove(p)

    # Boolean OR → unified outline(s)
    merged = gdstk.boolean(
        l2_polys, [],
        operation="or",
        layer=gds_layer,
        datatype=datatype,
    )
    if merged:
        cell.add(*merged)
    else:
        # Degenerate — put the originals back rather than silently deleting them
        cell.add(*l2_polys)


# ── Verification ──────────────────────────────────────────────────────────────

def _verify(path: Path, cell_name: str) -> dict:
    """Re-read the written file and return a summary."""
    try:
        lib   = gdstk.read_gds(str(path))
        cells = {c.name: c for c in lib.cells}
        cell  = cells.get(cell_name)
        if cell is None:
            raise ExportError(f"Cell '{cell_name}' not found in exported file.")

        all_polys = cell.get_polygons()
        layers    = sorted({(p.layer, p.datatype) for p in all_polys})

        if all_polys:
            xs = [p for poly in all_polys for p in poly.points[:, 0]]
            ys = [p for poly in all_polys for p in poly.points[:, 1]]
            bbox = (min(xs), min(ys), max(xs), max(ys))
        else:
            bbox = (0.0, 0.0, 0.0, 0.0)

        return {
            "path":    str(path),
            "cell":    cell_name,
            "shapes":  len(all_polys),
            "layers":  layers,
            "bbox_um": bbox,
        }
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"Verification failed: {exc}") from exc