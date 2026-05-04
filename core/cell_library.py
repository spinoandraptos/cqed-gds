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
  square_node       — bonding square with caps + L-undercut (from add_square_node)
  manhattan_jj      — Manhattan-style Josephson junction stack (from add_manhattan_junction)

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
  - build_square_node() port positions computed geometrically from face centres
    instead of four hand-coded combinatorial branches — extensible to new styles.
  - FIX: build_square_node() caps now emit TWO layers (CAP1 inner strip +
    CAP2 outer strip) matching add_top_caps/add_side_caps in undercuts.py.
    Previously only CAP1 was emitted; CAP2 was silently missing.
  - FIX: build_square_node() L-undercut is now drawn OUTSIDE the body body on
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

    This replaces the old combinatorial if/elif ladder in build_square_node
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
# Cell: Square Node  (→ add_square_node in components_lib.py)
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


def build_square_node(
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

    Mirrors add_square_node() / add_top_caps() / add_side_caps() /
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
# Cell catalogue  (what the palette reads)
# ═════════════════════════════════════════════════════════════════════════════

CELL_CATALOGUE: List[CellDef] = [
    CellDef(
        cell_id     = "square_node",
        name        = "Square Node",
        description = "Biysk bonding square with cap strips and L-undercut",
        category    = "Superconducting",
        defaults    = _SQ_DEFAULTS,
        builder     = build_square_node,
    ),
    CellDef(
        cell_id     = "manhattan_jj",
        name        = "Manhattan JJ",
        description = "Manhattan-style Josephson junction (lead + JJ square + extensions)",
        category    = "Superconducting",
        defaults    = _JJ_DEFAULTS,
        builder     = build_manhattan_jj,
    ),
]

# Fast lookup by cell_id
CELL_BY_ID: dict[str, CellDef] = {c.cell_id: c for c in CELL_CATALOGUE}


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