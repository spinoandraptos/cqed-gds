"""
core/serialiser.py — JSON save / load for DesignScene.

Schema v1:
{
  "version": 1,
  "name": str,
  "components": [
    {
      "id": str,
      "kind": "RECTANGLE" | "POLYGON" | "PATH",
      "layer": int,
      "origin": [x, y],          # DBU ints
      "width": int,
      "height": int,
      "path_width": int | null,
      "points": [[x,y], ...] | null,
      "ports": [
        {"id": str, "name": str, "offset": [x,y], "side": "NORTH|SOUTH|EAST|WEST"}
      ]
    }
  ],
  "connections": [
    {"id": str, "comp_a": str, "port_a": str, "comp_b": str, "port_b": str}
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.model import (
    DesignScene, GDSComponent, ComponentKind,
    Point, Port, PortSide, Connection, ComponentGroup
)


class SerialisationError(Exception):
    pass


# ── Encode ────────────────────────────────────────────────────────────────────

def save(design: DesignScene, path: str | Path, overlay=None) -> None:
    try:
        data = _encode(design, overlay=overlay)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except SerialisationError:
        raise
    except Exception as exc:
        raise SerialisationError(f"Save failed: {exc}") from exc


def _encode(design: DesignScene, overlay=None) -> dict:
    d = {
        "version":     1,
        "name":        design.name,
        "components":  [_encode_comp(c) for c in design.components],
        "connections": [_encode_conn(cn) for cn in design.connections],
        "groups":      [_encode_group(g) for g in design.groups],
    }
    if overlay is not None:
        masks = {}
        for obj_id, paths in overlay._masks.items():
            rects = []
            for path in paths:
                br = path.boundingRect()
                rects.append([br.x(), br.y(), br.width(), br.height()])
            if rects:
                masks[obj_id] = rects
        if masks:
            d["undercut_masks"] = masks
    return d

def _encode_group(g) -> dict:
    d: dict = {"id": g.id, "name": g.name, "member_ids": list(g.member_ids)}
    # Persist optional dynamic attrs so merged/cell groups survive save/load.
    if hasattr(g, "cell_id") and g.cell_id:
        d["cell_id"] = g.cell_id
    if hasattr(g, "_cell_params") and g._cell_params:
        d["cell_params"] = dict(g._cell_params)
    if hasattr(g, "_cell_subgroups") and g._cell_subgroups:
        d["cell_subgroups"] = g._cell_subgroups
    if hasattr(g, "_cell_rotation_steps") and g._cell_rotation_steps:
        d["cell_rotation_steps"] = g._cell_rotation_steps
    if (hasattr(g, "_cell_rotation_cx") and g._cell_rotation_cx is not None
            and hasattr(g, "_cell_rotation_cy") and g._cell_rotation_cy is not None):
        d["cell_rotation_cx"] = g._cell_rotation_cx
        d["cell_rotation_cy"] = g._cell_rotation_cy
    if hasattr(g, "_cell_origin") and g._cell_origin is not None:
        d["cell_origin"] = [g._cell_origin.x, g._cell_origin.y]
    return d


def _encode_comp(c: GDSComponent) -> dict:
    return {
        "id":         c.id,
        "kind":       c.kind.name,
        "layer":      c.layer,
        "origin":     [c.origin.x, c.origin.y],
        "width":      c.width,
        "height":     c.height,
        "path_width": c.path_width,
        "points":     [[p.x, p.y] for p in c.points] if c.points else None,
        "ports":      [_encode_port(p) for p in c.ports],
        "is_undercut": c.is_undercut,
    }


def _encode_port(p: Port) -> dict:
    return {
        "id":     p.id,
        "name":   p.name,
        "offset": [p.offset.x, p.offset.y],
        "side":   p.side.name,
    }


def _encode_conn(cn: Connection) -> dict:
    return {
        "id":     cn.id,
        "comp_a": cn.comp_a, "port_a": cn.port_a,
        "comp_b": cn.comp_b, "port_b": cn.port_b,
    }


# ── Decode ────────────────────────────────────────────────────────────────────

def load(path: str | Path) -> tuple[DesignScene, dict]:
    """Returns (design, masks_dict) where masks_dict is obj_id → list of [x,y,w,h]."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        design = _decode(data)
        masks  = data.get("undercut_masks", {})
        return design, masks
    except SerialisationError:
        raise
    except Exception as exc:
        raise SerialisationError(f"Load failed: {exc}") from exc

def load_with_masks(path: str | Path) -> tuple:
    """Returns (DesignScene, masks_dict) where masks_dict is obj_id → list of [x,y,w,h]."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        design = _decode(data)
        masks  = data.get("undercut_masks", {})
        return design, masks
    except SerialisationError:
        raise
    except Exception as exc:
        raise SerialisationError(f"Load failed: {exc}") from exc

def _decode(data: dict) -> DesignScene:
    ver = data.get("version")
    if ver != 1:
        raise SerialisationError(f"Unknown file version: {ver!r}")

    design = DesignScene(name=data.get("name", "TOP"))

    for cd in data.get("components", []):
        design._components.append(_decode_comp(cd))

    for cn in data.get("connections", []):
        design._connections.append(_decode_conn(cn))

    for gd in data.get("groups", []):
        g = ComponentGroup(id=gd["id"], name=gd["name"],
                           member_ids=gd["member_ids"])
        if "cell_id" in gd:
            g.cell_id = gd["cell_id"]
        if "cell_params" in gd:
            g._cell_params = dict(gd["cell_params"])
        if "cell_subgroups" in gd:
            g._cell_subgroups = gd["cell_subgroups"]
        if "cell_rotation_steps" in gd:
            g._cell_rotation_steps = gd["cell_rotation_steps"]
        if "cell_rotation_cx" in gd:
            g._cell_rotation_cx = gd["cell_rotation_cx"]
            g._cell_rotation_cy = gd["cell_rotation_cy"]
        if "cell_origin" in gd:
            from core.model import Point as _Point
            g._cell_origin = _Point(gd["cell_origin"][0], gd["cell_origin"][1])
        design._groups.append(g)

    design.is_dirty = False
    return design


def _decode_comp(d: dict) -> GDSComponent:
    try:
        kind   = ComponentKind[d["kind"]]
        origin = Point(d["origin"][0], d["origin"][1])
        points = (
            [Point(xy[0], xy[1]) for xy in d["points"]]
            if d.get("points") else None
        )
        ports  = [_decode_port(p) for p in d.get("ports", [])]
        return GDSComponent(
            id         = d["id"],
            kind       = kind,
            layer      = d["layer"],
            origin     = origin,
            width      = d.get("width", 0),
            height     = d.get("height", 0),
            path_width = d.get("path_width"),
            points     = points,
            ports      = ports,
            is_undercut = d.get("is_undercut", False), 
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise SerialisationError(f"Malformed component record: {exc}") from exc


def _decode_port(d: dict) -> Port:
    return Port(
        id     = d["id"],
        name   = d["name"],
        offset = Point(d["offset"][0], d["offset"][1]),
        side   = PortSide[d["side"]],
    )


def _decode_conn(d: dict) -> Connection:
    return Connection(
        id     = d["id"],
        comp_a = d["comp_a"], port_a = d["port_a"],
        comp_b = d["comp_b"], port_b = d["port_b"],
    )