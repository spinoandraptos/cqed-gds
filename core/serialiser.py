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

def save(design: DesignScene, path: str | Path) -> None:
    """Serialise *design* to JSON at *path*. Raises SerialisationError on failure."""
    try:
        data = _encode(design)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except SerialisationError:
        raise
    except Exception as exc:
        raise SerialisationError(f"Save failed: {exc}") from exc


def _encode(design: DesignScene) -> dict:
    return {
        "version":     1,
        "name":        design.name,
        "components":  [_encode_comp(c) for c in design.components],
        "connections": [_encode_conn(cn) for cn in design.connections],
        "groups":      [_encode_group(g) for g in design.groups],  # ← add
    }

def _encode_group(g) -> dict:
    d: dict = {"id": g.id, "name": g.name, "member_ids": list(g.member_ids)}
    # Persist optional dynamic attrs so merged/cell groups survive save/load.
    if hasattr(g, "cell_id") and g.cell_id:
        d["cell_id"] = g.cell_id
    if hasattr(g, "_cell_params") and g._cell_params:
        d["cell_params"] = dict(g._cell_params)
    if hasattr(g, "_cell_subgroups") and g._cell_subgroups:
        d["cell_subgroups"] = g._cell_subgroups
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

def load(path: str | Path) -> DesignScene:
    """Deserialise a JSON file into a fresh DesignScene. Raises SerialisationError on failure."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return _decode(data)
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