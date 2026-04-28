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
    """
    Port positions match exactly how _build_three_square_chain (and the layout
    code in general) attaches wire leads to the square.

    The convention throughout the layout is: a WIRE_WIDTH-wide lead is placed
    flush with one corner of the square, so its centreline sits at
    ±(h − WIRE_WIDTH/2) perpendicular to the exit direction.

    Which corner is used depends on cap_style / undercut_style:

      cap_style="top",  undercut_style="right"  (sq1, sq3)
        right edge  → lead exits +x, wire top-flush  → port at ( h,  h−W/2)
        bottom edge → lead exits −y, wire left-flush  → port at (−h+W/2, −h)

      cap_style="side", undercut_style="top"    (sq2)
        top edge    → lead exits +y, wire right-flush → port at ( h−W/2,  h)
        left edge   → lead exits −x, wire bottom-flush→ port at (−h, −(h−W/2))

    Unused cardinal sides keep a centred port for potential future connections.
    """
    h = cfg.SQUARE_SIZE / 2
    w = cfg.WIRE_WIDTH
    # perpendicular offset: wire centreline is this far from the square centre
    off = h - w / 2   # e.g. 1.0 − 0.15 = 0.85

    cap_style      = params.get("cap_style",      "top")
    undercut_style = params.get("undercut_style", "right")

    if cap_style == "top" and undercut_style == "right":
        # right lead: exits +x, top-flush
        # bottom lead: exits -y, left-flush
        return [
            Port("right",  h,     off,  "+x"),
            Port("bottom", -off, -h,    "-y"),
            Port("top",    0,     h,    "+y"),   # centred fallback
            Port("left",  -h,     0,    "-x"),   # centred fallback
        ]
    elif cap_style == "side" and undercut_style == "top":
        # top lead: exits +y, right-flush
        # left lead: exits -x, bottom-flush
        return [
            Port("top",    off,  h,    "+y"),
            Port("left",  -h,   -off,  "-x"),
            Port("right",  h,    0,    "+x"),   # centred fallback
            Port("bottom", 0,   -h,    "-y"),   # centred fallback
        ]
    elif cap_style == "top" and undercut_style == "top":
        return [
            Port("top",    off,  h,    "+y"),
            Port("bottom", 0,   -h,    "-y"),
            Port("right",  h,    0,    "+x"),
            Port("left",  -h,    0,    "-x"),
        ]
    else:  # cap_style="side", undercut_style="right"
        return [
            Port("right",  h,    off,  "+x"),
            Port("left",  -h,    0,    "-x"),
            Port("top",    0,    h,    "+y"),
            Port("bottom", 0,   -h,    "-y"),
        ]


def _make_jj_ports(params: dict, cfg: Config) -> list[Port]:
    s = cfg.JUNCTION_SQUARE_SIZE
    L = cfg.JUNCTION_LEAD_LENGTH
    return [
        Port("lead_in",   0,                  0,              "-x"),
        Port("down_out",  L + s / 2,         -(s / 2 + L),   "-y"),  # bottom of down lead
        Port("right_out", L + 2 * s + 0.9,   0,              "+x"),
        Port("top_out",   L + s / 2,          s / 2 + s + 0.9, "+y"),  # top of CAP2
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



def _make_turn_ports(params: dict, cfg: Config) -> list[Port]:
    """
    Ports for a standalone 90° turn.

    The turn arc has radius TURN_RADIUS.  Entry travels in `entry_dir`;
    exit travels 90° away according to `turn_dir` ('l' CCW, 'r' CW).

    Port positions:
      "entry" — at the arc start (origin = 0,0)
      "exit"  — at the arc end, offset by the swept arc geometry
    """
    entry_dir = params.get("entry_dir", "+x")
    turn_dir  = params.get("turn_dir",  "l")
    R = cfg.TURN_RADIUS

    # Map (entry_dir, turn_dir) → exit offset (dx, dy) and exit direction
    # A left turn (CCW) rotates the heading +90°; right (CW) rotates -90°.
    _exit_map = {
        ("+x", "l"): (( R,  R), "+y"),
        ("+x", "r"): (( R, -R), "-y"),
        ("-x", "l"): ((-R, -R), "-y"),
        ("-x", "r"): ((-R,  R), "+y"),
        ("+y", "l"): ((-R,  R), "-x"),
        ("+y", "r"): (( R,  R), "+x"),
        ("-y", "l"): (( R, -R), "+x"),
        ("-y", "r"): ((-R, -R), "-x"),
    }
    (dx, dy), exit_dir = _exit_map[(entry_dir, turn_dir)]

    # entry port faces opposite to entry_dir (wire comes in from outside)
    _opp = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
    return [
        Port("entry", 0,  0,  _opp[entry_dir]),
        Port("exit",  dx, dy, exit_dir),
    ]


def _make_branch_segment_ports(params: dict, cfg: Config) -> list[Port]:
    direction = params.get("direction", "+x")
    length    = params.get("length", 10.0)
    ends = {"+x": (length, 0), "-x": (-length, 0),
            "+y": (0, length), "-y": (0, -length)}
    ex, ey = ends[direction]
    opp = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
    return [
        Port("entry", 0,  0,  opp[direction]),
        Port("exit",  ex, ey, direction),
    ]


def _make_taper_segment_ports(params: dict, cfg: Config) -> list[Port]:
    direction  = params.get("direction",  "+x")
    length     = params.get("length",     10.0)
    narrow_end = params.get("narrow_end", "start")

    ends = {"+x": (length, 0), "-x": (-length, 0),
            "+y": (0, length), "-y": (0, -length)}
    ex, ey = ends[direction]
    opp = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}

    if narrow_end == "start":
        entry_label, exit_label = "narrow", "wide"
    else:
        entry_label, exit_label = "wide", "narrow"

    return [
        Port(entry_label, 0,  0,  opp[direction]),
        Port(exit_label,  ex, ey, direction),
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
    "taper_segment": ComponentType(
        name="Taper segment",
        type_id="taper_segment",
        params={"direction": "+x", "length": 6.1, "narrow_end": "start"},
        port_defs=[],
        description="Linear WIRE_WIDTH↔TAPER_WIDTH wedge (L1) with auto layer-11 narrow tip",
    ),
    "branch_segment": ComponentType(
        name="Branch segment",
        type_id="branch_segment",
        params={"direction": "+x", "length": 10.0},
        port_defs=[],
        description="Straight TAPER_WIDTH branch segment (L1), like those in snake/top branch",
    ),
    "turn": ComponentType(
        name="Turn (90°)",
        type_id="turn",
        params={"entry_dir": "+x", "turn_dir": "l"},
        port_defs=[],
        description="90° arc turn, configurable entry direction and handedness",
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

# ── Rotation helpers ──────────────────────────────────────────────────────────

def _rotate_dir(direction: str, steps: int) -> str:
    """Rotate a cardinal direction by `steps` × 90° CCW."""
    cycle = ["+x", "+y", "-x", "-y"]
    return cycle[(cycle.index(direction) + steps) % 4]


def _rotate_pt(x: float, y: float, steps: int) -> tuple[float, float]:
    """Rotate point (x,y) around origin by steps × 90° CCW."""
    for _ in range(steps % 4):
        x, y = -y, x
    return x, y


def _rotate_polygon(pts: list[tuple[float, float]],
                    steps: int) -> list[tuple[float, float]]:
    """Rotate a list of (x,y) points around origin by steps × 90° CCW."""
    return [_rotate_pt(x, y, steps) for x, y in pts]


class ComponentInstance:
    """One placed component on the canvas."""

    _id_counter = 0

    def __init__(self, type_id: str, x: float = 0.0, y: float = 0.0,
                 params: dict | None = None, rotation: int = 0):
        ComponentInstance._id_counter += 1
        self.inst_id  = ComponentInstance._id_counter
        self.type_id  = type_id
        self.x        = x
        self.y        = y
        self.rotation = rotation % 360   # 0 | 90 | 180 | 270 (CCW degrees)
        ctype = COMPONENT_TYPES[type_id]
        self.params: dict[str, Any] = copy.deepcopy(ctype.params)
        if params:
            self.params.update(params)

        # Wire connections: port_name → (other_inst_id, other_port_name)
        self.connections: dict[str, tuple[int, str]] = {}

    @property
    def rotation_steps(self) -> int:
        """Rotation in units of 90° CCW steps (0–3)."""
        return (self.rotation // 90) % 4

    def rotate_cw(self) -> None:
        """Rotate 90° clockwise."""
        self.rotation = (self.rotation + 270) % 360

    def rotate_ccw(self) -> None:
        """Rotate 90° counter-clockwise."""
        self.rotation = (self.rotation + 90) % 360

    @property
    def label(self) -> str:
        rot = f" {self.rotation}°" if self.rotation else ""
        return f"{COMPONENT_TYPES[self.type_id].name} #{self.inst_id}{rot}"

    def get_ports(self, cfg: Config) -> list[Port]:
        """Return ports in component-local coordinates (rotation applied)."""
        steps = self.rotation_steps
        if self.type_id == "square_node":
            raw = _make_square_ports(self.params, cfg)
        elif self.type_id == "manhattan_jj":
            raw = _make_jj_ports(self.params, cfg)
        elif self.type_id == "taper_pad":
            raw = _make_taper_ports(self.params, cfg)
        elif self.type_id == "taper_segment":
            raw = _make_taper_segment_ports(self.params, cfg)
        elif self.type_id == "branch_segment":
            raw = _make_branch_segment_ports(self.params, cfg)
        elif self.type_id == "turn":
            raw = _make_turn_ports(self.params, cfg)
        elif self.type_id == "wire":
            d = self.params.get("direction", "+x")
            L = self.params.get("length", 5.0)
            ends = {"+x": (L, 0), "-x": (-L, 0), "+y": (0, L), "-y": (0, -L)}
            ex, ey = ends[d]
            opp = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
            raw = [Port("start", 0, 0, opp[d]), Port("end", ex, ey, d)]
        else:
            raw = [Port("origin", 0, 0, "+x")]

        if steps == 0:
            return raw
        return [
            Port(p.name, *_rotate_pt(p.x, p.y, steps), _rotate_dir(p.direction, steps))
            for p in raw
        ]

    def world_ports(self, cfg: Config) -> list[Port]:
        """Ports in world coordinates."""
        return [
            Port(p.name, self.x + p.x, self.y + p.y, p.direction)
            for p in self.get_ports(cfg)
        ]


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

    Rotation is applied by:
      1. Rendering at origin (inst.x, inst.y) with rotation=0
      2. Translating each polygon point to be relative to the origin
      3. Rotating that local point by inst.rotation_steps × 90° CCW
      4. Translating back to world coords
    """
    from primitives import add_rect, add_square, add_taper_pad, smooth_taper
    from undercuts import (add_top_caps, add_side_caps,
                           add_L_undercut_right, add_L_undercut_top)
    from components_lib import (add_square_node, add_manhattan_junction,
                                add_top_branch, add_snake_right_branch,
                                add_turn, add_branch_segment,
                                add_taper_segment)

    parts: list = []
    x, y = inst.x, inst.y

    if inst.type_id == "square_node":
        add_square_node(parts, x, y,
                        inst.params.get("cap_style", "top"),
                        inst.params.get("undercut_style", "right"), cfg)

    elif inst.type_id == "manhattan_jj":
        add_manhattan_junction(parts, x, y, cfg)

    elif inst.type_id == "taper_pad":
        add_taper_pad(parts, x, y,
                      inst.params.get("direction", "+x"),
                      inst.params.get("layer", cfg.LAYER_BRANCH), cfg)

    elif inst.type_id == "top_branch":
        add_top_branch(parts, (x, y), cfg)

    elif inst.type_id == "snake_route":
        add_snake_right_branch(parts, (x, y), cfg)

    elif inst.type_id == "taper_segment":
        add_taper_segment(parts, (x, y),
                          inst.params.get("direction",  "+x"),
                          inst.params.get("length",     6.1),
                          inst.params.get("narrow_end", "start"), cfg)

    elif inst.type_id == "branch_segment":
        add_branch_segment(parts, (x, y),
                           inst.params.get("direction", "+x"),
                           inst.params.get("length", 10.0), cfg)

    elif inst.type_id == "turn":
        add_turn(parts, (x, y),
                 inst.params.get("entry_dir", "+x"),
                 inst.params.get("turn_dir", "l"), cfg)

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

    raw = _collect_polygons(parts)

    # Apply rotation around the instance origin (x, y)
    steps = inst.rotation_steps
    if steps == 0:
        return raw

    rotated: PolyData = []
    for layer, pts in raw:
        new_pts = []
        for px, py in pts:
            lx, ly = px - x, py - y          # to local
            rx, ry = _rotate_pt(lx, ly, steps)   # rotate
            new_pts.append((rx + x, ry + y))  # back to world
        rotated.append((layer, new_pts))
    return rotated


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