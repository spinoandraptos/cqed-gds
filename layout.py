"""
layout.py — Top-level assembly of the full device layout.

draw_structure() builds every component and returns a centred list of
gdspy geometry objects ready to be added to a GDS cell.

To add or remove a component, edit the clearly labelled sections below.
"""

from __future__ import annotations

import gdspy

from config import Config
from primitives import add_rect, add_taper_pad, clip_narrow_end
from components import (
    add_square_node,
    add_right_junction_branch,
    add_top_branch,
    add_snake_right_branch,
)


# ── Private helpers for start-end narrow taper clipping ─────────────────────

def _clip_at_start(
    parts: list,
    start_x: float,
    start_y: float,
    half_w: float,
    travel_direction: str,
    src_layer: int,
    cfg: Config,
) -> None:
    """
    Clip the 1 µm trapezoidal slice at the narrow (start) end of a taper
    whose last polygon was just appended to *parts*.

    Uses _taper_slice_polygon to build a true trapezoid that follows the
    slanted taper edges.  For a start-narrow taper the taper segment is
    always the FIRST polygon (index 0) of the Path, so we pass poly_idx=0.

    Parameters
    ----------
    start_x / start_y  : coordinates of the narrow (start) end (kept for
                          call-site clarity; geometry comes from polygon)
    half_w             : (unused — kept for API compatibility)
    travel_direction   : '+x' | '-x' | '+y' | '-y'
    src_layer          : GDS layer the taper was placed on
    cfg                : Config instance
    """
    # The narrow end is the START of the taper, opposite to travel_direction
    opposite = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
    narrow_end = opposite[travel_direction]

    poly = parts.pop()
    # For a start-narrow taper the taper polygon is the first segment (index 0)
    taper_pts = poly.polygons[0] if hasattr(poly, "polygons") else poly.points

    from primitives import _taper_slice_polygon, _expand_clip
    slice_pts = _taper_slice_polygon(taper_pts, narrow_end, cfg.NARROW_END_LENGTH)
    slice_pts = _expand_clip(slice_pts, narrow_end)
    clip_poly  = gdspy.Polygon(slice_pts)

    remainder = gdspy.boolean(poly, clip_poly, "not", layer=src_layer)
    sliver    = gdspy.boolean(poly, clip_poly, "and", layer=cfg.LAYER_NARROW_END)

    if remainder is not None:
        parts.append(remainder)
    if sliver is not None:
        parts.append(sliver)


def _clip_snake_narrow_end(
    parts: list,
    start_x: float,
    start_y: float,
    half_w: float,
    src_layer: int,
    cfg: Config,
) -> None:
    """Snake taper travels in -y so the narrow end is at the top (start_y)."""
    _clip_at_start(parts, start_x, start_y, half_w, "-y", src_layer, cfg)


def _clip_start_narrow_end(
    parts: list,
    start_x: float,
    start_y: float,
    half_w: float,
    travel_direction: str,
    src_layer: int,
    cfg: Config,
) -> None:
    """Generic start-narrow clip; delegates to _clip_at_start."""
    _clip_at_start(parts, start_x, start_y, half_w, travel_direction, src_layer, cfg)


def _build_three_square_chain(parts: list, cfg: Config) -> dict:
    """
    Place squares 1-2-3 with their inter-square leads.
    Returns a dict of key coordinates used by the rest of the layout.
    """
    h     = cfg.SQUARE_SIZE / 2
    biysk = cfg.LAYER_BIYSK_JUNCTION

    # ── Square 1 ─────────────────────────────────────────────────────────────
    x1, y1 = 0.0, 0.0
    add_square_node(parts, x1, y1, cap_style="top", undercut_style="right", cfg=cfg)

    # Downward stub from square 1 (connects to snake route below)
    lead1_y_end = y1 - h - cfg.L_SHORT
    add_rect(
        parts,
        (x1 - h, lead1_y_end),
        (x1 - h + cfg.WIRE_WIDTH, y1 - h),
        biysk,
    )

    # ── Square 2 ─────────────────────────────────────────────────────────────
    x2 = x1 + h + cfg.L_LONG + h
    y2 = y1 + cfg.SQUARE_SIZE - cfg.WIRE_WIDTH
    add_square_node(parts, x2, y2, cap_style="side", undercut_style="top", cfg=cfg)

    # Horizontal lead: square 1 → square 2
    add_rect(
        parts,
        (x1 + h, y1 + h - cfg.WIRE_WIDTH),
        (x2 - h, y1 + h),
        biysk,
    )

    # ── Square 3 ─────────────────────────────────────────────────────────────
    x3 = x2 + cfg.SQUARE_SIZE - cfg.WIRE_WIDTH
    y3 = y2 + h + cfg.L_LONG + h
    add_square_node(parts, x3, y3, cap_style="top", undercut_style="right", cfg=cfg)

    # Vertical lead: square 2 → square 3
    add_rect(
        parts,
        (x2 + h - cfg.WIRE_WIDTH, y2 + h),
        (x2 + h, y3 - h),
        biysk,
    )

    return {
        "x1": x1, "y1": y1,
        "x3": x3, "y3": y3,
        "lead1_y_end": lead1_y_end,
        "lead1_x": x1 - h + cfg.WIRE_WIDTH / 2,   # centreline x of downward stub
    }


def _build_upper_branch_network(
    parts: list,
    x3: float,
    y3: float,
    cfg: Config,
) -> tuple[float, float]:
    """
    Shared taper stub from square 3, then right-junction and top branches.
    Returns (downlead_cx, downlead_bottom) from the Manhattan junction.
    """
    h     = cfg.SQUARE_SIZE / 2
    biysk = cfg.LAYER_BIYSK_JUNCTION
    branch = cfg.LAYER_BRANCH

    # Short horizontal stub from square 3
    lead_out_x = x3 + h + cfg.L_SHORT
    add_rect(
        parts,
        (x3 + h, y3 + h - cfg.WIRE_WIDTH),
        (lead_out_x, y3 + h),
        biysk,
    )

    # Taper: narrow wire → wide branch width
    p_taper = gdspy.Path(cfg.WIRE_WIDTH, (lead_out_x, y3 + h - cfg.WIRE_WIDTH / 2))
    p_taper.segment(cfg.L_TAPER, "+x", final_width=cfg.TAPER_WIDTH, layer=branch)
    parts.append(p_taper)

    # Reassign 1 µm at the narrow (left/start) end of p_taper.
    # The narrow end is at x = lead_out_x; we clip the first 1 µm in +x.
    _clip_start_narrow_end(parts, lead_out_x, y3 + h - cfg.WIRE_WIDTH / 2,
                           cfg.WIRE_WIDTH / 2, "+x", branch, cfg)

    branch_start = (p_taper.x, p_taper.y)

    # Right branch + Manhattan junction
    downlead_cx, downlead_bottom = add_right_junction_branch(parts, branch_start, cfg)

    # Top branch with pad
    add_top_branch(parts, branch_start, cfg)

    return downlead_cx, downlead_bottom


def _build_snake_route(
    parts: list,
    lead1_x: float,
    lead1_y_end: float,
    downlead_cx: float,
    downlead_bottom: float,
    cfg: Config,
) -> None:
    """
    Snake route from square 1's downward stub.
    Tapers down → turns right → turns up → connects to junction downlead.
    Also spawns the snake-right branch with its pad.
    """
    branch = cfg.LAYER_BRANCH

    # ── Main snake path ───────────────────────────────────────────────────────
    snake_start_x = lead1_x
    snake_start_y = lead1_y_end
    p_snake = gdspy.Path(cfg.WIRE_WIDTH, (snake_start_x, snake_start_y))
    p_snake.segment(cfg.L_TAPER,              "-y", final_width=cfg.TAPER_WIDTH, layer=branch)
    p_snake.segment(1.0,                      "-y", layer=branch)
    p_snake.turn(cfg.TURN_RADIUS,  "l",       layer=branch, number_of_points=128)
    p_snake.segment(cfg.LEN_COMPENSATE,       "+x", layer=branch)
    p_snake.turn(cfg.TURN_RADIUS,  "l",       layer=branch, number_of_points=128)
    p_snake.segment(6.0,                      "+y", layer=branch)
    parts.append(p_snake)

    # Reassign 1 µm at the narrow (top) end of the snake taper.
    # The narrow end is at snake_start_y (the origin of the -y taper), so we
    # build the clip box manually and boolean it against the last polygon.
    _clip_snake_narrow_end(parts, snake_start_x, snake_start_y,
                           cfg.WIRE_WIDTH / 2, branch, cfg)

    # ── Upward taper connecting snake to junction downlead ────────────────────
    b_connect = gdspy.Path(cfg.TAPER_WIDTH, (p_snake.x, p_snake.y))

    dx = downlead_cx - p_snake.x
    b_connect.segment(abs(dx), "+x" if dx >= 0 else "-x", layer=branch)

    taper_len = downlead_bottom - b_connect.y
    direction = "+y" if taper_len >= 0 else "-y"
    b_connect.segment(
        abs(taper_len), direction,
        final_width=cfg.JUNCTION_LEAD_WIDTH,
        layer=branch,
    )
    parts.append(b_connect)

    # Reassign 1 µm at the narrow end of b_connect (the end touching the junction).
    # narrow_end points in *direction* (the taper narrows as it travels that way).
    clip_narrow_end(parts, b_connect, direction, cfg)

    # ── Snake right branch with pad ───────────────────────────────────────────
    add_snake_right_branch(parts, (p_snake.x, p_snake.y), cfg)


def draw_structure() -> list:
    """
    Assemble the full layout and return a centred list of gdspy polygons.

    Structure overview
    ------------------
    1. Three-square chain (squares 1 → 2 → 3) with inter-square leads
    2. Upper branch network: taper stub → right junction branch + top branch
    3. Snake route: from square 1 downward → loop right → up to junction
    """
    parts: list = []
    cfg = Config()

    # ── 1. Three-square chain ────────────────────────────────────────────────
    coords = _build_three_square_chain(parts, cfg)

    # ── 2. Upper branch network ──────────────────────────────────────────────
    downlead_cx, downlead_bottom = _build_upper_branch_network(
        parts, coords["x3"], coords["y3"], cfg
    )

    # ── 3. Snake route ───────────────────────────────────────────────────────
    _build_snake_route(
        parts,
        lead1_x=coords["lead1_x"],
        lead1_y_end=coords["lead1_y_end"],
        downlead_cx=downlead_cx,
        downlead_bottom=downlead_bottom,
        cfg=cfg,
    )

    # ── Centre everything on (0, 0) ──────────────────────────────────────────
    union = gdspy.boolean(parts, None, "or")
    bb    = union.get_bounding_box()
    dx    = -(bb[0][0] + bb[1][0]) / 2
    dy    = -(bb[0][1] + bb[1][1]) / 2

    return [p.translate(dx, dy) for p in parts]