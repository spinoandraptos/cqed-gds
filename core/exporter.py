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

Verification:
  After writing, re-read the file with gdstk and return a summary dict so
  the caller can show a confirmation dialog without re-importing the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple, Optional

import gdstk

from core.model import DesignScene, GDSComponent, ComponentKind, dbu_to_um, DBU_PER_UM

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


# ── Core export ───────────────────────────────────────────────────────────────

class ExportError(Exception):
    pass


def export_gds(
    design: DesignScene,
    path: str | Path,
    layer_map: LayerMap,
    precision: float = 1e-9,   # 1 nm
    unit: float = 1e-6,        # µm
) -> dict:
    """
    Write *design* to a GDS-II file at *path*.

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

        for comp in design.components:
            gds_layer, datatype = _resolve(comp.layer, layer_map)
            _add_component(cell, comp, gds_layer, datatype)

        lib.write_gds(str(path))

    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"gdstk error: {exc}") from exc

    # ── Verify by re-reading ──────────────────────────────────────────────────
    return _verify(path, design.name)


def _add_component(cell, comp, gds_layer, datatype):
    if comp.kind == ComponentKind.RECTANGLE:
        x0 =  _um(comp.origin.x)
        y0 = -_um(comp.origin.y)           # negate Y
        x1 = x0 + _um(comp.width)
        y1 = y0 - _um(comp.height)         # negate Y (height goes the other way now)
        cell.add(gdstk.rectangle(
            (x0, y0), (x1, y1),
            layer=gds_layer, datatype=datatype,
        ))

    elif comp.kind == ComponentKind.POLYGON:
        if not comp.points or len(comp.points) < 3:
            return
        pts = _pts_um(comp.points)          # already negates Y
        cell.add(gdstk.Polygon(pts, layer=gds_layer, datatype=datatype))

    elif comp.kind == ComponentKind.PATH:
        if not comp.points or len(comp.points) < 2:
            return
        pts   = _pts_um(comp.points)        # already negates Y
        width = _um(comp.path_width) if comp.path_width else 0.001
        fp    = gdstk.FlexPath(
            pts[0], width,
            layer=gds_layer, datatype=datatype,
        )
        for pt in pts[1:]:
            fp.segment(pt)
        cell.add(fp)


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
