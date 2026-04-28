"""
component_model.py — Data model for placeable components.

Each ComponentInstance holds:
  - type name
  - position (µm)
  - parameters (overrides on top of Config defaults)
  - ports: named connection points in local coords (µm)

GDSRenderer.render(instance, cfg) returns a list of (layer, [(x,y)...]) polygons
by calling into the existing primitives/components_lib code.
"""

from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import Any

import gdspy
import numpy as np

from config import Config


# ── Layer colour map (layer → Qt colour string) ──────────────────────────────

LAYER_COLORS: dict[int, str] = {
    1:  "#7F77DD",   # Branch
    4:  "#F0997B",   # Cap1
    5:  "#5DCAA5",   # Biysk junction
    6:  "#EF9F27",   # Cap2
    10: "#E24B4A",   # JJ
    11: "#888780",   # Narrow end
}

LAYER_NAMES: dict[int, str] = {
    1: "Branch",
    4: "Cap 1",
    5: "Biysk junc.",
    6: "Cap 2",
    10: "JJ",
    11: "Narrow end",
}


# ── Port definition ───────────────────────────────────────────────────────────

@dataclass
class Port:
    """Named connection point in component-local coordinates (µm)."""
    name: str
    x: float          # local x offset from component origin
    y: float          # local y offset from component origin
    direction: str    # '+x' | '-x' | '+y' | '-y'  (outgoing wire direction)


# ── Component type catalogue ──────────────────────────────────────────────────

@dataclass
class ComponentType:
    name: str                       # display name
    type_id: str                    # unique key
    params: dict[str, Any]          # default parameters
    port_defs: list[dict]           # port specs (evaluated at render time)
    description: str = ""


def _make_square_ports(params: dict, cfg: Config) -> list[Port]:
    h = cfg.SQUARE_SIZE / 2
    return [
        Port("top",    0,  h, "+y"),
        Port("right",  h,  0, "+x"),
        Port("bottom", 0, -h, "-y"),
        Port("left",  -h,  0, "-x"),
    ]


def _make_jj_ports(params: dict, cfg: Config) -> list[Port]:
    s = cfg.JUNCTION_SQUARE_SIZE
    L = cfg.JUNCTION_LEAD_LENGTH
    return [
        Port("lead_in",   0,            0,   "-x"),
        Port("down_out",  L + s / 2,   -L,  "-y"),
        Port("right_out", L + 2 * s + 0.9, 0, "+x"),
        Port("top_out",   L + s / 2,    s + 0.9, "+y"),
    ]


def _make_taper_ports(params: dict, cfg: Config) -> list[Port]:
    direction = params.get("direction", "+x")
    L = cfg.FINAL_TAPER_LENGTH + cfg.FINAL_PAD_LENGTH
    offsets = {"+x": (L, 0), "-x": (-L, 0), "+y": (0, L), "-y": (0, -L)}
    ox, oy = offsets[direction]
    return [
        Port("narrow", 0, 0, direction),
        Port("wide",   ox, oy, direction),
    ]


COMPONENT_TYPES: dict[str, ComponentType] = {
    "square_node": ComponentType(
        name="Square node",
        type_id="square_node",
        params={"cap_style": "top", "undercut_style": "right"},
        port_defs=[],
        description="Bonding square with caps and L-undercut",
    ),
    "manhattan_jj": ComponentType(
        name="Manhattan JJ",
        type_id="manhattan_jj",
        params={},
        port_defs=[],
        description="Manhattan-style Josephson junction stack",
    ),
    "taper_pad": ComponentType(
        name="Taper pad",
        type_id="taper_pad",
        params={"direction": "+x", "layer": 1},
        port_defs=[],
        description="Smooth cosine taper → wide overlap pad",
    ),
    "snake_route": ComponentType(
        name="Snake route",
        type_id="snake_route",
        params={},
        port_defs=[],
        description="Pre-routed snake path with taper pad",
    ),
    "top_branch": ComponentType(
        name="Top branch",
        type_id="top_branch",
        params={},
        port_defs=[],
        description="+y → left turn → -x path with taper pad",
    ),
    "wire": ComponentType(
        name="Wire segment",
        type_id="wire",
        params={"direction": "+x", "length": 5.0, "width": 0.3, "layer": 5},
        port_defs=[],
        description="Simple rectangular wire segment",
    ),
}


# ── Component instance ────────────────────────────────────────────────────────

class ComponentInstance:
    """One placed component on the canvas."""

    _id_counter = 0

    def __init__(self, type_id: str, x: float = 0.0, y: float = 0.0,
                 params: dict | None = None):
        ComponentInstance._id_counter += 1
        self.inst_id = ComponentInstance._id_counter
        self.type_id = type_id
        self.x = x          # canvas position in µm
        self.y = y
        ctype = COMPONENT_TYPES[type_id]
        self.params: dict[str, Any] = copy.deepcopy(ctype.params)
        if params:
            self.params.update(params)

        # Wire connections: port_name → (other_inst_id, other_port_name)
        self.connections: dict[str, tuple[int, str]] = {}

    @property
    def label(self) -> str:
        return f"{COMPONENT_TYPES[self.type_id].name} #{self.inst_id}"

    def get_ports(self, cfg: Config) -> list[Port]:
        """Return ports in component-local coordinates."""
        if self.type_id == "square_node":
            return _make_square_ports(self.params, cfg)
        elif self.type_id == "manhattan_jj":
            return _make_jj_ports(self.params, cfg)
        elif self.type_id == "taper_pad":
            return _make_taper_ports(self.params, cfg)
        elif self.type_id == "wire":
            d = self.params.get("direction", "+x")
            L = self.params.get("length", 5.0)
            ends = {"+x": (L, 0), "-x": (-L, 0), "+y": (0, L), "-y": (0, -L)}
            ex, ey = ends[d]
            opp = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
            return [Port("start", 0, 0, opp[d]), Port("end", ex, ey, d)]
        else:
            # Generic: single origin port
            return [Port("origin", 0, 0, "+x")]

    def world_ports(self, cfg: Config) -> list[Port]:
        """Ports in world coordinates."""
        result = []
        for p in self.get_ports(cfg):
            result.append(Port(p.name, self.x + p.x, self.y + p.y, p.direction))
        return result


# ── GDS polygon renderer ──────────────────────────────────────────────────────

PolyData = list[tuple[int, list[tuple[float, float]]]]  # [(layer, [(x,y)...])]


def _collect_polygons(parts: list) -> PolyData:
    """Extract (layer, points) from a list of gdspy objects."""
    result = []
    for obj in parts:
        if obj is None:
            continue
        if hasattr(obj, "polygons"):
            # PolygonSet or Path
            for poly, layer in zip(obj.polygons, obj.layers):
                result.append((layer, [(float(x), float(y)) for x, y in poly]))
        elif hasattr(obj, "points"):
            # Polygon
            layer = obj.layer if hasattr(obj, "layer") else 0
            result.append((layer, [(float(x), float(y)) for x, y in obj.points]))
        elif hasattr(obj, "get_polygons"):
            for poly in obj.get_polygons(by_spec=True).items():
                spec, polys = poly
                for pts in polys:
                    result.append((spec[0], [(float(x), float(y)) for x, y in pts]))
    return result


def render_instance(inst: ComponentInstance, cfg: Config) -> PolyData:
    """
    Call the underlying gdspy functions for this instance and return
    a list of (layer, polygon_points) ready for the canvas to draw.
    """
    from primitives import add_rect, add_square, add_taper_pad, smooth_taper
    from undercuts import (add_top_caps, add_side_caps,
                           add_L_undercut_right, add_L_undercut_top)
    from components_lib import (add_square_node, add_manhattan_junction,
                                add_top_branch, add_snake_right_branch)

    parts: list = []
    x, y = inst.x, inst.y

    if inst.type_id == "square_node":
        cap_style      = inst.params.get("cap_style", "top")
        undercut_style = inst.params.get("undercut_style", "right")
        add_square_node(parts, x, y, cap_style, undercut_style, cfg)

    elif inst.type_id == "manhattan_jj":
        add_manhattan_junction(parts, x, y, cfg)

    elif inst.type_id == "taper_pad":
        direction = inst.params.get("direction", "+x")
        layer     = inst.params.get("layer", cfg.LAYER_BRANCH)
        add_taper_pad(parts, x, y, direction, layer, cfg)

    elif inst.type_id == "top_branch":
        add_top_branch(parts, (x, y), cfg)

    elif inst.type_id == "snake_route":
        add_snake_right_branch(parts, (x, y), cfg)

    elif inst.type_id == "wire":
        direction = inst.params.get("direction", "+x")
        length    = inst.params.get("length", 5.0)
        width     = inst.params.get("width", cfg.WIRE_WIDTH)
        layer     = inst.params.get("layer", cfg.LAYER_BIYSK_JUNCTION)
        hw = width / 2
        if direction == "+x":
            add_rect(parts, (x, y - hw), (x + length, y + hw), layer)
        elif direction == "-x":
            add_rect(parts, (x - length, y - hw), (x, y + hw), layer)
        elif direction == "+y":
            add_rect(parts, (x - hw, y), (x + hw, y + length), layer)
        elif direction == "-y":
            add_rect(parts, (x - hw, y - length), (x + hw, y), layer)

    return _collect_polygons(parts)


def export_to_gds(instances: list[ComponentInstance], cfg: Config,
                  filepath: str) -> None:
    """Render all instances and write a GDS file."""
    gdspy.current_library = gdspy.GdsLibrary()
    cell = gdspy.Cell("EDITOR_LAYOUT")

    for inst in instances:
        polys = render_instance(inst, cfg)
        for layer, pts in polys:
            if len(pts) >= 3:
                cell.add(gdspy.Polygon(pts, layer=layer))

    gdspy.write_gds(filepath)
