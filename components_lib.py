"""
components.py — Mid-level components: bonding squares, Manhattan junction,
                and routed branches.

Each function appends geometry to *parts* and returns any coordinates
needed by callers (documented per function).
"""

from __future__ import annotations

import gdspy

from config import Config
from primitives import add_rect, add_square, add_taper_pad, clip_narrow_end, clip_start_narrow_end
from undercuts import (
    add_top_caps,
    add_side_caps,
    add_L_undercut_right,
    add_L_undercut_top,
)


# ── Bonding / connecting squares ─────────────────────────────────────────────

def add_square_node(
    parts: list,
    cx: float,
    cy: float,
    cap_style: str,
    undercut_style: str,
    cfg: Config,
) -> None:
    """
    Place a bonding square with caps and undercut.

    Parameters
    ----------
    cap_style      : 'top' | 'side'
    undercut_style : 'right' | 'top'
    """
    add_square(parts, cx, cy, cfg.SQUARE_SIZE, cfg.LAYER_BIYSK_JUNCTION)

    cap_fn = {"top": add_top_caps, "side": add_side_caps}[cap_style]
    cap_fn(parts, cx, cy, cfg)

    undercut_fn = {"right": add_L_undercut_right, "top": add_L_undercut_top}[undercut_style]
    undercut_fn(parts, cx, cy, cfg)


# ── Manhattan Josephson junction ──────────────────────────────────────────────

def add_manhattan_junction(
    parts: list,
    x0: float,
    y0: float,
    cfg: Config,
) -> tuple[float, float]:
    """
    Draw a Manhattan-style JJ stack starting from (x0, y0).

    Stack (left → right):
      horizontal lead (L5) → JJ square (L10) → right extensions (L5/CAP1/CAP2)

    Stack (bottom → top of JJ square):
      top extensions (L5/CAP1/CAP2)

    Below JJ square:
      downward vertical lead (L5)

    Returns
    -------
    (downlead_cx, downlead_bottom) — bottom-centre of the downward lead,
    used to connect the snake route.
    """
    w = cfg.JUNCTION_LEAD_WIDTH
    L = cfg.JUNCTION_LEAD_LENGTH
    s = cfg.JUNCTION_SQUARE_SIZE

    # Horizontal lead → square origin
    add_rect(parts, (x0, y0 - w / 2), (x0 + L, y0 + w / 2), cfg.LAYER_BIYSK_JUNCTION)

    x_sq, y_sq = x0 + L, y0

    # JJ square
    add_rect(parts, (x_sq, y_sq - s / 2), (x_sq + s, y_sq + s / 2), cfg.LAYER_JJ)

    # ── Right extensions ──────────────────────────────────────────────────
    EXT1_H = 0.2   # height of first right extension
    EXT2_W = 0.1   # width of CAP1 strip
    EXT3_W = 0.8   # width of CAP2 wide bar

    add_rect(
        parts,
        (x_sq + s,           y_sq - EXT1_H / 2),
        (x_sq + 2 * s,       y_sq + EXT1_H / 2),
        cfg.LAYER_BIYSK_JUNCTION,
    )
    add_rect(
        parts,
        (x_sq + 2 * s,              y_sq - s / 2),
        (x_sq + 2 * s + EXT2_W,    y_sq + s / 2),
        cfg.LAYER_CAP1,
    )
    add_rect(
        parts,
        (x_sq + 2 * s + EXT2_W,           y_sq - s / 2),
        (x_sq + 2 * s + EXT2_W + EXT3_W,  y_sq + s / 2),
        cfg.LAYER_CAP2,
    )

    # ── Top extensions (90° rotated copy of right extensions) ────────────
    EXT2_H = 0.1   # thickness of CAP1 strip going up
    EXT3_H = 0.8   # height of CAP2 bar going up

    xt, yt = x_sq, y_sq + s / 2   # base of top extensions

    add_rect(parts, (xt, yt),         (xt + s, yt + s),              cfg.LAYER_BIYSK_JUNCTION)
    add_rect(parts, (xt, yt + s),     (xt + s, yt + s + EXT2_H),    cfg.LAYER_CAP1)
    add_rect(parts, (xt, yt + s + EXT2_H), (xt + s, yt + s + EXT2_H + EXT3_H), cfg.LAYER_CAP2)

    # ── Downward lead ─────────────────────────────────────────────────────
    y_down_top    = y_sq - s / 2
    y_down_bottom = y_down_top - L

    add_rect(parts, (x_sq, y_down_bottom), (x_sq + s, y_down_top), cfg.LAYER_BIYSK_JUNCTION)

    return x_sq + s / 2, y_down_bottom


# ── Routed branches ───────────────────────────────────────────────────────────

def add_right_junction_branch(
    parts: list,
    start: tuple[float, float],
    cfg: Config,
) -> tuple[float, float]:
    """
    Right branch: wide taper (L1) that narrows into a Manhattan junction (L5/L10).

    Returns
    -------
    (downlead_cx, downlead_bottom) from the junction, passed to the snake route.
    """
    taper = gdspy.Path(cfg.TAPER_WIDTH, start)
    taper.segment(
        cfg.BRANCH_RIGHT, "+x",
        final_width=cfg.JUNCTION_LEAD_WIDTH,
        layer=cfg.LAYER_BRANCH,
    )
    parts.append(taper)

    # Reassign 1 µm at the narrow (right/end) end of this taper.
    clip_narrow_end(parts, taper, "+x", cfg)
    
    return add_manhattan_junction(parts, taper.x, taper.y, cfg)


def add_top_branch(
    parts: list,
    start: tuple[float, float],
    cfg: Config,
) -> None:
    """
    Top branch (L1): +y → left turn → −x → smooth taper → overlap pad.
    """
    path = gdspy.Path(cfg.TAPER_WIDTH, start)
    path.segment(cfg.BRANCH_UP_PRE_TURN, "+y", layer=cfg.LAYER_BRANCH)
    path.turn(cfg.TURN_RADIUS, "l", layer=cfg.LAYER_BRANCH, number_of_points=128)
    path.segment(cfg.BRANCH_LEFT_POST_TURN, "-x", layer=cfg.LAYER_BRANCH)
    parts.append(path)

    add_taper_pad(parts, path.x, path.y, "-x", cfg.LAYER_BRANCH, cfg)


def add_taper_segment(
    parts: list,
    start: tuple[float, float],
    direction: str,
    length: float,
    narrow_end: str,
    cfg: Config,
) -> tuple[float, float]:
    """
    Standalone linearly-tapered path segment (WIRE_WIDTH → TAPER_WIDTH, L1).

    This is the wedge shape used in the snake route and upper branch network
    to transition from a narrow lead onto a full-width branch.  The narrow
    end is always WIRE_WIDTH; the wide end is always TAPER_WIDTH.

    The 1 µm slice at the narrow tip is automatically re-assigned to
    LAYER_NARROW_END (layer 11) via clip_narrow_end / clip_start_narrow_end,
    matching the behaviour in layout.py.

    Parameters
    ----------
    start      : (x, y) centreline of the entry end of the segment
    direction  : '+x' | '-x' | '+y' | '-y' — direction of travel
    length     : taper length in µm
    narrow_end : 'start' — narrow at entry, widens toward exit
                 'end'   — wide at entry, narrows toward exit
                 (mirrors the two cases that appear in the real layout)

    Returns
    -------
    (exit_x, exit_y) — centreline exit point
    """
    if narrow_end not in ("start", "end"):
        raise ValueError(f"narrow_end must be 'start' or 'end'; got {narrow_end!r}")

    if narrow_end == "start":
        w0, w1 = cfg.WIRE_WIDTH, cfg.TAPER_WIDTH
    else:
        w0, w1 = cfg.TAPER_WIDTH, cfg.WIRE_WIDTH

    path = gdspy.Path(w0, start)
    path.segment(length, direction, final_width=w1, layer=cfg.LAYER_BRANCH)
    parts.append(path)

    if narrow_end == "start":
        # Narrow tip is at the START — use the start-clip helper
        clip_start_narrow_end(parts, direction, cfg.LAYER_BRANCH, cfg)
    else:
        # Narrow tip is at the END — use the standard end-clip helper
        clip_narrow_end(parts, path, direction, cfg)

    return path.x, path.y



def add_branch_segment(
    parts: list,
    start: tuple[float, float],
    direction: str,
    length: float,
    cfg: Config,
) -> tuple[float, float]:
    """
    Standalone straight branch segment (LAYER_BRANCH, TAPER_WIDTH wide).

    This is the same rectangular segment used between turns in the snake
    route and top branch — a uniform-width path on L1.

    Parameters
    ----------
    start     : (x, y) centreline entry point
    direction : '+x' | '-x' | '+y' | '-y'
    length    : segment length in µm

    Returns
    -------
    (exit_x, exit_y) — centreline exit point
    """
    path = gdspy.Path(cfg.TAPER_WIDTH, start)
    path.segment(length, direction, layer=cfg.LAYER_BRANCH)
    parts.append(path)
    return path.x, path.y



def add_turn(
    parts: list,
    start: tuple[float, float],
    entry_dir: str,
    turn_dir: str,
    cfg: Config,
) -> tuple[float, float]:
    """
    Standalone 90-degree turn arc (L1 / LAYER_BRANCH).

    Parameters
    ----------
    start     : (x, y) of the arc entry point (centreline)
    entry_dir : direction the wire travels INTO the turn
                '+x' | '-x' | '+y' | '-y'
    turn_dir  : 'l' (left / CCW) | 'r' (right / CW)

    Returns
    -------
    (exit_x, exit_y) — centreline endpoint of the arc exit.
    """
    # gdspy.Path.turn() uses the current direction implicitly from its
    # last segment.  We prime it with a zero-length segment in entry_dir
    # so the turn knows which way it is heading.
    path = gdspy.Path(cfg.TAPER_WIDTH, start)
    path.segment(0, entry_dir, layer=cfg.LAYER_BRANCH)
    path.turn(cfg.TURN_RADIUS, turn_dir,
              layer=cfg.LAYER_BRANCH, number_of_points=128)
    parts.append(path)
    return path.x, path.y


def add_snake_right_branch(
    parts: list,
    start: tuple[float, float],
    cfg: Config,
) -> None:
    """
    Snake right branch (L1): +x → left turn → +y → right turn → +x → taper pad.
    """
    path = gdspy.Path(cfg.TAPER_WIDTH, start)
    path.segment(10.0,  "+x", layer=cfg.LAYER_BRANCH)
    path.turn(cfg.TURN_RADIUS, "l", layer=cfg.LAYER_BRANCH, number_of_points=128)
    path.segment(17.0,  "+y", layer=cfg.LAYER_BRANCH)
    path.turn(cfg.TURN_RADIUS, "r", layer=cfg.LAYER_BRANCH, number_of_points=128)
    path.segment(20.0,  "+x", layer=cfg.LAYER_BRANCH)
    parts.append(path)

    add_taper_pad(parts, path.x, path.y, "+x", cfg.LAYER_BRANCH, cfg)