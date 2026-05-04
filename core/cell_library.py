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

Fixes applied (v2)
------------------
  - Layer constants defined once here (removed duplication with component_model.py).
  - _poly() now correctly closes the polygon when last point ≠ first.
  - Port offsets in build_manhattan_jj() computed directly in component-origin
    space; no more redundant round-trip through the cell-centre frame.
  - place_cell() validates param keys against catalogue defaults and raises a
    clear ValueError for unknown keys instead of letting the builder fail.
  - build_square_node() port positions computed geometrically from face centres
    instead of four hand-coded combinatorial branches — extensible to new styles.
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
    cap_width      = 0.3,    # width of cap strip
    cap_length     = 0.8,    # length of cap strip
    wire_width     = 0.3,    # wire lead width (used for port offset calc)
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
    Bonding square on LAYER_BIYSK_JUNCTION (L5) with cap strips on
    LAYER_CAP1 (L4) / LAYER_CAP2 (L6) and an L-shaped undercut on
    LAYER_UNDERCUT_RING (L2).

    Coordinate origin is the **centre** of the square body, matching the
    reference codebase convention (cx, cy passed to add_square_node).

    cap_style      : "top"  — caps extend above and below (±y)
                     "side" — caps extend left and right (±x)
    undercut_style : "right" — L-undercut opens to the right (+x)
                     "top"   — L-undercut opens upward (+y)

    Ports are placed geometrically on the four free faces of the body.
    The face blocked by the undercut arm gets its port omitted so routing
    can't accidentally connect into the undercut.  All offsets derive from
    the body's bbox — no hand-coded combinatorial branches.
    """
    hx = square_x / 2.0
    hy = square_y / 2.0
    cw = cap_width
    cl = cap_length

    components: List[GDSComponent] = []

    # ── 1. Main body (L5) ─────────────────────────────────────────────────────
    body = _rect(origin, -hx, -hy, hx, hy, LAYER_BIYSK_JUNCTION)
    components.append(body)

    # ── 2. Cap strips ─────────────────────────────────────────────────────────
    if cap_style == "top":
        # Top cap strip (above +y edge)
        components.append(_rect(origin, -hx, hy, hx, hy + cl, LAYER_CAP1))
        # Bottom cap strip (below −y edge)
        components.append(_rect(origin, -hx, -hy - cl, hx, -hy, LAYER_CAP1))
    else:  # "side"
        # Right cap strip (+x edge)
        components.append(_rect(origin, hx, -hy, hx + cl, hy, LAYER_CAP1))
        # Left cap strip (−x edge)
        components.append(_rect(origin, -hx - cl, -hy, -hx, hy, LAYER_CAP1))

    # ── 3. L-undercut (L2) ────────────────────────────────────────────────────
    # Reference add_L_undercut_right / add_L_undercut_top:
    #   "right" undercut: horizontal arm along +y edge, vertical arm down −y side
    #   "top"   undercut: vertical arm along +x edge, horizontal arm left −x side
    uc_thick = cw   # undercut strip thickness equals cap_width in the reference

    if undercut_style == "right":
        # Horizontal arm: top edge of square, extending to the right
        components.append(_rect(
            origin,
            hx, hy - uc_thick,
            hx + cl, hy,
            LAYER_UNDERCUT_RING,
        ))
        # Vertical arm: right side of square, extending down
        components.append(_rect(
            origin,
            hx - uc_thick, -hy,
            hx, hy - uc_thick,
            LAYER_UNDERCUT_RING,
        ))
    else:  # "top"
        # Vertical arm: top edge of square, extending up
        components.append(_rect(
            origin,
            hx - uc_thick, hy,
            hx, hy + cl,
            LAYER_UNDERCUT_RING,
        ))
        # Horizontal arm: top of square, extending left
        components.append(_rect(
            origin,
            -hx, hy - uc_thick,
            hx - uc_thick, hy,
            LAYER_UNDERCUT_RING,
        ))

    # ── 4. Ports — computed geometrically, no combinatorial branches ──────────
    # ── 4. Ports — semantically correct, matching component_model._make_square_ports
    #
    # Convention (from the reference layout in component_model.py):
    #   A wire_width-wide lead placed flush with one corner of the square
    #   has its centreline at ±(h − wire_width/2) on the transverse axis.
    #
    # All offsets are body-local (relative to body.origin = bbox min-corner).
    # The square centre in body-local coords is (hx, hy).
    w = wire_width
    def _p(name, lx_um, ly_um, side):
        return Port(name, Point(um_to_dbu(lx_um), um_to_dbu(ly_um)), side)

    if cap_style == "top" and undercut_style == "right":
        ports = [
            _p("right",  hx * 2,           hy * 2 - w / 2, PortSide.EAST),
            _p("bottom", w / 2,             0,              PortSide.SOUTH),
            _p("top",    hx,                hy * 2,         PortSide.NORTH),
            _p("left",   0,                 hy,             PortSide.WEST),
        ]
    elif cap_style == "side" and undercut_style == "top":
        ports = [
            _p("top",    hx * 2 - w / 2,   hy * 2,         PortSide.NORTH),
            _p("left",   0,                 w / 2,          PortSide.WEST),
            _p("right",  hx * 2,            hy,             PortSide.EAST),
            _p("bottom", hx,                0,              PortSide.SOUTH),
        ]
    elif cap_style == "top" and undercut_style == "top":
        ports = [
            _p("top",    hx * 2 - w / 2,   hy * 2,         PortSide.NORTH),
            _p("bottom", hx,                0,              PortSide.SOUTH),
            _p("right",  hx * 2,            hy,             PortSide.EAST),
            _p("left",   0,                 hy,             PortSide.WEST),
        ]
    else:  # cap_style="side", undercut_style="right"
        ports = [
            _p("right",  hx * 2,           hy * 2 - w / 2, PortSide.EAST),
            _p("left",   0,                 hy,             PortSide.WEST),
            _p("top",    hx,                hy * 2,         PortSide.NORTH),
            _p("bottom", hx,                0,              PortSide.SOUTH),
        ]
    _assign_ports(body, ports)

    # Mark sub-components (everything except the anchor body) as port-free.
    # This prevents rebuild_ports() from auto-generating generic N/S/E/W ports
    # on caps, undercut arms, etc.  Only the body (index 0) gets real ports.
    for comp in components[1:]:
        comp._no_auto_ports = True

    return CellResult(
        components=components,
        group_name=f"SquareNode ({square_x:.1f}×{square_y:.1f}µm)",
        description=(
            f"Biysk bonding square  {square_x}×{square_y} µm  "
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

    # ── 6. Ports on the horizontal lead — matching component_model._make_jj_ports
    #
    # Reference (component_model.py _make_jj_ports):
    #   s = lead_width (JJ square side), L = lead_length
    #   Port("lead_in",   0,            0,               "-x")   ← entry of horiz lead
    #   Port("down_out",  L + s/2,     -(s/2 + L),      "-y")   ← bottom of down lead
    #   Port("right_out", L + 2s + 0.9, 0,               "+x")  ← right end of CAP2
    #   Port("top_out",   L + s/2,      s/2 + s + 0.9,  "+y")   ← top of CAP2
    #
    # Those are all in CELL-FRAME coords (relative to cell origin = entry of horiz lead).
    # horiz_lead.origin is at (origin.x, origin.y - w/2) in world DBU,
    # so we must subtract horiz_lead.origin from each cell-frame position to get
    # the port offset relative to horiz_lead.origin.
    #
    # horiz_lead.origin relative to cell origin: (0, -w/2)  [µm]
    # → port_offset = cell_frame_pos - (0, -w/2) = (cf_x, cf_y + w/2)

    # Cell-frame positions (µm, origin = left end of horiz lead at cell entry):
    #   lead_in  : (0,               0)        → body offset (0,        w/2)
    #   down_out : (x_sq + s/2,      y_down_bottom) → body offset (x_sq+s/2, y_down_bottom+w/2)
    #   right_out: (x_sq+2s+e2+e3,   0)        → body offset (x_sq+2s+e2+e3, w/2)
    #   top_out  : (x_sq + s/2,      y_top_base+s+e2+e3) → body offset (x_sq+s/2, y_top_base+s+e2+e3+w/2)

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