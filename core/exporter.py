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

    The ring is built the same way as the visual overlay:
      1. Collect the filled shape polygon(s) for the object (or union of group members).
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

    grouped_ids: Set[str] = set()
    for group in design.groups:
        grouped_ids.update(group.member_ids)

    comp_map = {c.id: c for c in design.components}

    # ── Groups ────────────────────────────────────────────────────────────────
    for group in design.groups:
        if overlay.is_excluded(group.id):
            continue

        members = [comp_map[cid] for cid in group.member_ids if cid in comp_map]
        if not members:
            continue

        # Union all member shapes (any layer) into one base polygon set.
        base_polys: list[gdstk.Polygon] = []
        for comp in members:
            # Use layer/datatype=0 here — only geometry matters for the ring.
            base_polys.extend(_comp_to_gdstk_polys(comp, 0, 0))

        ring_polys = _build_gdstk_ring(base_polys, offset_um, gds_layer, datatype)
        if ring_polys:
            cell.add(*ring_polys)
            added += len(ring_polys)

    # ── Ungrouped components ──────────────────────────────────────────────────
    for comp in design.components:
        if comp.id in grouped_ids:
            continue
        if overlay.is_excluded(comp.id):
            continue

        base_polys = _comp_to_gdstk_polys(comp, 0, 0)
        ring_polys = _build_gdstk_ring(base_polys, offset_um, gds_layer, datatype)
        if ring_polys:
            cell.add(*ring_polys)
            added += len(ring_polys)

    return added


def _build_gdstk_ring(
    base_polys: list,
    offset_um: float,
    gds_layer: int,
    datatype: int,
) -> list:
    """
    Given a list of gdstk.Polygon objects representing the filled base shape,
    return the ring = expanded_outline − base_shape as a list of Polygons.
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
    return ring if ring else []


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