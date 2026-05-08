"""
core/cell_library.py — Parametric cell library for GDS Canvas Designer.

Design rules (matching the rest of this codebase):
  - Zero Qt dependency. Pure data-in / component-list-out.
  - All lengths in DBU (nm). Public APIs accept µm and convert internally.
  - Each cell builder returns a CellResult: a list of GDSComponent objects
    plus a suggested ComponentGroup name.  The caller is responsible for
    issuing AddComponent / GroupComponents commands via the undo stack.
  - Layer numbers mirror the reference layout exactly — defined once in
    LAYERS dict and exported as module-level constants for backwards compat:
        1  Branch          (L1)
        2  Undercut ring   (L2)
        4  Cap 1           (L4)
        5  Biysk junction  (L5)
        6  Cap 2           (L6)
       10  JJ square       (L10)
       11  Narrow end      (L11)
  - Coordinate convention: Y-axis is NOT flipped here.
    The exporter in exporter.py negates Y when writing to GDS — identical
    to how every other component in this codebase is handled.

Cells implemented
-----------------
  byisk_jj       — bonding square with caps + L-undercut (from add_byisk_jj)
  manhattan_jj      — Manhattan-style Josephson junction stack (from add_manhattan_junction)
  taper_lead        — linear taper wedge L1 + L11 narrow-tip slice (from add_taper_segment)
  taper_pad         — linear taper + flat overlap pad, both L1 (from add_taper_pad)
  branch_segment    — straight uniform-width branch rect on L1 (from add_branch_segment)
  turn              — 90° arc turn, configurable direction+handedness, L1 (from add_turn)
  wire              — plain rectangle on any layer, configurable width/length/layer

Adding new cells
----------------
  1. Write a `build_<name>(origin, **kwargs) -> CellResult` function.
  2. Register it in CELL_CATALOGUE at the bottom of this file.
  3. That is all — the palette picks it up automatically.

Fixes applied (v3)
------------------
  - Layer constants defined once here (removed duplication with component_model.py).
  - _poly() now correctly closes the polygon when last point ≠ first.
  - Port offsets in build_manhattan_jj() computed directly in component-origin
    space; no more redundant round-trip through the cell-centre frame.
  - place_cell() validates param keys against catalogue defaults and raises a
    clear ValueError for unknown keys instead of letting the builder fail.
  - build_byisk_jj() port positions computed geometrically from face centres
    instead of four hand-coded combinatorial branches — extensible to new styles.
  - FIX: build_byisk_jj() caps now emit TWO layers (CAP1 inner strip +
    CAP2 outer strip) matching add_top_caps/add_side_caps in undercuts.py.
    Previously only CAP1 was emitted; CAP2 was silently missing.
  - FIX: build_byisk_jj() L-undercut is now drawn OUTSIDE the body body on
    LAYER_CAP1 (L outline) + LAYER_CAP2 (interior fill), matching
    add_L_undercut_right / add_L_undercut_top in undercuts.py exactly.
    Previously the undercut was drawn inside the body on LAYER_UNDERCUT_RING
    (L2) which is the wrong layer and the wrong position entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Callable

from core.model import (
    GDSComponent, ComponentKind, ComponentGroup,
    Point, Port, PortSide,
    um_to_dbu, dbu_to_um,
)


# ── Layer constants (single source of truth for this project) ─────────────────
# Import from here wherever layer numbers are needed — do NOT redeclare in other
# modules (component_model.py, canvas_scene.py, etc.).

LAYERS: dict[str, int] = {
    "BRANCH":         1,
    "UNDERCUT_RING":  2,
    "CAP1":           4,
    "BIYSK_JUNCTION": 5,
    "CAP2":           6,
    "JJ":             10,
    "NARROW_END":     11,
}

# Backwards-compatible module-level aliases (existing code uses LAYER_* names)
LAYER_BRANCH          = LAYERS["BRANCH"]
LAYER_UNDERCUT_RING   = LAYERS["UNDERCUT_RING"]
LAYER_CAP1            = LAYERS["CAP1"]
LAYER_BIYSK_JUNCTION  = LAYERS["BIYSK_JUNCTION"]
LAYER_CAP2            = LAYERS["CAP2"]
LAYER_JJ              = LAYERS["JJ"]
LAYER_NARROW_END      = LAYERS["NARROW_END"]


# ── Cell result container ─────────────────────────────────────────────────────

@dataclass
class CellResult:
    """
    What a cell builder returns.

    components  — ordered list of GDSComponent objects.  The first element is
                  considered the "anchor" (origin / primary body).
    group_name  — suggested name for the ComponentGroup that will wrap them.
    description — one-line human description shown in the palette tooltip.
    """
    components:  List[GDSComponent]
    group_name:  str
    description: str = ""


# ── Cell catalogue entry ──────────────────────────────────────────────────────

@dataclass
class CellDef:
    """Static metadata + factory reference for one cell type."""
    cell_id:     str
    name:        str
    description: str
    category:    str                        # palette section header
    defaults:    dict                       # default keyword arguments
    builder:     "Callable"                 # build_*(origin, **kwargs) -> CellResult


# ── Geometry helpers (µm → DBU, no Qt) ───────────────────────────────────────

def _rect(
    origin: Point,
    x0_um: float, y0_um: float,
    x1_um: float, y1_um: float,
    layer: int,
) -> GDSComponent:
    """
    Return a RECTANGLE GDSComponent whose corners are at
    (origin + (x0,y0)) and (origin + (x1,y1)) in µm, converted to DBU.

    width  = |x1 − x0| in DBU
    height = |y1 − y0| in DBU  (always positive; sign encodes orientation only)
    origin of the rect is its top-left corner: (min_x, min_y) in DBU
    """
    ax = origin.x + um_to_dbu(min(x0_um, x1_um))
    ay = origin.y + um_to_dbu(min(y0_um, y1_um))
    w  = um_to_dbu(abs(x1_um - x0_um))
    h  = um_to_dbu(abs(y1_um - y0_um))
    # Sub-components in a cell carry NO ports by default — only the designated
    # anchor gets explicit ports via _assign_ports().  Standalone shapes placed
    # directly on the canvas go through AddComponent.execute() which calls
    # build_default_ports() when ports is empty; that path is unaffected.
    return GDSComponent(
        kind=ComponentKind.RECTANGLE,
        layer=layer,
        origin=Point(ax, ay),
        width=w,
        height=h,
    )


def _poly(
    origin: Point,
    pts_um: List[tuple[float, float]],
    layer: int,
) -> GDSComponent:
    """
    Return a POLYGON GDSComponent from a list of (x, y) µm offsets relative
    to *origin*.  Automatically closes the polygon if the last point ≠ first.
    """
    dbu_pts = [
        Point(origin.x + um_to_dbu(px), origin.y + um_to_dbu(py))
        for px, py in pts_um
    ]
    # FIX: actually close the polygon when required (was documented but never done)
    if dbu_pts and dbu_pts[-1] != dbu_pts[0]:
        dbu_pts.append(dbu_pts[0])

    return GDSComponent(
        kind=ComponentKind.POLYGON,
        layer=layer,
        origin=dbu_pts[0],
        points=dbu_pts,
    )


# ── Port helpers ──────────────────────────────────────────────────────────────

def _assign_ports(comp: GDSComponent, ports: List[Port]) -> None:
    """Replace auto-generated ports with explicitly specified ones."""
    comp.ports = ports


def _port(name: str, ox_um: float, oy_um: float, side: PortSide) -> Port:
    """Port at (ox_um, oy_um) µm relative to the component's *own* origin."""
    return Port(
        name=name,
        offset=Point(um_to_dbu(ox_um), um_to_dbu(oy_um)),
        side=side,
    )


def _face_ports(
    comp: GDSComponent,
    wire_width_um: float,
    free_faces: tuple[str, ...],
) -> List[Port]:
    """
    Compute edge-centre ports geometrically from *comp*'s bounding box.

    Only faces listed in *free_faces* ("N", "S", "E", "W") get a port.
    The port is offset inward by wire_width_um/2 on that face's transverse
    axis so a wire lead of that width fits flush.

    All offsets are in µm relative to comp.origin (the bbox min-corner).

    This replaces the old combinatorial if/elif ladder in build_byisk_jj
    and scales to any number of style combinations without extra code.
    """
    bb     = comp.bbox
    ox, oy = comp.origin.x, comp.origin.y
    w_dbu  = um_to_dbu(wire_width_um)

    # bbox corners in DBU relative to comp.origin
    bx0 = bb.x_min - ox
    by0 = bb.y_min - oy
    bx1 = bb.x_max - ox
    by1 = bb.y_max - oy
    # centre coords relative to comp.origin
    cx  = (bx0 + bx1) // 2
    cy  = (by0 + by1) // 2

    _side_map = {
        "N": (cx,          by0,          PortSide.NORTH),  # top edge
        "S": (cx,          by1,          PortSide.SOUTH),  # bottom edge
        "W": (bx0,         cy,           PortSide.WEST),
        "E": (bx1,         cy,           PortSide.EAST),
    }

    ports: List[Port] = []
    for face in free_faces:
        px_dbu, py_dbu, side = _side_map[face]
        ports.append(Port(
            name=face.lower(),
            offset=Point(px_dbu, py_dbu),
            side=side,
        ))
    return ports


# ═════════════════════════════════════════════════════════════════════════════
# Cell: Square Node  (→ add_byisk_jj in components_lib.py)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors cfg defaults from the reference codebase
_SQ_DEFAULTS = dict(
    square_x       = 2.0,    # full width  (µm)
    square_y       = 2.0,    # full height (µm)
    cap_style      = "top",  # "top" | "side"
    undercut_style = "right", # "right" | "top"
    # Cap geometry (µm) — matches reference undercuts.py defaults
    cap_width      = 0.3,    # CAP1 strip thickness (= CAP_H in reference)
    cap_length     = 0.8,    # CAP2 bar thickness   (= CAP2_H in reference)
    wire_width     = 0.3,    # wire lead width (used for L-undercut extent calc)
)


def build_byisk_jj(
    origin: Point,
    square_x:       float = _SQ_DEFAULTS["square_x"],
    square_y:       float = _SQ_DEFAULTS["square_y"],
    cap_style:      str   = _SQ_DEFAULTS["cap_style"],
    undercut_style: str   = _SQ_DEFAULTS["undercut_style"],
    cap_width:      float = _SQ_DEFAULTS["cap_width"],
    cap_length:     float = _SQ_DEFAULTS["cap_length"],
    wire_width:     float = _SQ_DEFAULTS["wire_width"],
) -> CellResult:
    """
    Bonding square on LAYER_BIYSK_JUNCTION (L5) with two-layer cap strips
    (LAYER_CAP1 inner + LAYER_CAP2 outer) and an L-shaped undercut drawn
    outside the body (LAYER_CAP1 outline + LAYER_CAP2 fill).

    Mirrors add_byisk_jj() / add_top_caps() / add_side_caps() /
    add_L_undercut_right() / add_L_undercut_top() from components_lib.py
    exactly.  Origin is the **centre** of the square body (cx, cy convention).

    cap_style      : "top"  — cap extends above (+y) the body only  (add_top_caps)
                     "side" — cap extends to the right (+x) only    (add_side_caps)
    undercut_style : "right" — L opens rightward, anchored at body bottom-right
                     "top"   — L opens upward,    anchored at body top-left

    cap_width  = CAP_H  in undercuts.py  (thickness of the inner CAP1 strip)
    cap_length = CAP2_H in undercuts.py  (thickness of the outer CAP2 strip)
    wire_width = WIRE_WIDTH              (controls the L arm extent)

    Ports match _make_square_ports() in component_model.py exactly.
    All port offsets are relative to body.origin (bbox min-corner = cx−hx, cy−hy).
    """
    hx = square_x / 2.0
    hy = square_y / 2.0
    cw = cap_width    # CAP1 strip thickness  (= cfg.CAP_H)
    cl = cap_length   # CAP2 strip thickness  (= cfg.CAP2_H)
    ww = wire_width   # wire width            (= cfg.WIRE_WIDTH)

    components: List[GDSComponent] = []

    # ── 1. Main body (L5) ─────────────────────────────────────────────────────
    body = _rect(origin, -hx, -hy, hx, hy, LAYER_BIYSK_JUNCTION)
    components.append(body)

    # ── 2. Cap strips — TWO layers, ONE side only ─────────────────────────────
    #
    # add_top_caps  → only ABOVE the body  (not above+below)
    # add_side_caps → only to the RIGHT    (not left+right)
    #
    # Each emits:
    #   CAP1 inner strip flush against the body edge  (thickness = cw = CAP_H)
    #   CAP2 outer strip stacked beyond CAP1          (thickness = cl = CAP2_H)
    # ─────────────────────────────────────────────────────────────────────────
    if cap_style == "top":
        # add_top_caps: above only (top = cy+hy)
        components.append(_rect(origin, -hx, hy,      hx, hy + cw,      LAYER_CAP1))
        components.append(_rect(origin, -hx, hy + cw, hx, hy + cw + cl, LAYER_CAP2))
    else:  # "side"
        # add_side_caps: right only (right = cx+hx)
        components.append(_rect(origin, hx,      -hy, hx + cw,      hy, LAYER_CAP1))
        components.append(_rect(origin, hx + cw, -hy, hx + cw + cl, hy, LAYER_CAP2))

    # ── 3. L-undercut — literal port of add_L_undercut_right / _top ──────────
    #
    # Reference uses gdspy.boolean(box, [c1_v, c1_h], "not") to cut the L arms
    # out of the bounding box.  We decompose this into three non-overlapping
    # rectangles instead (same result, no boolean needed).
    #
    # add_L_undercut_right  (undercut_style="right"):
    #   Anchors at bottom-right corner of the body: (right=cx+hx, bottom=cy-hy)
    #   l_vert = hy*2 - WIRE_WIDTH   ← full square height minus wire width
    #   c1_v : vertical arm   — (right, bottom) → (right+CAP_H, bottom+l_vert)       CAP1
    #   c1_h : horizontal arm — (right, bottom+l_vert-CAP_H) → (right+L_HORZ, bottom+l_vert)  CAP1
    #   box  : bounding box   — (right, bottom) → (right+L_HORZ, bottom+l_vert)      CAP2
    #   c2   : box NOT (c1_v, c1_h) → right of c1_v, below c1_h                      CAP2
    #
    # add_L_undercut_top  (undercut_style="top"):
    #   Anchors at top-left corner of the body: (left=cx-hx, top=cy+hy)
    #   l_vert = hx*2 - WIRE_WIDTH   ← full square width minus wire width
    #   c1_v : horizontal arm — (left, top) → (left+l_vert, top+CAP_H)               CAP1
    #   c1_h : vertical arm   — (left+l_vert-CAP_H, top) → (left+l_vert, top+L_HORZ) CAP1
    #   box  : bounding box   — (left, top) → (left+l_vert, top+L_HORZ)              CAP2
    #   c2   : box NOT (c1_v, c1_h) → below c1_v, left of c1_h                       CAP2
    #
    # All coords below are centre-relative (cell frame), _rect adds origin.
    # ─────────────────────────────────────────────────────────────────────────
    l_reach = cw + cl   # L_HORZ = CAP_H + CAP2_H

    if undercut_style == "right":
        l_vert = hy * 2 - ww          # = square_y - WIRE_WIDTH
        right  =  hx                  # right edge of body
        bottom = -hy                  # bottom edge of body

        # c1_v — vertical arm (left strip of L)
        components.append(_rect(
            origin,
            right,       bottom,
            right + cw,  bottom + l_vert,
            LAYER_CAP1,
        ))
        # c1_h — horizontal arm (top strip of L)
        components.append(_rect(
            origin,
            right,          bottom + l_vert - cw,
            right + l_reach, bottom + l_vert,
            LAYER_CAP1,
        ))
        # c2_fill — interior: right of c1_v AND below c1_h
        components.append(_rect(
            origin,
            right + cw,     bottom,
            right + l_reach, bottom + l_vert - cw,
            LAYER_CAP2,
        ))

    else:  # "top"
        l_vert = hx * 2 - ww          # = square_x - WIRE_WIDTH
        left   = -hx                  # left edge of body
        top    =  hy                  # top edge of body

        # c1_v — horizontal arm (bottom strip of L, confusingly named c1_v in reference)
        components.append(_rect(
            origin,
            left,          top,
            left + l_vert, top + cw,
            LAYER_CAP1,
        ))
        # c1_h — vertical arm (right strip of L)
        components.append(_rect(
            origin,
            left + l_vert - cw, top,
            left + l_vert,       top + l_reach,
            LAYER_CAP1,
        ))
        # c2_fill — interior: above c1_v AND left of c1_h
        components.append(_rect(
            origin,
            left,               top + cw,
            left + l_vert - cw, top + l_reach,
            LAYER_CAP2,
        ))

    # ── 4. Ports — match _make_square_ports() in component_model.py ───────────
    #
    # All offsets are body-local, i.e. relative to body.origin = (cx−hx, cy−hy).
    # Centre of body in body-local coords = (hx, hy).
    # Wire lead flush with one corner → centreline at (h − w/2) on transverse axis.
    #
    # PortSide convention: Qt scene Y increases downward (origin = bbox min-corner),
    # so y=0 is the TOP of the screen (NORTH) and y=2*hy is the BOTTOM (SOUTH).
    # EAST/WEST are unchanged — X is rightward in both conventions.
    w = wire_width

    def _p(name: str, lx_um: float, ly_um: float, side: PortSide) -> Port:
        return Port(name, Point(um_to_dbu(lx_um), um_to_dbu(ly_um)), side)

    if cap_style == "top" and undercut_style == "right":
        ports = [
            _p("right",  2*hx,       2*hy - w/2, PortSide.EAST),
            _p("bottom", w/2,        0,           PortSide.NORTH),   # y=0 → top of screen
            _p("top",    hx,         2*hy,        PortSide.SOUTH),   # y=2hy → bottom of screen
            _p("left",   0,          hy,          PortSide.WEST),
        ]
    elif cap_style == "side" and undercut_style == "top":
        ports = [
            _p("top",    2*hx - w/2, 2*hy,       PortSide.SOUTH),   # y=2hy → bottom of screen
            _p("left",   0,          w/2,         PortSide.WEST),
            _p("right",  2*hx,       hy,          PortSide.EAST),
            _p("bottom", hx,         0,           PortSide.NORTH),   # y=0 → top of screen
        ]
    elif cap_style == "top" and undercut_style == "top":
        ports = [
            _p("top",    2*hx - w/2, 2*hy,       PortSide.SOUTH),
            _p("bottom", hx,         0,           PortSide.NORTH),
            _p("right",  2*hx,       hy,          PortSide.EAST),
            _p("left",   0,          hy,          PortSide.WEST),
        ]
    else:  # cap_style="side", undercut_style="right"
        ports = [
            _p("right",  2*hx,       2*hy - w/2, PortSide.EAST),
            _p("left",   0,          hy,          PortSide.WEST),
            _p("top",    hx,         2*hy,        PortSide.SOUTH),
            _p("bottom", hx,         0,           PortSide.NORTH),
        ]

    _assign_ports(body, ports)

    # Sub-components carry no ports — only the anchor body (index 0) does.
    for comp in components[1:]:
        comp._no_auto_ports = True

    return CellResult(
        components=components,
        group_name=f"ByiskJJ ({square_x:.1f}×{square_y:.1f}µm)",
        description=(
            f"Byisk JJ bonding square  {square_x}×{square_y} µm  "
            f"cap={cap_style}  undercut={undercut_style}"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell: Manhattan Josephson Junction  (→ add_manhattan_junction)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors cfg defaults from the reference codebase
_JJ_DEFAULTS = dict(
    lead_width  = 0.2,   # JUNCTION_LEAD_WIDTH  (also = JUNCTION_SQUARE_SIZE)
    lead_length = 1.5,   # JUNCTION_LEAD_LENGTH
    ext2_width  = 0.1,   # CAP1 strip width   (EXT2_W in reference)
    ext3_width  = 0.8,   # CAP2 bar width     (EXT3_W in reference)
)


def build_manhattan_jj(
    origin: Point,
    lead_width:  float = _JJ_DEFAULTS["lead_width"],
    lead_length: float = _JJ_DEFAULTS["lead_length"],
    ext2_width:  float = _JJ_DEFAULTS["ext2_width"],
    ext3_width:  float = _JJ_DEFAULTS["ext3_width"],
) -> CellResult:
    """
    Manhattan-style Josephson junction stack.

    Origin is the entry point of the horizontal lead (left end), matching
    the reference codebase convention where (x0, y0) is passed to
    add_manhattan_junction.

    Stack geometry (all relative to *origin*):

      ── Horizontal lead (L5) ──►  [JJ square (L10)]
                                        │   right extensions: L5 → L4 strip → L6 bar
                                        │   top  extensions:  L5 → L4 strip → L6 bar
                                        ▼
                                   downward lead (L5)

    Layers used
    -----------
      L5  LAYER_BIYSK_JUNCTION  — horizontal lead, JJ body extensions, down lead
      L10 LAYER_JJ              — JJ square
      L4  LAYER_CAP1            — thin cap strip  (EXT2 in reference)
      L6  LAYER_CAP2            — wide cap bar    (EXT3 in reference)

    Ports (all offsets in µm relative to horiz_lead.origin = (0, -w/2))
    --------------------------------
      lead_in   — left end of horizontal lead (entry, WEST)
      down_out  — bottom of downward lead     (SOUTH)
      right_out — right end of CAP2 bar       (EAST)
      top_out   — top of CAP2 bar             (NORTH)
    """
    w  = lead_width
    L  = lead_length
    s  = lead_width   # JJ square side = lead_width (JUNCTION_SQUARE_SIZE == lead_width)
    e2 = ext2_width
    e3 = ext3_width

    components: List[GDSComponent] = []

    # ── 1. Horizontal lead (L5): x ∈ [0, L], y ∈ [-w/2, w/2] ────────────────
    horiz_lead = _rect(origin, 0, -w / 2, L, w / 2, LAYER_BIYSK_JUNCTION)
    components.append(horiz_lead)

    # ── 2. JJ square (L10): x ∈ [L, L+s], y ∈ [-s/2, s/2] ──────────────────
    x_sq = L
    jj_sq = _rect(origin, x_sq, -s / 2, x_sq + s, s / 2, LAYER_JJ)
    components.append(jj_sq)

    # ── 3. Right extensions (→ +x from JJ square right edge) ─────────────────
    # 3a. L5 continuation: same y-width as lead, one square-width long
    components.append(_rect(
        origin, x_sq + s, -w / 2, x_sq + 2 * s, w / 2,
        LAYER_BIYSK_JUNCTION,
    ))
    # 3b. L4 (CAP1) thin strip: full JJ-square height, e2 wide
    components.append(_rect(
        origin, x_sq + 2 * s, -s / 2, x_sq + 2 * s + e2, s / 2,
        LAYER_CAP1,
    ))
    # 3c. L6 (CAP2) wide bar: full JJ-square height, e3 wide
    components.append(_rect(
        origin, x_sq + 2 * s + e2, -s / 2, x_sq + 2 * s + e2 + e3, s / 2,
        LAYER_CAP2,
    ))

    # ── 4. Top extensions (↑ +y from JJ square top edge) ─────────────────────
    y_top_base = s / 2   # top edge of JJ square (cell-frame µm)

    # 4a. L5 continuation: s wide, s tall
    components.append(_rect(
        origin, x_sq, y_top_base, x_sq + s, y_top_base + s,
        LAYER_BIYSK_JUNCTION,
    ))
    # 4b. L4 (CAP1) thin strip: s wide, e2 tall
    components.append(_rect(
        origin, x_sq, y_top_base + s, x_sq + s, y_top_base + s + e2,
        LAYER_CAP1,
    ))
    # 4c. L6 (CAP2) wide bar: s wide, e3 tall
    components.append(_rect(
        origin, x_sq, y_top_base + s + e2, x_sq + s, y_top_base + s + e2 + e3,
        LAYER_CAP2,
    ))

    # ── 5. Downward lead (L5): x ∈ [L, L+s], y ∈ [-s/2-L, -s/2] ────────────
    y_down_top    = -s / 2
    y_down_bottom = -s / 2 - L

    components.append(_rect(
        origin, x_sq, y_down_bottom, x_sq + s, y_down_top,
        LAYER_BIYSK_JUNCTION,
    ))

    # ── 6. Ports on the horizontal lead ──────────────────────────────────────
    #
    # horiz_lead.origin is at (origin.x, origin.y - w/2) in world DBU,
    # so port offsets relative to horiz_lead.origin are:
    #   port_offset = (cell_frame_x, cell_frame_y + w/2)

    oy = w / 2   # correction: cell_frame_y → body_offset_y = cf_y + w/2

    lead_ports = [
        _port("lead_in",   0.0,                           oy,                                  PortSide.WEST),
        _port("down_out",  x_sq + s / 2,                  y_down_bottom + oy,                  PortSide.SOUTH),
        _port("right_out", x_sq + 2 * s + e2 + e3,        oy,                                  PortSide.EAST),
        _port("top_out",   x_sq + s / 2,                  y_top_base + s + e2 + e3 + oy,       PortSide.NORTH),
    ]
    _assign_ports(horiz_lead, lead_ports)

    # Mark all sub-components except the anchor (horiz_lead, index 0) as port-free.
    for comp in components[1:]:
        comp._no_auto_ports = True

    return CellResult(
        components=components,
        group_name=f"ManhattanJJ (w={lead_width}µm L={lead_length}µm)",
        description=(
            f"Manhattan Josephson junction  lead {lead_width}×{lead_length} µm  "
            f"JJ square {lead_width}µm  CAP1 {ext2_width}µm  CAP2 {ext3_width}µm"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell: Tapered Lead  (→ add_taper_segment in components_lib.py)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors taper_segment ComponentType in component_model.py
_TAPER_SEG_DEFAULTS = dict(
    direction       = "+x",    # "+x" | "-x" | "+y" | "-y"
    length          = 6.1,     # taper length (µm)
    narrow_end      = "start", # "start" — narrow at entry, "end" — narrow at exit
    narrow_width    = 0.3,     # narrow-end width (µm)  → cfg.WIRE_WIDTH
    taper_width     = 2.0,     # wide-end width  (µm)  → cfg.TAPER_WIDTH
    narrow_undercut = False,   # emit tapered-lead undercut ring on L2 at the narrow end
)


def build_taper_segment(
    origin: Point,
    direction:       str   = _TAPER_SEG_DEFAULTS["direction"],
    length:          float = _TAPER_SEG_DEFAULTS["length"],
    narrow_end:      str   = _TAPER_SEG_DEFAULTS["narrow_end"],
    narrow_width:    float = _TAPER_SEG_DEFAULTS["narrow_width"],
    taper_width:     float = _TAPER_SEG_DEFAULTS["taper_width"],
    narrow_undercut: bool  = _TAPER_SEG_DEFAULTS["narrow_undercut"],
) -> CellResult:
    """
    Linear taper wedge on LAYER_BRANCH (L1) with a 1 µm narrow-tip slice
    re-assigned to LAYER_NARROW_END (L11).

    Mirrors add_taper_segment() from components_lib.py and the taper_segment
    ComponentType from component_model.py.

    The taper is a trapezoid: one end is narrow_width wide, the other is
    taper_width wide.  narrow_end controls which end is which:
      "start" — narrow at origin, widens toward exit  (w0=narrow_width, w1=taper_width)
      "end"   — wide  at origin, narrows toward exit  (w0=taper_width,  w1=narrow_width)

    The 1 µm narrow-tip slice is a thin rectangle on LAYER_NARROW_END (L11)
    that trims the very end of the taper, matching clip_narrow_end /
    clip_start_narrow_end in the reference code.

    Origin is the centreline of the ENTRY end of the taper (matching the
    (x, y) start convention used everywhere in components_lib.py).

    Ports
    -----
      entry — face the wire comes in from (opposite to direction of travel)
      exit  — face the wire exits through (in direction of travel)
    Port names also reflect narrow_end:
      "narrow" / "wide" instead of "entry" / "exit" when narrow_end is clear.
    """
    if narrow_end not in ("start", "end"):
        raise ValueError(f"narrow_end must be 'start' or 'end'; got {narrow_end!r}")

    nw = narrow_width   # narrow-end half-width
    tw = taper_width    # wide-end half-width
    L  = length
    CLIP = 1.0          # narrow-tip slice thickness (µm) — matches reference

    # Determine which end is narrow based on narrow_end
    if narrow_end == "start":
        entry_hw, exit_hw = nw / 2, tw / 2
        entry_label, exit_label = "narrow", "wide"
    else:
        entry_hw, exit_hw = tw / 2, nw / 2
        entry_label, exit_label = "wide", "narrow"

    # Build polygon vertices for a trapezoid travelling in `direction`.
    # All coords are µm offsets from origin (entry centreline).
    # The narrow-tip clip rectangle is on the narrow end — 1 µm thick.
    #
    # Direction map: travel axis, transverse axis, signs
    _dir_map = {
        "+x": dict(tx=1,  ty=0,  px=0,  py=1),
        "-x": dict(tx=-1, ty=0,  px=0,  py=1),
        "+y": dict(tx=0,  ty=1,  px=1,  py=0),
        "-y": dict(tx=0,  ty=-1, px=1,  py=0),
    }
    d = _dir_map[direction]
    tx, ty = d["tx"], d["ty"]   # unit vector along travel
    px, py = d["px"], d["py"]   # unit vector along transverse (always positive)

    # Entry corners (at travel=0):  ±entry_hw transverse
    # Exit  corners (at travel=L):  ±exit_hw  transverse
    def _pt(travel: float, hw: float, sign: int) -> tuple[float, float]:
        return (tx * travel + px * sign * hw,
                ty * travel + py * sign * hw)

    # Linearly interpolated half-width at the clip boundary (1 µm from narrow end).
    # The L1 body is trimmed to exclude the narrow tip so L1 and L11 never overlap —
    # they share the clip boundary edge but have no duplicate region.
    if narrow_end == "start":
        hw_at_clip = entry_hw + (exit_hw - entry_hw) * (CLIP / L) if L > 0 else entry_hw
        # L1 body: clip boundary → wide exit
        trap_pts = [
            _pt(CLIP, hw_at_clip, -1),
            _pt(CLIP, hw_at_clip, +1),
            _pt(L,    exit_hw,    +1),
            _pt(L,    exit_hw,    -1),
        ]
        # L11 clip: narrow entry → clip boundary (exclusive tip slice)
        clip_pts = [
            _pt(0,    entry_hw,   -1),
            _pt(0,    entry_hw,   +1),
            _pt(CLIP, hw_at_clip, +1),
            _pt(CLIP, hw_at_clip, -1),
        ]
    else:
        hw_at_clip = exit_hw + (entry_hw - exit_hw) * (CLIP / L) if L > 0 else exit_hw
        # L1 body: wide entry → clip boundary
        trap_pts = [
            _pt(0,        entry_hw,   -1),
            _pt(0,        entry_hw,   +1),
            _pt(L - CLIP, hw_at_clip, +1),
            _pt(L - CLIP, hw_at_clip, -1),
        ]
        # L11 clip: clip boundary → narrow exit (exclusive tip slice)
        clip_pts = [
            _pt(L - CLIP, hw_at_clip, -1),
            _pt(L - CLIP, hw_at_clip, +1),
            _pt(L,        exit_hw,    +1),
            _pt(L,        exit_hw,    -1),
        ]

    taper_body = _poly(origin, trap_pts, LAYER_BRANCH)
    taper_body._no_auto_ports = False   # anchor — gets ports below

    # ── Narrow-tip clip slice (L11) ────────────────────────────────────────
    # Exclusive 1 µm tip region — no overlap with the L1 body above.
    narrow_clip = _poly(origin, clip_pts, LAYER_NARROW_END)
    narrow_clip._no_auto_ports = True

    components = [taper_body, narrow_clip]

    # ── Narrow-end undercut ring (L2) — optional ──────────────────────────
    #
    # Implements the same formula as the reference component_model.py render
    # block for narrow_undercut on taper_segment:
    #
    #   1. Take the full taper silhouette (L1 trapezoid + L11 clip welded into
    #      one outline: narrow face at travel=0 to wide face at travel=L).
    #   2. Shift a copy by UNDERCUT_OFFSET (0.8 µm) toward the narrow tip:
    #      shift = −travel direction when narrow_end="start",
    #      shift = +travel direction when narrow_end="end".
    #   3. Crescent = shifted outline (outer) + original outline reversed (inner).
    #      This 8-vertex closed polygon covers both tapered flanks + the tip face.
    #   4. Punch out the open wire-connection face by replacing the four corner
    #      vertices at the tip with punch-boundary vertices at ±narrow_width/2,
    #      yielding a final 8-vertex punched crescent that covers only the flanks.
    #
    # The result is a single L2 GDSComponent.  Zero gdspy/gdstk required —
    # all geometry is pure vertex arithmetic from variables already in scope.
    #
    # Merging with outer undercut rings (same layer):
    #   The exporter's L2 merge pass (export_gds in exporter.py) boolean-ORs
    #   every L2 polygon in the design cell before writing.  When a narrow
    #   undercut polygon is spatially adjacent to or overlapping an outer
    #   undercut ring, they automatically unify into one smooth outline.
    #
    # Toggle: rebuild the cell with narrow_undercut=True/False to show/hide.
    if narrow_undercut:
        _UCUT = 0.8   # µm — matches UNDERCUT_RING_THICKNESS everywhere

        # Shift toward narrow tip (−travel for "start", +travel for "end")
        _tip_sign = -1.0 if narrow_end == "start" else 1.0
        sx = tx * _tip_sign * _UCUT
        sy = ty * _tip_sign * _UCUT

        # Full taper silhouette: 4 corners from narrow entry to wide exit.
        # (entry_hw / exit_hw depend on narrow_end — set above in the L1 block)
        full_pts = [
            _pt(0, entry_hw, +1),   # entry +transverse
            _pt(L, exit_hw,  +1),   # exit  +transverse
            _pt(L, exit_hw,  -1),   # exit  −transverse
            _pt(0, entry_hw, -1),   # entry −transverse
        ]

        # Shifted copy (outer boundary of crescent)
        s_pts = [(xp + sx, yp + sy) for xp, yp in full_pts]

        # Tip face position and narrow half-width for the punch
        tip_travel = 0.0 if narrow_end == "start" else float(L)
        hw_n = nw / 2   # narrow_width / 2

        # Punch boundary corners (at ±hw_n transverse, depth _UCUT along shift)
        # These replace the tip-face corners in the crescent, clipping the open
        # connection face to zero undercut width.
        p_orig_pos  = (tx * tip_travel + px *  hw_n,        ty * tip_travel + py *  hw_n)
        p_orig_neg  = (tx * tip_travel - px *  hw_n,        ty * tip_travel - py *  hw_n)
        p_shift_pos = (tx * tip_travel + sx + px *  hw_n,   ty * tip_travel + sy + py *  hw_n)
        p_shift_neg = (tx * tip_travel + sx - px *  hw_n,   ty * tip_travel + sy - py *  hw_n)

        # ── Exact replication of component_model.py boolean result ───────────
        #
        # Reference: shifted_union NOT original_union, then punch rectangle.
        #
        # For narrow_end="start", direction="+x":
        #   shift = (-0.8, 0)  (toward the narrow tip at x=0)
        #   shifted trapezoid: (-0.8, ±entry_hw) → (L-0.8, ±exit_hw)
        #   original trapezoid: (0, ±entry_hw) → (L, ±exit_hw)
        #
        #   shifted NOT original produces THREE pieces:
        #     1. Top flank strip:    s_entry+ → s_exit+ → o_exit+ → o_entry+
        #     2. Bottom flank strip: o_entry- → o_exit- → s_exit- → s_entry-
        #     3. Tip triangle:       s_entry+ → o_entry+ → o_entry- → s_entry-
        #        (the part of the shifted shape that pokes past the original tip face)
        #
        #   Punch rectangle at tip (from tip face outward into the shift):
        #     (0, -hw_n) → (sx, -hw_n) → (sx, +hw_n) → (0, +hw_n)
        #     This covers the full tip triangle (hw_n = entry_hw for narrow_end="start")
        #     so after subtraction the tip triangle is completely removed.
        #
        #   Final result: two flank quadrilaterals only.
        #
        # For narrow_end="end" the geometry is symmetric (tip at travel=L,
        # shift is +travel direction).
        #
        # We emit each flank as its own _poly component.

        # Corners of the shifted and original trapezoids
        o_epos = _pt(0, entry_hw, +1)   # original entry +
        o_eneg = _pt(0, entry_hw, -1)   # original entry −
        o_xpos = _pt(L, exit_hw,  +1)   # original exit  +
        o_xneg = _pt(L, exit_hw,  -1)   # original exit  −

        s_epos = (o_epos[0] + sx, o_epos[1] + sy)  # shifted entry +
        s_eneg = (o_eneg[0] + sx, o_eneg[1] + sy)  # shifted entry −
        s_xpos = (o_xpos[0] + sx, o_xpos[1] + sy)  # shifted exit  +
        s_xneg = (o_xneg[0] + sx, o_xneg[1] + sy)  # shifted exit  −

        # Top flank: shifted entry+ → shifted exit+ → orig exit+ → orig entry+
        top_flank = _poly(origin, [s_epos, s_xpos, o_xpos, o_epos], LAYER_UNDERCUT_RING)
        top_flank._no_auto_ports = True
        top_flank.is_undercut = True          # ← add
        components.append(top_flank)

        bot_flank = _poly(origin, [o_eneg, o_xneg, s_xneg, s_eneg], LAYER_UNDERCUT_RING)
        bot_flank._no_auto_ports = True
        bot_flank.is_undercut = True          # ← add
        components.append(bot_flank)

    # ── Ports on the anchor (taper_body) ──────────────────────────────────
    # Port offsets are relative to taper_body.origin = _poly's dbu_pts[0]
    # = the first vertex of trap_pts.
    #
    # For narrow_end="start": first vertex = _pt(CLIP, hw_at_clip, -1)
    #   body_origin_cell = (tx*CLIP - px*hw_at_clip, ty*CLIP - py*hw_at_clip)
    # For narrow_end="end":   first vertex = _pt(0, entry_hw, -1)
    #   body_origin_cell = (-px*entry_hw, -py*entry_hw)
    #
    # Desired port world positions (cell-frame, relative to cell origin):
    #   entry midpoint = (0, 0)      exit midpoint = (tx*L, ty*L)
    # Port offset = desired_world - body_origin_cell
    _opp = {"+x": PortSide.WEST, "-x": PortSide.EAST,
            "+y": PortSide.NORTH, "-y": PortSide.SOUTH}
    _fwd = {"+x": PortSide.EAST,  "-x": PortSide.WEST,
            "+y": PortSide.SOUTH, "-y": PortSide.NORTH}

    if narrow_end == "start":
        bx = tx * CLIP - px * hw_at_clip
        by = ty * CLIP - py * hw_at_clip
    else:
        bx = -px * entry_hw
        by = -py * entry_hw

    entry_offset = (0  - bx,       0  - by)
    exit_offset  = (tx * L - bx,   ty * L - by)

    _assign_ports(taper_body, [
        Port(entry_label,
             Point(um_to_dbu(entry_offset[0]), um_to_dbu(entry_offset[1])),
             _opp[direction]),
        Port(exit_label,
             Point(um_to_dbu(exit_offset[0]), um_to_dbu(exit_offset[1])),
             _fwd[direction]),
    ])

    dir_label = direction.replace("+", "+").replace("-", "-")
    ucut_tag = " +ucut" if narrow_undercut else ""
    return CellResult(
        components=components,
        group_name=f"TaperedLead ({direction} L={length:.1f}µm nw={narrow_width:.2f}µm{ucut_tag})",
        description=(
            f"Linear taper  {direction}  L={length}µm  "
            f"narrow={narrow_width}µm → wide={taper_width}µm  "
            f"narrow_end={narrow_end}"
            + ("  narrow_undercut=L2" if narrow_undercut else "")
        ),
    )

# ═════════════════════════════════════════════════════════════════════════════
# Cell: Smooth Taper Pad  (→ smooth_taper + add_taper_pad in primitives.py)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors the Config values used in the reference layout:
#   TAPER_WIDTH        → narrow_width  (entry, wire-side)
#   FINAL_TAPER_WIDTH  → pad_width     (exit,  pad-side)
#   FINAL_TAPER_LENGTH → taper_length
#   FINAL_PAD_LENGTH   → pad_length
_SMOOTH_TAPER_PAD_DEFAULTS = dict(
    direction    = "+x",   # "+x" | "-x" | "+y" | "-y"
    narrow_width = 2.0,    # entry (wire-side) width µm  → cfg.TAPER_WIDTH
    pad_width    = 10.0,   # exit  (pad-side)  width µm  → cfg.FINAL_TAPER_WIDTH
    taper_length = 6.1,    # cosine-taper length µm      → cfg.FINAL_TAPER_LENGTH
    pad_length   = 2.1,    # flat pad length µm           → cfg.FINAL_PAD_LENGTH
    n_segments   = 128,    # polygon vertex count for the cosine curve
)


def build_smooth_taper_pad(
    origin: Point,
    direction:    str   = _SMOOTH_TAPER_PAD_DEFAULTS["direction"],
    narrow_width: float = _SMOOTH_TAPER_PAD_DEFAULTS["narrow_width"],
    pad_width:    float = _SMOOTH_TAPER_PAD_DEFAULTS["pad_width"],
    taper_length: float = _SMOOTH_TAPER_PAD_DEFAULTS["taper_length"],
    pad_length:   float = _SMOOTH_TAPER_PAD_DEFAULTS["pad_length"],
    n_segments:   int   = _SMOOTH_TAPER_PAD_DEFAULTS["n_segments"],
) -> CellResult:
    """
    Two-stage transition on LAYER_BRANCH (L1):

    Stage 1 — Smooth cosine taper (polygon):
        Runs from *origin* for *taper_length* in *direction*.
        Entry width = narrow_width, exit width = pad_width.
        Width profile: w(t) = w0 + (w1 − w0) × ½(1 − cos(πt)), t ∈ [0, 1].
        Approximated as a 2×n_segments-vertex polygon, identical to the
        output of ``smooth_taper()`` in the reference primitives.py.

    Stage 2 — Flat overlap pad (rectangle):
        Runs from the taper exit for *pad_length* in *direction*.
        Uniform width = pad_width throughout.
        Shares the taper exit edge as its entry boundary (no gap).

    Both stages are on LAYER_BRANCH (L1).  No LAYER_NARROW_END clip is
    emitted — this is the wide termination end, not the narrow tip.

    Origin convention
    -----------------
    *origin* is the centreline of the **entry (narrow) end**, matching the
    (x0, y0) convention used by ``smooth_taper()`` and ``add_taper_pad()``
    in the reference codebase.

    Ports (on the taper anchor)
    ---------------------------
    "narrow" — entry face, faces opposite to direction of travel.
    "wide"   — exit face (at the far end of the flat pad), faces direction.

    Port offsets are relative to taper_body.origin (first polygon vertex).
    """
    import math

    if direction not in ("+x", "-x", "+y", "-y"):
        raise ValueError(
            f"direction must be '+x', '-x', '+y', or '-y'; got {direction!r}"
        )

    nw = narrow_width
    pw = pad_width
    tL = taper_length
    pL = pad_length
    n  = n_segments

    # ── Direction unit vectors ─────────────────────────────────────────────
    # Convention matches _dir_map used throughout this file.
    _dir_map = {
        "+x": (( 1,  0), (0,  1)),
        "-x": ((-1,  0), (0,  1)),
        "+y": (( 0,  1), (1,  0)),
        "-y": (( 0, -1), (1,  0)),
    }
    (tx, ty), (px, py) = _dir_map[direction]

    # ── Cosine width profile ───────────────────────────────────────────────
    # half-width at fractional taper position t ∈ [0, 1]
    def _hw(t: float) -> float:
        return (nw + (pw - nw) * 0.5 * (1.0 - math.cos(math.pi * t))) / 2.0

    # upper edge: entry → exit along +transverse
    upper: list[tuple[float, float]] = [
        (tx * tL * t + px * _hw(t),
         ty * tL * t + py * _hw(t))
        for t in (i / n for i in range(n + 1))
    ]
    # lower edge: exit → entry along -transverse (reversed for CCW winding)
    lower: list[tuple[float, float]] = [
        (tx * tL * t - px * _hw(t),
         ty * tL * t - py * _hw(t))
        for t in (i / n for i in range(n, -1, -1))
    ]

    taper_body = _poly(origin, upper + lower, LAYER_BRANCH)
    taper_body._no_auto_ports = False   # anchor — carries all ports

    # ── Flat pad (rectangle) ──────────────────────────────────────────────
    pad_pts: list[tuple[float, float]] = [
        (tx * tL        - px * pw / 2,  ty * tL        - py * pw / 2),
        (tx * tL        + px * pw / 2,  ty * tL        + py * pw / 2),
        (tx * (tL + pL) + px * pw / 2,  ty * (tL + pL) + py * pw / 2),
        (tx * (tL + pL) - px * pw / 2,  ty * (tL + pL) - py * pw / 2),
    ]
    pad_body = _poly(origin, pad_pts, LAYER_BRANCH)
    pad_body._no_auto_ports = True

    # ── Ports on the anchor ────────────────────────────────────────────────
    # taper_body.origin is the first polygon vertex = upper[0]
    # = (px * nw/2, py * nw/2) in cell-frame µm.
    # Port offset = desired_cell_frame_pos − body_origin_cell_frame
    bx = px * nw / 2
    by = py * nw / 2

    _opp_side = {
        "+x": PortSide.WEST,  "-x": PortSide.EAST,
        "+y": PortSide.NORTH, "-y": PortSide.SOUTH,
    }
    _fwd_side = {
        "+x": PortSide.EAST,  "-x": PortSide.WEST,
        "+y": PortSide.SOUTH, "-y": PortSide.NORTH,
    }

    _assign_ports(taper_body, [
        Port("narrow",
             Point(um_to_dbu(0.0 - bx),              um_to_dbu(0.0 - by)),
             _opp_side[direction]),
        Port("wide",
             Point(um_to_dbu(tx * (tL + pL) - bx),  um_to_dbu(ty * (tL + pL) - by)),
             _fwd_side[direction]),
    ])

    return CellResult(
        components=[taper_body, pad_body],
        group_name=(
            f"SmoothTaperPad ({direction} tL={taper_length:.1f}µm pL={pad_length:.1f}µm)"
        ),
        description=(
            f"Cosine taper pad  {direction}  "
            f"taper {taper_length}µm  pad {pad_length}µm  "
            f"nw={narrow_width}µm → pw={pad_width}µm  L1"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell: Turn 90°  (→ add_turn in components_lib.py)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors turn ComponentType in component_model.py
_TURN_DEFAULTS = dict(
    entry_dir   = "+x",   # direction wire travels INTO the turn
    turn_dir    = "l",    # "l" (CCW / left) | "r" (CW / right)
    taper_width = 2.0,    # arc width (µm) → cfg.TAPER_WIDTH
    turn_radius = 5.0,    # arc centreline radius (µm) → cfg.TURN_RADIUS
)


def build_turn(
    origin: Point,
    entry_dir:   str   = _TURN_DEFAULTS["entry_dir"],
    turn_dir:    str   = _TURN_DEFAULTS["turn_dir"],
    taper_width: float = _TURN_DEFAULTS["taper_width"],
    turn_radius: float = _TURN_DEFAULTS["turn_radius"],
) -> CellResult:
    """
    90-degree arc turn on LAYER_BRANCH (L1).

    Mirrors add_turn() from components_lib.py.  The arc is approximated
    as a polygon fan (32 segments) so it renders correctly in the canvas
    without requiring gdspy at build time.

    Origin is the centreline of the arc entry point, matching the (x, y)
    convention used throughout components_lib.py.

    The arc sweeps from entry_dir to the exit direction:
      left  (CCW, "l") — rotates heading +90°
      right (CW,  "r") — rotates heading −90°

    Exit offset from origin (arc centre-of-curvature geometry):
      The centre of curvature sits turn_radius perpendicular to entry,
      so the exit point is at:
        dx = R * (perp_x + exit_x_component)
        dy = R * (perp_y + exit_y_component)

    Ports
    -----
      entry — centreline of the arc entry (at origin, faces opposite to entry_dir)
      exit  — centreline of the arc exit  (offset by arc geometry, faces exit_dir)
    """
    import math

    R  = turn_radius
    hw = taper_width / 2
    N  = 32   # polygon segments for the arc

    # ── Direction arithmetic ───────────────────────────────────────────────
    # Unit vectors
    _fwd_vec = {"+x": (1, 0), "-x": (-1, 0), "+y": (0, 1), "-y": (0, -1)}
    _opp_dir = {"+x": "-x",  "-x": "+x",  "+y": "-y",  "-y": "+y"}

    # Left (CCW) perpendicular of a vector (dx, dy): (-dy, dx)
    # Right (CW)                                   : ( dy, -dx)
    fx, fy = _fwd_vec[entry_dir]
    if turn_dir == "l":
        perp_x, perp_y = -fy,  fx    # CCW: rotate entry +90° to get left normal
    else:
        perp_x, perp_y =  fy, -fx    # CW:  rotate entry -90° to get right normal

    # Centre of curvature is R in the perpendicular (inward) direction
    cx_oc = perp_x * R   # offset of arc centre from origin (µm)
    cy_oc = perp_y * R

    # Arc sweeps 90° — determine start and end angles
    # Start angle: vector from arc centre → origin = -(perp) direction
    start_angle = math.atan2(-perp_y, -perp_x)
    if turn_dir == "l":
        end_angle = start_angle + math.pi / 2
    else:
        end_angle = start_angle - math.pi / 2

    # ── Build fan polygon ─────────────────────────────────────────────────
    # Outer arc: radius R + hw, inner arc: radius R - hw
    # Points in µm relative to origin
    outer_pts = []
    inner_pts = []
    for i in range(N + 1):
        t = i / N
        angle = start_angle + (end_angle - start_angle) * t
        ca, sa = math.cos(angle), math.sin(angle)
        outer_pts.append((cx_oc + (R + hw) * ca, cy_oc + (R + hw) * sa))
        inner_pts.append((cx_oc + (R - hw) * ca, cy_oc + (R - hw) * sa))

    # Fan: outer arc forward, inner arc reversed → closed polygon
    fan_pts = outer_pts + list(reversed(inner_pts))

    body = _poly(origin, fan_pts, LAYER_BRANCH)
    body._no_auto_ports = False   # anchor

    # ── Exit port offset ──────────────────────────────────────────────────
    #
    # Port positions match _make_turn_ports() in component_model.py exactly.
    # The exit centreline is at (R, R), (R, -R), etc. relative to the entry
    # centreline (origin) — derived from the arc geometry: the centre of
    # curvature is R in the perpendicular direction, and the exit point is
    # R away from the centre in the exit direction.
    #
    # Simple lookup (entry_dir, turn_dir) → (dx, dy) µm, exit_dir string.
    # This is exactly _make_turn_ports._exit_map — keep in sync.
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
    (ex_dx, ex_dy), exit_dir = _exit_map[(entry_dir, turn_dir)]

    # Entry port faces opposite to entry_dir (wire comes in from that side).
    # Exit  port faces in exit_dir (wire leaves in that direction).
    _opp_side = {"+x": PortSide.WEST,  "-x": PortSide.EAST,
                 "+y": PortSide.SOUTH, "-y": PortSide.NORTH}
    _fwd_side = {"+x": PortSide.EAST,  "-x": PortSide.WEST,
                 "+y": PortSide.NORTH, "-y": PortSide.SOUTH}

    # Port offsets must be relative to body.origin = dbu_pts[0] = outer_pts[0]
    # (the first polygon vertex = outer arc start point).
    # Correction vector = -(outer_pts[0]) so that adding it to a cell-frame
    # world position yields the correct body-local offset.
    first_x = cx_oc + (R + hw) * math.cos(start_angle)
    first_y = cy_oc + (R + hw) * math.sin(start_angle)
    corr_x  = -first_x
    corr_y  = -first_y

    _assign_ports(body, [
        Port("entry",
             Point(um_to_dbu(corr_x), um_to_dbu(corr_y)),
             _opp_side[entry_dir]),
        Port("exit",
             Point(um_to_dbu(ex_dx + corr_x), um_to_dbu(ex_dy + corr_y)),
             _fwd_side[exit_dir]),
    ])

    return CellResult(
        components=[body],
        group_name=f"Turn90 ({entry_dir} {turn_dir} R={turn_radius:.1f}µm)",
        description=(
            f"90° arc turn  entry={entry_dir}  turn={turn_dir}  "
            f"R={turn_radius}µm  width={taper_width}µm  L1"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell: Wire Segment  (→ "wire" ComponentType in component_model.py)
# ═════════════════════════════════════════════════════════════════════════════

# Default geometry (µm) — mirrors wire ComponentType in component_model.py
_WIRE_DEFAULTS = dict(
    direction = "+x",   # "+x" | "-x" | "+y" | "-y"
    length    = 5.0,    # wire length  (µm)
    width     = 2.0,    # wire width   (µm) → cfg.WIRE_WIDTH
    layer     = 1,      # GDS layer    (int) → LAYER_BIYSK_JUNCTION default
)


def build_wire(
    origin: Point,
    direction: str   = _WIRE_DEFAULTS["direction"],
    length:    float = _WIRE_DEFAULTS["length"],
    width:     float = _WIRE_DEFAULTS["width"],
    layer:     int   = _WIRE_DEFAULTS["layer"],
) -> CellResult:
    """
    Simple rectangular wire segment on any layer.

    Mirrors the "wire" ComponentType from component_model.py.  Unlike
    branch_segment (which is always L1 / TAPER_WIDTH), this cell is fully
    configurable: any layer and any width, making it the go-to primitive
    for connecting leads on L5, L10, or any other layer.

    Origin is the centreline of the entry end.

    Ports
    -----
      start — centreline of the entry face (opposite to direction)
      end   — centreline of the exit  face (in direction of travel)
    """
    L  = length
    hw = width / 2

    _dir_map = {
        "+x": dict(tx=1,  ty=0,  px=0,  py=1),
        "-x": dict(tx=-1, ty=0,  px=0,  py=1),
        "+y": dict(tx=0,  ty=1,  px=1,  py=0),
        "-y": dict(tx=0,  ty=-1, px=1,  py=0),
    }
    d  = _dir_map[direction]
    tx, ty = d["tx"], d["ty"]
    px, py = d["px"], d["py"]

    rect_pts = [
        (tx * 0 + px * (-hw), ty * 0 + py * (-hw)),
        (tx * 0 + px * ( hw), ty * 0 + py * ( hw)),
        (tx * L + px * ( hw), ty * L + py * ( hw)),
        (tx * L + px * (-hw), ty * L + py * (-hw)),
    ]
    body = _poly(origin, rect_pts, layer)
    body._no_auto_ports = False   # anchor

    _opp = {"+x": PortSide.WEST,  "-x": PortSide.EAST,
            "+y": PortSide.NORTH, "-y": PortSide.SOUTH}
    _fwd = {"+x": PortSide.EAST,  "-x": PortSide.WEST,
            "+y": PortSide.SOUTH, "-y": PortSide.NORTH}

    _assign_ports(body, [
        Port("start",
             Point(um_to_dbu(px * hw),            um_to_dbu(py * hw)),
             _opp[direction]),
        Port("end",
             Point(um_to_dbu(tx * L + px * hw),   um_to_dbu(ty * L + py * hw)),
             _fwd[direction]),
    ])

    return CellResult(
        components=[body],
        group_name=f"Wire ({direction} L={length:.1f}µm w={width:.2f}µm L{layer})",
        description=(
            f"Wire segment  {direction}  L={length}µm  width={width}µm  layer={layer}"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell: T-Junction  — two back-to-back arc turns forming a T shape
# ═════════════════════════════════════════════════════════════════════════════

_T_JCT_DEFAULTS = dict(
    stem_dir    = "+y",   # direction the stem wire travels INTO the junction
    taper_width = 2.0,    # arc width (µm)
    turn_radius = 5.0,    # arc centreline radius (µm)
)


def build_t_junction(
    origin:      Point,
    stem_dir:    str   = _T_JCT_DEFAULTS["stem_dir"],
    taper_width: float = _T_JCT_DEFAULTS["taper_width"],
    turn_radius: float = _T_JCT_DEFAULTS["turn_radius"],
) -> CellResult:
    """
    T-junction: a single solid polygon on L1.

    Geometry (default stem_dir="+y", stem enters from below):

              left_exit (−x)           right_exit (+x)
        ────────────────────────────────────────────────
        |                                              |
        |   ╭──────────────────────────────────────╮  |
        |   │      ← bar top (y = R+hw) →          │  |
        |   ╰──────────╮            ╭──────────────╯  |
        |               ╲          ╱
        |                ╲        ╱   ← inner arcs (radius R−hw)
        |                 |      |
        |                 | stem |
        |                 |      |
                         stem port (origin)

    The polygon boundary (CCW) traces:
      1. right inner arc (R−hw, stem right-wall → right arm inner tip)
      2. straight bar corners (right outer corner, bar top-right, bar top-left,
                               left outer corner)
      3. left inner arc reversed (left arm inner tip → stem left-wall)
      4. stem base (left wall → right wall)

    This correctly avoids the self-intersecting bowtie produced by the previous
    outer-arc approach (outer arcs of radius R+hw cross each other between
    y=0 and y=R−hw when their centres are only 2R apart).

    Parameters
    ----------
    stem_dir    : direction the stem wire travels into the junction.
                  "+y" → stem enters from below, bar runs left/right.
                  Any of "+x" / "-x" / "+y" / "-y".
    taper_width : full width of each arm and stem leg (µm).
    turn_radius : centreline radius of each inner bend arc (µm).

    Ports
    -----
    stem       — entry, at cell origin, faces opposite to stem_dir.
    left_exit  — left  arm exit face centre, faces outward (left of stem).
    right_exit — right arm exit face centre, faces outward (right of stem).
    """
    import math as _math

    R  = turn_radius
    hw = taper_width / 2
    N  = 64   # arc segments per quarter-circle

    _fwd_vec = {"+x": (1, 0), "-x": (-1, 0), "+y": (0, 1), "-y": (0, -1)}
    _opp_side = {"+x": PortSide.WEST,  "-x": PortSide.EAST,
                 "+y": PortSide.SOUTH, "-y": PortSide.NORTH}
    _fwd_side = {"+x": PortSide.EAST,  "-x": PortSide.WEST,
                 "+y": PortSide.NORTH, "-y": PortSide.SOUTH}

    fx, fy = _fwd_vec[stem_dir]

    # Perpendicular unit vectors (right and left of the forward direction)
    rpx, rpy =  fy, -fx   # right_perp  (rotate fwd 90° CW)
    lpx, lpy = -fy,  fx   # left_perp   (rotate fwd 90° CCW)

    # ── Inner arc centres (distance R from origin, perpendicular to stem) ─────
    # Right arc: bends the stem to the right arm.
    # Left  arc: bends the stem to the left  arm.
    r_cx, r_cy = R * rpx, R * rpy
    l_cx, l_cy = R * lpx, R * lpy

    # ── Arc angle spans ───────────────────────────────────────────────────────
    # For each inner arc (radius R−hw), the start point is on the stem wall
    # and the end point is at the arm's inner corner.
    #
    # Right arc start: vector from r_centre to stem right-wall point = −right_perp
    #   a_start_right = atan2(−rpy, −rpx)
    # Right arc end:   vector from r_centre to arm inner tip = fwd direction
    #   a_end_right   = atan2(fy, fx)
    # Left arc start (reversed arc used in polygon): vector = +right_perp from l_centre
    #   a_start_left  = atan2(rpy, rpx)   (= a_end of the forward left arc)
    # Left arc end (reversed):             vector = fwd direction
    #   a_end_left    = atan2(fy, fx)
    a_start_right = _math.atan2(-rpy, -rpx)
    a_end_right   = _math.atan2(fy, fx)
    a_start_left  = _math.atan2(rpy, rpx)
    a_end_left    = _math.atan2(fy, fx)

    def _arc_pts(cx, cy, radius, a_start, a_end, n=N):
        """Uniformly-sampled arc from a_start to a_end (cell-frame µm)."""
        return [
            (cx + radius * _math.cos(a_start + (a_end - a_start) * i / n),
             cy + radius * _math.sin(a_start + (a_end - a_start) * i / n))
            for i in range(n + 1)
        ]

    # ── Inner arc strips ──────────────────────────────────────────────────────
    # inner_right : stem right-wall → right arm inner tip  (forward sweep)
    # inner_left_rev : left arm inner tip → stem left-wall (reverse of left arc)
    inner_right   = _arc_pts(r_cx, r_cy, R - hw, a_start_right, a_end_right)
    inner_left_rev = _arc_pts(l_cx, l_cy, R - hw, a_end_left,   a_start_left)

    # ── Bar corner points (in cell-frame µm) ──────────────────────────────────
    #
    #  right inner tip  = R*right_perp + (R−hw)*fwd
    #  right outer corner = (R+hw)*right_perp + (R−hw)*fwd
    #  right bar top    = (R+hw)*right_perp + (R+hw)*fwd
    #  left bar top     = (R+hw)*left_perp  + (R+hw)*fwd
    #  left outer corner = (R+hw)*left_perp  + (R−hw)*fwd
    #  (left inner tip  = R*left_perp  + (R−hw)*fwd  — provided by inner_left_rev[0])
    r_outer_corner = ((R + hw) * rpx + (R - hw) * fx,
                      (R + hw) * rpy + (R - hw) * fy)
    r_bar_top      = ((R + hw) * rpx + (R + hw) * fx,
                      (R + hw) * rpy + (R + hw) * fy)
    l_bar_top      = ((R + hw) * lpx + (R + hw) * fx,
                      (R + hw) * lpy + (R + hw) * fy)
    l_outer_corner = ((R + hw) * lpx + (R - hw) * fx,
                      (R + hw) * lpy + (R - hw) * fy)

    # ── Assemble polygon (CCW, starting at stem right-wall) ───────────────────
    body_pts = (
        inner_right                                           # stem right → right arm inner tip
        + [r_outer_corner, r_bar_top, l_bar_top, l_outer_corner]  # bar corners
        + inner_left_rev                                      # left arm inner tip → stem left
        # _poly() auto-closes: stem left-wall → stem right-wall (stem base)
    )

    body = _poly(origin, body_pts, LAYER_BRANCH)
    body._no_auto_ports = False   # anchor — carries all three ports

    # ── Ports ─────────────────────────────────────────────────────────────────
    # Offsets are relative to body.origin = body_pts[0] (= stem right-wall point).
    bx0, by0 = body_pts[0]

    def _cell_to_body(cx_um, cy_um):
        return Point(um_to_dbu(cx_um - bx0), um_to_dbu(cy_um - by0))

    # Stem port: at cell origin (0, 0), faces opposite to stem_dir.
    stem_port_pos = _cell_to_body(0.0, 0.0)

    # Left exit: centre of left arm face = (R+hw)*left_perp + R*fwd
    # (midpoint of the left outer corner's exit face, at the bar's left wall)
    l_exit_x = (R + hw) * lpx + R * fx
    l_exit_y = (R + hw) * lpy + R * fy
    left_exit_pos = _cell_to_body(l_exit_x, l_exit_y)

    # Right exit: centre of right arm face = (R+hw)*right_perp + R*fwd
    r_exit_x = (R + hw) * rpx + R * fx
    r_exit_y = (R + hw) * rpy + R * fy
    right_exit_pos = _cell_to_body(r_exit_x, r_exit_y)

    # Exit directions: left arm exits in left_perp direction, right in right_perp
    _perp_side = {
        ( 1,  0): PortSide.EAST,
        (-1,  0): PortSide.WEST,
        ( 0,  1): PortSide.NORTH,
        ( 0, -1): PortSide.SOUTH,
    }
    left_exit_side  = _perp_side[(int(lpx), int(lpy))]
    right_exit_side = _perp_side[(int(rpx), int(rpy))]

    _assign_ports(body, [
        Port("stem",       stem_port_pos,  _opp_side[stem_dir]),
        Port("left_exit",  left_exit_pos,  left_exit_side),
        Port("right_exit", right_exit_pos, right_exit_side),
    ])

    return CellResult(
        components=[body],
        group_name=(
            f"TJunction ({stem_dir} R={turn_radius:.1f}µm w={taper_width:.1f}µm)"
        ),
        description=(
            f"T-junction  stem={stem_dir}  R={turn_radius}µm  "
            f"width={taper_width}µm  L1"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cell catalogue  (what the palette reads)
# ═════════════════════════════════════════════════════════════════════════════

CELL_CATALOGUE: List[CellDef] = [
    CellDef(
        cell_id     = "byisk_jj",
        name        = "Biysk JJ",
        description = "Biysk bonding square with cap strips and L-undercut",
        category    = "Junctions",
        defaults    = _SQ_DEFAULTS,
        builder     = build_byisk_jj,
    ),
    CellDef(
        cell_id     = "manhattan_jj",
        name        = "Manhattan JJ",
        description = "Manhattan-style Josephson junction (lead + JJ square + extensions)",
        category    = "Junctions",
        defaults    = _JJ_DEFAULTS,
        builder     = build_manhattan_jj,
    ),
    CellDef(
        cell_id     = "wire",
        name        = "Lead Segment",
        description = "Straight uniform wire on any layer — configurable width, length, direction",
        category    = "Routing",
        defaults    = _WIRE_DEFAULTS,
        builder     = build_wire,
    ),
    CellDef(
        cell_id     = "taper_segment",
        name        = "Tapered Lead",
        description = "Linear WIRE_WIDTH↔TAPER_WIDTH wedge (L1) with L11 narrow-tip slice; optional narrow-end undercut ring on L2",
        category    = "Routing",
        defaults    = _TAPER_SEG_DEFAULTS,
        builder     = build_taper_segment,
    ),
    CellDef(
        cell_id     = "smooth_taper_pad",
        name        = "Smooth Taper Pad",
        description = "Cosine-profile taper wedge (L1) → flat overlap pad (L1)",
        category    = "Routing",
        defaults    = _SMOOTH_TAPER_PAD_DEFAULTS,
        builder     = build_smooth_taper_pad,
    ),
    CellDef(
        cell_id     = "turn",
        name        = "Turn 90°",
        description = "90° arc turn — configurable entry direction and handedness (L1)",
        category    = "Routing",
        defaults    = _TURN_DEFAULTS,
        builder     = build_turn,
    ),
    CellDef(
        cell_id     = "t_junction",
        name        = "T-Junction",
        description = "Two back-to-back arc turns forming a T with rounded corners (L1)",
        category    = "Routing",
        defaults    = _T_JCT_DEFAULTS,
        builder     = build_t_junction,
    ),
]

# Fast lookup by cell_id
CELL_BY_ID: dict[str, CellDef] = {c.cell_id: c for c in CELL_CATALOGUE}


# ═════════════════════════════════════════════════════════════════════════════
# Undercut ring  (also registered in CELL_CATALOGUE above for palette access)
# ═════════════════════════════════════════════════════════════════════════════

def build_undercut_ring(
    bbox_um: tuple[float, float, float, float],
    thickness_um: float = 0.8,
    sides: dict[str, bool] | None = None,
    origin: Point | None = None,
) -> CellResult:
    """
    Build a 0.8 µm perimeter undercut ring around any rectangular bounding box.

    The ring is decomposed into up to four axis-aligned rectangles on
    LAYER_UNDERCUT_RING (L2) with fully mitred corners — no gdspy boolean
    operations required, zero Qt dependency.

    Corner convention  (mitre = full thickness square at each corner)
    -----------------------------------------------------------------
    Each active side owns the corner squares at BOTH its ends, so corners
    are always filled when both adjacent sides are active.  When one side is
    inactive its strip is simply omitted; the corner square is then owned by
    the remaining adjacent side — this keeps the ring gapless for any
    combination of active sides.

    Specifically:
      top    strip : x ∈ [x0 − t, x1 + t],  y ∈ [y1, y1 + t]
      bottom strip : x ∈ [x0 − t, x1 + t],  y ∈ [y0 − t, y0]
      left   strip : x ∈ [x0 − t, x0],       y ∈ [y0, y1]
      right  strip : x ∈ [x1, x1 + t],        y ∈ [y0, y1]

    Top and bottom each span the full outer width (including corner squares).
    Left and right span only the inner height (corner squares belong to top/bottom).
    When top is inactive, left/right each gain their own corner square by
    spanning y ∈ [y0 − t, y1 + t] (handled automatically by the side logic).

    Parameters
    ----------
    bbox_um   : (x0, y0, x1, y1) — bounding box of the target object in µm,
                world-space (same coordinate frame as cell origins).
                x0 < x1, y0 < y1.
    thickness_um : ring thickness in µm (default 0.8 — matches UNDERCUT_RING_THICKNESS).
    sides     : dict of which sides to emit.  Missing keys default to True.
                Keys: "top", "bottom", "left", "right".
                Example: {"top": True, "bottom": True, "left": False, "right": True}
    origin    : optional Point (DBU) to use as the anchor for the returned CellResult.
                Defaults to the bbox min-corner (x0, y0) in DBU.

    Returns
    -------
    CellResult whose components are the active side strips on LAYER_UNDERCUT_RING.
    The first component (anchor) carries four ports at the midpoints of each
    bbox edge — "top", "bottom", "left", "right" — for snap connections.
    All sub-components carry _no_auto_ports = True.

    Usage
    -----
    Typical call (place a full ring around a cell group's bbox):

        from core.cell_library import build_undercut_ring, place_cell
        from core.model import Point

        result = place_cell("byisk_jj", Point(0, 0))
        # Compute µm bbox of the result:
        comps = result.components
        xs = [dbu_to_um(c.bbox.x_min) for c in comps] + [dbu_to_um(c.bbox.x_max) for c in comps]
        ys = [dbu_to_um(c.bbox.y_min) for c in comps] + [dbu_to_um(c.bbox.y_max) for c in comps]
        ring = build_undercut_ring((min(xs), min(ys), max(xs), max(ys)))

    Then push ring through PlaceCellCommand as usual.
    """
    # ── Resolve sides ─────────────────────────────────────────────────────────
    if sides is None:
        sides = {}
    top_on    = sides.get("top",    True)
    bottom_on = sides.get("bottom", True)
    left_on   = sides.get("left",   True)
    right_on  = sides.get("right",  True)

    x0, y0, x1, y1 = bbox_um
    t = thickness_um

    # ── Origin for the CellResult ─────────────────────────────────────────────
    # Use the provided origin, or default to bbox min-corner.
    if origin is None:
        origin = Point(um_to_dbu(x0), um_to_dbu(y0))

    # ── Strip geometry helpers ────────────────────────────────────────────────
    # All coords are in µm, offset from a cell-frame origin of (x0, y0).
    # _rect(origin, dx0, dy0, dx1, dy1, layer) converts µm offsets to DBU
    # relative to `origin`.  We express everything as offsets from (x0, y0).

    # Cell-frame offsets (so that _rect's origin + offset = world coords):
    #   world_x = x0 + dx   →   dx = world_x − x0
    #   world_y = y0 + dy   →   dy = world_y − y0
    W = x1 - x0   # bbox width  in µm
    H = y1 - y0   # bbox height in µm

    # Top strip    : y ∈ [H, H+t],  x ∈ [-t, W+t]  (owns corner squares)
    # Bottom strip : y ∈ [-t, 0],   x ∈ [-t, W+t]  (owns corner squares)
    # Left strip   : y ∈ [0, H],    x ∈ [-t, 0]    (inner height only)
    # Right strip  : y ∈ [0, H],    x ∈ [W, W+t]   (inner height only)
    #
    # When top is absent, left/right each extend to y ∈ [-t, H+t] to keep
    # corners filled.  Same logic for bottom.

    left_y0  = -t if bottom_on else -t   # always starts at -t (bottom owns it when on)
    left_y1  =  H + t if not top_on else H   # extends to H+t only when top is absent
    # Simplify: left/right get inner y when BOTH top and bottom are on;
    # they gain one corner when the adjacent horizontal side is absent.
    ly0 = 0  - (t if not bottom_on else 0)
    ly1 = H  + (t if not top_on    else 0)

    components: list[GDSComponent] = []

    # Track whether we have an anchor yet
    anchor: GDSComponent | None = None

    def _add_strip(dx0: float, dy0: float, dx1: float, dy1: float) -> None:
        nonlocal anchor
        comp = _rect(origin, dx0, dy0, dx1, dy1, LAYER_UNDERCUT_RING)
        comp.is_undercut = True
        if anchor is None:
            comp._no_auto_ports = False   # first strip is anchor
            anchor = comp
        else:
            comp._no_auto_ports = True
        components.append(comp)

    if top_on:
        _add_strip(-t,   H,   W + t, H + t)
    if bottom_on:
        _add_strip(-t,  -t,   W + t, 0)
    if left_on:
        _add_strip(-t,  ly0,  0,     ly1)
    if right_on:
        _add_strip( W,  ly0,  W + t, ly1)

    # Fallback: if ALL sides are off, return an empty CellResult
    if not components:
        return CellResult(
            components=[],
            group_name="UnderCutRing (empty — all sides off)",
            description="Undercut ring — no sides active",
        )

    # ── Ports on the anchor ───────────────────────────────────────────────────
    # Four ports at the midpoints of each bbox edge (not the ring outer edge),
    # expressed relative to the anchor component's own .origin.
    # anchor.origin = _rect origin for the first strip = Point(um_to_dbu(x0), um_to_dbu(y0))
    # = our `origin`.  So the offset is simply the bbox-edge midpoint in cell-frame µm.

    # bbox edge midpoints in cell-frame (offset from (x0, y0)):
    #   top    : (W/2, H)
    #   bottom : (W/2, 0)
    #   left   : (0,   H/2)
    #   right  : (W,   H/2)

    _assign_ports(anchor, [
        Port("top",    Point(um_to_dbu(W / 2), um_to_dbu(H)),     PortSide.NORTH),
        Port("bottom", Point(um_to_dbu(W / 2), um_to_dbu(0)),     PortSide.SOUTH),
        Port("left",   Point(um_to_dbu(0),     um_to_dbu(H / 2)), PortSide.WEST),
        Port("right",  Point(um_to_dbu(W),     um_to_dbu(H / 2)), PortSide.EAST),
    ])

    active = [s for s, on in [("top", top_on), ("bottom", bottom_on),
                               ("left", left_on), ("right", right_on)] if on]
    sides_label = "+".join(active) if active else "none"

    return CellResult(
        components=components,
        group_name=f"UnderCutRing (t={thickness_um}µm sides={sides_label})",
        description=(
            f"0.8 µm perimeter undercut ring  thickness={thickness_um}µm  "
            f"sides={sides_label}  L2"
        ),
    )


# ── Public placement API ──────────────────────────────────────────────────────

def place_cell(
    cell_id: str,
    origin: Point,
    params: dict | None = None,
) -> CellResult:
    """
    Build a cell at *origin* using the catalogue defaults overridden by *params*.

    Parameters
    ----------
    cell_id : one of the cell_id strings in CELL_CATALOGUE
    origin  : world-space Point (DBU) for the cell anchor
    params  : optional dict of µm-space parameter overrides

    Returns
    -------
    CellResult ready to be pushed through AddComponent + GroupComponents commands.

    Raises
    ------
    KeyError   — unknown cell_id
    ValueError — params contains an unrecognised key (catches typos silently
                 swallowed before this fix)
    """
    cdef = CELL_BY_ID[cell_id]   # raises KeyError for unknown cell_id
    if params:
        unknown = set(params) - set(cdef.defaults)
        if unknown:
            raise ValueError(
                f"place_cell('{cell_id}'): unknown parameter(s) {sorted(unknown)}. "
                f"Valid keys: {sorted(cdef.defaults)}"
            )
    kwargs = dict(cdef.defaults)
    if params:
        kwargs.update(params)
    return cdef.builder(origin, **kwargs)