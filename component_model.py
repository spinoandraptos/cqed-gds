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
    2:  "#C060FF",   # Undercut ring
    4:  "#F0997B",   # Cap1
    5:  "#5DCAA5",   # Biysk junction
    6:  "#EF9F27",   # Cap2
    10: "#E24B4A",   # JJ
    11: "#888780",   # Narrow end
}

LAYER_NAMES: dict[int, str] = {
    1: "Branch",
    2: "Undercut ring",
    4: "Cap 1",
    5: "Biysk junc.",
    6: "Cap 2",
    10: "JJ",
    11: "Narrow end",
}

UNDERCUT_RING_LAYER     = 2
UNDERCUT_RING_THICKNESS = 0.8   # µm


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
    # Respect per-instance dimensions
    sx = params.get("square_x", cfg.SQUARE_SIZE)
    sy = params.get("square_y", cfg.SQUARE_SIZE)
    hx = sx / 2
    hy = sy / 2
    w  = cfg.WIRE_WIDTH

    cap_style      = params.get("cap_style",      "top")
    undercut_style = params.get("undercut_style", "right")

    if cap_style == "top" and undercut_style == "right":
        return [
            Port("right",   hx,          hy - w / 2,  "+x"),
            Port("bottom", -hx + w / 2, -hy,          "-y"),
            Port("top",     0,            hy,          "+y"),
            Port("left",   -hx,           0,           "-x"),
        ]
    elif cap_style == "side" and undercut_style == "top":
        return [
            Port("top",    hx - w / 2,  hy,   "+y"),
            Port("left",  -hx,         -hy + w / 2, "-x"),
            Port("right",  hx,          0,    "+x"),
            Port("bottom", 0,          -hy,   "-y"),
        ]
    elif cap_style == "top" and undercut_style == "top":
        return [
            Port("top",    hx - w / 2,  hy,   "+y"),
            Port("bottom", 0,           -hy,   "-y"),
            Port("right",  hx,           0,    "+x"),
            Port("left",  -hx,           0,    "-x"),
        ]
    else:  # cap_style="side", undercut_style="right"
        return [
            Port("right",  hx,          hy - w / 2,  "+x"),
            Port("left",  -hx,           0,           "-x"),
            Port("top",    0,            hy,          "+y"),
            Port("bottom", 0,           -hy,          "-y"),
        ]


def _make_jj_ports(params: dict, cfg: Config) -> list[Port]:
    s = params.get("lead_width",  cfg.JUNCTION_LEAD_WIDTH)   # square side = lead width
    L = params.get("lead_length", cfg.JUNCTION_LEAD_LENGTH)
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
        params={
            "cap_style": "top",
            "undercut_style": "right",
            "square_x": 2.0,   # µm — full width  (default = cfg.SQUARE_SIZE)
            "square_y": 2.0,   # µm — full height (default = cfg.SQUARE_SIZE)
        },
        port_defs=[],
        description="Bonding square with caps and L-undercut",
    ),
    "manhattan_jj": ComponentType(
        name="Manhattan JJ",
        type_id="manhattan_jj",
        params={
            # Per-instance overrides (None = use global Config value)
            "lead_width":  0.2,   # µm — overrides Config.JUNCTION_LEAD_WIDTH
            "lead_length": 1.5,   # µm — overrides Config.JUNCTION_LEAD_LENGTH
        },
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
        params={"direction": "+x", "length": 6.1, "narrow_end": "start",
                "narrow_width": 0.3},
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
    "merged_group": ComponentType(
        name="Merged Group",
        type_id="merged_group",
        params={},
        port_defs=[],
        description="A merged union of multiple components",
    ),
    "undercut_ring": ComponentType(
        name="Undercut Ring",
        type_id="undercut_ring",
        params={
            # Bounding box as LOCAL offsets from the ring anchor (inst.x, inst.y)
            "bbox_x0": -2.0, "bbox_y0": -2.0,
            "bbox_x1":  2.0, "bbox_y1":  2.0,
            # Which of the 4 sides are present: top/bottom/left/right
            "side_top":    True,
            "side_bottom": True,
            "side_left":   True,
            "side_right":  True,
        },
        port_defs=[],
        description="0.8 µm ring outline around a component (sides deletable)",
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

    def clone(self, offset_x: float = 2.0, offset_y: float = -2.0) -> "ComponentInstance":
        """Return a new ComponentInstance with the same type/params/rotation, offset by (offset_x, offset_y)."""
        new = ComponentInstance(
            self.type_id,
            self.x + offset_x,
            self.y + offset_y,
            params=copy.deepcopy(self.params),
            rotation=self.rotation,
        )
        return new


# ── Merged group ──────────────────────────────────────────────────────────────

class MergedInstance(ComponentInstance):
    """
    A ComponentInstance whose geometry is pre-baked polygon data rather than
    a parametric type.  Created by merge_instances(); rendered by returning
    self._poly_data directly without calling the gdspy builders.

    The anchor (x, y) is set to the centroid of the bounding box of all
    merged polygons so that the Properties panel shows a meaningful position
    and the item can still be moved / rotated like any other component.
    """

    def __init__(self, poly_data: "PolyData", cx: float, cy: float,
                 source_labels: list[str]):
        # Bypass normal ComponentInstance.__init__ param handling — we don't
        # want it looking up "merged_group" in COMPONENT_TYPES for params.
        ComponentInstance._id_counter += 1
        self.inst_id   = ComponentInstance._id_counter
        self.type_id   = "merged_group"
        self.x         = cx
        self.y         = cy
        self.rotation  = 0
        self.params: dict = {"source_labels": ", ".join(source_labels)}
        self.connections: dict = {}
        self._poly_data: PolyData = poly_data

    @property
    def label(self) -> str:  # type: ignore[override]
        n = len(self._poly_data)
        return f"Merged Group #{self.inst_id} ({n} polygons)"

    def get_ports(self, cfg: "Config") -> list:  # type: ignore[override]
        return []

    def clone(self, offset_x: float = 2.0, offset_y: float = -2.0) -> "MergedInstance":
        import copy as _copy
        new = MergedInstance(
            _copy.deepcopy(self._poly_data),
            self.x + offset_x,
            self.y + offset_y,
            [self.params.get("source_labels", "")],
        )
        return new


def merge_instances(instances: list[ComponentInstance], cfg: "Config") -> MergedInstance:
    """
    Render each instance, collect all their polygons, and return a single
    MergedInstance whose anchor sits at the centroid of the combined bounding box.

    The polygon coordinates are kept in world space so that the MergedInstance
    can be moved: render_instance() detects MergedInstance and returns
    _poly_data unchanged (the canvas subtracts the anchor itself).

    Parameters
    ----------
    instances : two or more ComponentInstance objects to merge
    cfg       : Config passed to render_instance for each source instance

    Returns
    -------
    MergedInstance with all source polygons baked in
    """
    if len(instances) < 2:
        raise ValueError("Need at least 2 components to merge")

    all_polys: PolyData = []
    for inst in instances:
        all_polys.extend(render_instance(inst, cfg))

    # Compute centroid of bounding box over all polygon vertices
    all_xs = [x for _, pts in all_polys for x, _ in pts]
    all_ys = [y for _, pts in all_polys for _, y in pts]
    cx = (min(all_xs) + max(all_xs)) / 2
    cy = (min(all_ys) + max(all_ys)) / 2

    labels = [inst.label for inst in instances]

    # Store polygons as LOCAL coords (relative to centroid) so that:
    # - moving just adds (dx, dy) to each point via inst.x / inst.y
    # - rotation pivots cleanly around the anchor in render_instance
    local_polys: PolyData = [
        (layer, [(px - cx, py - cy) for px, py in pts])
        for layer, pts in all_polys
    ]
    return MergedInstance(local_polys, cx, cy, labels)




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


def compute_undercut_ring_polys(
    source_polys: PolyData,
    sides: dict,
    thickness: float,
    layer: int,
    cx: float,
    cy: float,
) -> list[tuple[int, list[tuple[float, float]]]]:
    """
    Compute a geometry-hugging undercut ring by:
      1. Union all source polygons (excluding layer 11 slivers).
      2. Offset outward by `thickness` µm using gdspy.offset().
      3. Subtract the original union to get just the shell.
      4. Optionally clip to only the requested sides (top/bottom/left/right)
         by intersecting with a half-plane mask for each inactive side.
      5. Return as LOCAL-offset PolyData (coords relative to cx, cy).

    Parameters
    ----------
    source_polys : rendered polygons of the source component (world coords)
    sides        : dict of side→bool from the undercut editor
    thickness    : ring thickness in µm (UNDERCUT_RING_THICKNESS)
    layer        : GDS layer for the ring
    cx, cy       : world anchor of the ring instance (centroid of source bbox)
    """
    # Build gdspy polygon list, skipping layer-11 slivers
    gds_polys = [
        gdspy.Polygon(pts)
        for lyr, pts in source_polys
        if lyr != 11 and len(pts) >= 3
    ]
    if not gds_polys:
        return []

    # Union of source geometry
    union = gdspy.boolean(gds_polys, None, "or", precision=1e-4)
    if union is None:
        return []

    # Outward offset
    expanded = gdspy.offset(union, thickness, join="round",
                            tolerance=0.01, precision=1e-4)
    if expanded is None:
        return []

    # Subtract original to get shell
    shell = gdspy.boolean(expanded, union, "not", precision=1e-4)
    if shell is None:
        return []

    # Clip inactive sides: build a large rectangular mask for each active
    # side and intersect, then union the pieces. Simpler: subtract a mask
    # rectangle for each *inactive* side.
    all_xs = [x for _, pts in source_polys if _ != 11 for x, _ in pts]
    all_ys = [y for _, pts in source_polys if _ != 11 for _, y in pts]
    if not all_xs:
        return []
    x0, x1 = min(all_xs) - thickness * 2, max(all_xs) + thickness * 2
    y0, y1 = min(all_ys) - thickness * 2, max(all_ys) + thickness * 2
    src_x0, src_x1 = min(all_xs), max(all_xs)
    src_y0, src_y1 = min(all_ys), max(all_ys)

    inactive_masks = []
    if not sides.get("top", True):
        # Remove everything above src_y1
        inactive_masks.append(gdspy.Rectangle((x0, src_y1), (x1, y1 + thickness)))
    if not sides.get("bottom", True):
        inactive_masks.append(gdspy.Rectangle((x0, y0 - thickness), (x1, src_y0)))
    if not sides.get("left", True):
        inactive_masks.append(gdspy.Rectangle((x0 - thickness, y0), (src_x0, y1)))
    if not sides.get("right", True):
        inactive_masks.append(gdspy.Rectangle((src_x1, y0), (x1 + thickness, y1)))

    if inactive_masks:
        shell = gdspy.boolean(shell, inactive_masks, "not", precision=1e-4)
        if shell is None:
            return []

    # Collect result polygons and convert to LOCAL coords (relative to cx, cy)
    result = []
    polys_arr = shell.polygons if hasattr(shell, "polygons") else [shell.points]
    for pts in polys_arr:
        local_pts = [(float(px) - cx, float(py) - cy) for px, py in pts]
        result.append((layer, local_pts))
    return result


def clip_undercut_ring_by_rect(
    inst: "ComponentInstance",
    rect_um: tuple[float, float, float, float],
) -> bool:
    """
    Boolean-subtract a µm-space rectangle from an undercut_ring instance's
    stored ring_polys.  Modifies inst.params["ring_polys"] in place.

    Parameters
    ----------
    inst     : a ComponentInstance whose type_id == "undercut_ring"
    rect_um  : (x0, y0, x1, y1) in world µm coords, with x0<x1 and y0<y1

    Returns
    -------
    True  if any geometry was actually removed (ring changed)
    False if nothing was clipped (rect didn't overlap any ring polygon)
    """
    baked: list = inst.params.get("ring_polys", [])
    if not baked:
        return False

    x0, y0, x1, y1 = rect_um
    cx, cy = inst.x, inst.y

    # Convert local ring polys → world coords for gdspy boolean
    world_polys = [
        (lyr, [(px + cx, py + cy) for px, py in pts])
        for lyr, pts in baked
    ]

    # Eraser rectangle in world coords
    eraser = gdspy.Rectangle((x0, y0), (x1, y1))

    new_baked: list = []
    changed = False

    for lyr, pts in world_polys:
        if len(pts) < 3:
            continue
        src_poly = gdspy.Polygon(pts, layer=lyr)

        # Check if the eraser overlaps this polygon at all (quick bbox test)
        arr = np.array(pts)
        px0, py0 = arr[:, 0].min(), arr[:, 1].min()
        px1, py1 = arr[:, 0].max(), arr[:, 1].max()
        if x1 < px0 or x0 > px1 or y1 < py0 or y0 > py1:
            # No overlap — keep as-is
            new_baked.append((lyr, [(float(px) - cx, float(py) - cy) for px, py in pts]))
            continue

        remainder = gdspy.boolean(src_poly, eraser, "not", layer=lyr, precision=1e-5)
        changed = True
        if remainder is None:
            continue  # entire polygon was erased

        polys_arr = remainder.polygons if hasattr(remainder, "polygons") else [remainder.points]
        for poly_pts in polys_arr:
            local_pts = [(float(px) - cx, float(py) - cy) for px, py in poly_pts]
            if len(local_pts) >= 3:
                new_baked.append((lyr, local_pts))

    if changed:
        inst.params["ring_polys"] = new_baked
    return changed


def _patched_cfg(cfg: Config, overrides: dict) -> Config:
    """
    Return a shallow copy of *cfg* with selected attributes replaced by values
    from *overrides*.  Only keys that map to a known Config attribute and carry
    a non-None value are applied.

    Mapping from instance-param key → Config attribute name:
        "square_size"  → SQUARE_SIZE
        "wire_width"   → WIRE_WIDTH
        "lead_width"   → JUNCTION_LEAD_WIDTH  (also sets JUNCTION_SQUARE_SIZE)
        "lead_length"  → JUNCTION_LEAD_LENGTH
    """
    new_cfg = copy.copy(cfg)   # shallow copy — all scalars are independent
    mapping = {
        "square_size":  "SQUARE_SIZE",
        "wire_width":   "WIRE_WIDTH",
        "lead_width":   "JUNCTION_LEAD_WIDTH",
        "lead_length":  "JUNCTION_LEAD_LENGTH",
    }
    for param_key, attr in mapping.items():
        val = overrides.get(param_key)
        if val is not None:
            setattr(new_cfg, attr, float(val))
    # JUNCTION_SQUARE_SIZE must equal JUNCTION_LEAD_WIDTH (it's the JJ square side)
    if "lead_width" in overrides and overrides["lead_width"] is not None:
        new_cfg.JUNCTION_SQUARE_SIZE = float(overrides["lead_width"])
    return new_cfg


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
    x, y = inst.x, inst.y
    steps = inst.rotation_steps

    def _apply_rotation(world_pts_list: PolyData) -> PolyData:
        """Rotate world-coord polygons around (x, y) by rotation_steps."""
        if steps == 0:
            return world_pts_list
        rotated: PolyData = []
        for layer, pts in world_pts_list:
            new_pts = []
            for px, py in pts:
                lx, ly = px - x, py - y
                rx, ry = _rotate_pt(lx, ly, steps)
                new_pts.append((rx + x, ry + y))
            rotated.append((layer, new_pts))
        return rotated

    # MergedInstance carries pre-baked LOCAL-space polygons (relative to centroid).
    if isinstance(inst, MergedInstance):
        if not inst._poly_data:
            return []
        # Translate from local → world, then apply rotation
        world = [
            (layer, [(lx + x, ly + y) for lx, ly in pts])
            for layer, pts in inst._poly_data
        ]
        return _apply_rotation(world)

    from primitives import add_rect, add_square, add_taper_pad, smooth_taper
    from undercuts import (add_top_caps, add_side_caps,
                           add_L_undercut_right, add_L_undercut_top)
    from components_lib import (add_square_node, add_manhattan_junction,
                                add_top_branch, add_snake_right_branch,
                                add_turn, add_branch_segment,
                                add_taper_segment)

    parts: list = []

    if inst.type_id == "square_node":
        add_square_node(parts, x, y,
                        inst.params.get("cap_style", "top"),
                        inst.params.get("undercut_style", "right"), cfg,
                        square_x=inst.params.get("square_x"),
                        square_y=inst.params.get("square_y"))

    elif inst.type_id == "manhattan_jj":
        # Apply per-instance lead_width / lead_length overrides
        local_cfg = _patched_cfg(cfg, inst.params)
        add_manhattan_junction(parts, x, y, local_cfg)

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
                          inst.params.get("direction",    "+x"),
                          inst.params.get("length",       6.1),
                          inst.params.get("narrow_end",   "start"), cfg,
                          narrow_width=inst.params.get("narrow_width", None))

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

    elif inst.type_id == "undercut_ring":
        # ring_polys are stored as LOCAL offsets from anchor (inst.x, inst.y).
        # Translate to world, then apply rotation around the same anchor.
        baked: list = inst.params.get("ring_polys", [])
        if not baked:
            return []
        world = [
            (lyr, [(px + x, py + y) for px, py in pts])
            for lyr, pts in baked
        ]
        return _apply_rotation(world)

    raw = _collect_polygons(parts)
    return _apply_rotation(raw)


# ── Workspace serialization ───────────────────────────────────────────────────

WORKSPACE_VERSION = 1


def instance_to_dict(inst: ComponentInstance) -> dict:
    """Serialise one ComponentInstance (or MergedInstance) to a plain dict."""
    d: dict = {
        "version":     WORKSPACE_VERSION,
        "inst_id":     inst.inst_id,
        "type_id":     inst.type_id,
        "x":           inst.x,
        "y":           inst.y,
        "rotation":    inst.rotation,
        "params":      copy.deepcopy(inst.params),
        "connections": {k: list(v) for k, v in inst.connections.items()},
    }
    if isinstance(inst, MergedInstance):
        d["_merged"]    = True
        # _poly_data is list[(layer, [(x,y)…])] — fully JSON-serialisable
        d["_poly_data"] = [
            [layer, [[px, py] for px, py in pts]]
            for layer, pts in inst._poly_data
        ]
    return d


def instance_from_dict(d: dict) -> ComponentInstance:
    """Deserialise a dict produced by instance_to_dict()."""
    if d.get("_merged"):
        poly_data: PolyData = [
            (int(layer), [(float(px), float(py)) for px, py in pts])
            for layer, pts in d["_poly_data"]
        ]
        inst = MergedInstance(
            poly_data,
            cx=d["x"],
            cy=d["y"],
            source_labels=[d["params"].get("source_labels", "")],
        )
    else:
        inst = ComponentInstance(
            type_id=d["type_id"],
            x=d["x"],
            y=d["y"],
            params=d.get("params"),
            rotation=d.get("rotation", 0),
        )

    # Restore the original inst_id so wire connections remain valid
    inst.inst_id = d["inst_id"]

    # Restore connections (stored as [other_id, other_port])
    inst.connections = {
        port: (int(other_id), other_port)
        for port, (other_id, other_port) in d.get("connections", {}).items()
    }
    return inst


def save_workspace(instances: list[ComponentInstance], filepath: str) -> None:
    """Write all instances to a JSON workspace file."""
    import json
    payload = {
        "version":    WORKSPACE_VERSION,
        "id_counter": ComponentInstance._id_counter,
        "instances":  [instance_to_dict(i) for i in instances],
    }
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def load_workspace(filepath: str) -> list[ComponentInstance]:
    """
    Read a JSON workspace file and return a list of ComponentInstances.
    The global _id_counter is advanced past all loaded ids so new placements
    never collide with restored ones.
    """
    import json
    with open(filepath, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    instances = [instance_from_dict(d) for d in payload.get("instances", [])]

    # Advance id counter so future instances don't reuse any loaded id
    if instances:
        max_id = max(i.inst_id for i in instances)
        if max_id >= ComponentInstance._id_counter:
            ComponentInstance._id_counter = max_id  # next __init__ will +1

    return instances


def export_gds_script(instances: list[ComponentInstance], cfg: Config,
                      filepath: str) -> None:
    """
    Render every instance and write a self-contained Python script that,
    when run, reproduces the exact same GDS file using only gdspy.

    The generated script embeds all polygon coordinates and layer numbers
    directly — no dependency on this codebase whatsoever.

    Parameters
    ----------
    instances : all ComponentInstances currently on the canvas
    cfg       : Config (needed by render_instance)
    filepath  : destination .py path chosen by the user
    """
    from collections import defaultdict

    # ── 1. Render all geometry ────────────────────────────────────────────────
    layer_polys: dict[int, list[list[tuple[float, float]]]] = defaultdict(list)
    for inst in instances:
        for layer, pts in render_instance(inst, cfg):
            if len(pts) >= 3:
                layer_polys[layer].append(pts)

    if not layer_polys:
        raise ValueError("Nothing to export — canvas is empty.")

    # ── 2. Derive a default output GDS name from the script name ─────────────
    import os
    script_stem = os.path.splitext(os.path.basename(filepath))[0]
    default_gds = f"{script_stem}.gds"

    # ── 3. Build script text ──────────────────────────────────────────────────
    def _fmt_pts(pts: list[tuple[float, float]]) -> str:
        inner = ",".join(f"({x:.6f},{y:.6f})" for x, y in pts)
        return f"[{inner}]"

    lines: list[str] = []
    a = lines.append

    a('"""')
    a(f'Auto-generated GDS script — reproduces the layout exported from the GDS Layout Editor.')
    a(f'Output file: {default_gds}')
    a('Dependency : gdspy  (pip install gdspy)')
    a('Run        : python ' + os.path.basename(filepath))
    a('"""')
    a('')
    a('import os')
    a('import gdspy')
    a('')
    a('# ── Layer name reference (informational only) ────────────────────────────')
    a('LAYER_NAMES = {')
    for layer, name in sorted(LAYER_NAMES.items()):
        a(f'    {layer}: "{name}",')
    a('}')
    a('')
    a('# ── Polygon data: {layer: [[(x, y), ...], ...]} ─────────────────────────')
    a('# Each entry is one polygon on the given GDS layer.')
    a('POLYGONS = {')
    for layer in sorted(layer_polys.keys()):
        pts_list = layer_polys[layer]
        name = LAYER_NAMES.get(layer, f"L{layer}")
        a(f'    {layer}: [  # {name} — {len(pts_list)} polygon(s)')
        for pts in pts_list:
            a(f'        {_fmt_pts(pts)},')
        a('    ],')
    a('}')
    a('')
    a('')
    a('def write_gds(output_path: str = None) -> str:')
    a('    """Write the embedded layout to a GDS file and return the path."""')
    a(f'    if output_path is None:')
    a(f'        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),')
    a(f'                                   "{default_gds}")')
    a('')
    a('    gdspy.current_library = gdspy.GdsLibrary()')
    a('    cell = gdspy.Cell("LAYOUT")')
    a('')
    a('    for layer, polys in POLYGONS.items():')
    a('        for pts in polys:')
    a('            cell.add(gdspy.Polygon(pts, layer=layer))')
    a('')
    a('    gdspy.write_gds(output_path)')
    a('    print(f"GDS written → {output_path}")')
    a('    return output_path')
    a('')
    a('')
    a('if __name__ == "__main__":')
    a('    import sys')
    a('    out = sys.argv[1] if len(sys.argv) > 1 else None')
    a('    write_gds(out)')

    # ── 4. Write file ─────────────────────────────────────────────────────────
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


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