"""
primitives.py — Low-level shape helpers.

Each function appends one or more gdspy objects to *parts* and returns None,
unless it needs to return a computed coordinate (noted in the docstring).
"""

from __future__ import annotations

import numpy as np
import gdspy

from config import Config


# ── Basic rectangles / squares ───────────────────────────────────────────────

def add_rect(parts: list, p1: tuple, p2: tuple, layer: int) -> None:
    """Append an axis-aligned rectangle."""
    parts.append(gdspy.Rectangle(p1, p2, layer=layer))


def add_square(parts: list, cx: float, cy: float, size: float, layer: int) -> None:
    """Append a square centred at (cx, cy)."""
    h = size / 2
    add_rect(parts, (cx - h, cy - h), (cx + h, cy + h), layer)


# ── Smooth (cosine) tapers ───────────────────────────────────────────────────

def smooth_taper(
    parts: list,
    x0: float,
    y0: float,
    length: float,
    w0: float,
    w1: float,
    direction: str,
    layer: int,
    n: int = 128,
) -> None:
    """
    Append a cosine-interpolated taper polygon.

    Parameters
    ----------
    x0, y0   : start position (centreline)
    length    : taper length along *direction*
    w0, w1    : start / end half-width
    direction : '+x' | '-x' | '+y' | '-y'
    layer     : GDS layer
    n         : number of polygon vertices per edge
    """
    t = np.linspace(0, 1, n)
    s = 0.5 * (1 - np.cos(np.pi * t))
    widths = w0 + (w1 - w0) * s

    if direction in ("+x", "-x"):
        sign = 1 if direction == "+x" else -1
        x = x0 + sign * length * t
        y = np.full_like(t, y0)
        upper = np.column_stack([x, y + widths / 2])
        lower = np.column_stack([x[::-1], (y - widths / 2)[::-1]])

    elif direction in ("+y", "-y"):
        sign = 1 if direction == "+y" else -1
        x = np.full_like(t, x0)
        y = y0 + sign * length * t
        upper = np.column_stack([x - widths / 2, y])
        lower = np.column_stack([(x + widths / 2)[::-1], y[::-1]])

    else:
        raise ValueError(f"direction must be '+x', '-x', '+y', or '-y'; got {direction!r}")

    parts.append(gdspy.Polygon(np.vstack([upper, lower]), layer=layer))


def _taper_slice_polygon(
    taper_pts: np.ndarray,
    narrow_end: str,
    length: float,
) -> np.ndarray:
    """
    Return a 4-vertex trapezoidal polygon that is the first *length* µm
    of a linear (4-vertex) taper measured from its narrow end, preserving
    the slanted edges exactly.

    gdspy.Path always produces a 4-vertex trapezoid. For a +x taper the
    winding order is [top-left, bot-left, bot-right, top-right], so the
    two edges are vertex 0↔3 (top) and vertex 1↔2 (bottom).  The narrow
    pair is whichever two vertices share the extreme axis value.

    We identify the two edges by finding which wide-end vertex is connected
    to which narrow-end vertex (they share an edge = they are adjacent in
    the polygon winding).

    Parameters
    ----------
    taper_pts  : (4, 2) array of polygon vertices
    narrow_end : '+x' | '-x' | '+y' | '-y'  — direction of the narrow end
    length     : distance from narrow end to cut line (µm)

    Returns
    -------
    (4, 2) array defining the trapezoidal slice
    """
    axis  = 0 if narrow_end in ("+x", "-x") else 1
    sign  = 1 if narrow_end in ("+x", "+y") else -1

    vals       = taper_pts[:, axis]
    narrow_val = vals.max() if sign == 1 else vals.min()
    wide_val   = vals.min() if sign == 1 else vals.max()

    n_idx = np.where(np.isclose(vals, narrow_val))[0]  # indices of narrow-end vertices
    w_idx = np.where(np.isclose(vals, wide_val))[0]    # indices of wide-end vertices

    n = len(taper_pts)  # always 4

    # For each narrow vertex, find its wide partner by adjacency in the winding
    # (adjacent means index differs by 1 mod n).
    pairs = []  # list of (narrow_pt, wide_pt)
    for ni in n_idx:
        for wi in w_idx:
            if (ni - wi) % n == 1 or (wi - ni) % n == 1:
                pairs.append((taper_pts[ni], taper_pts[wi]))
                break

    if len(pairs) != 2:
        # Fallback: just zip them in order (shouldn't happen for valid tapers)
        pairs = list(zip(taper_pts[n_idx], taper_pts[w_idx]))

    total_len = abs(wide_val - narrow_val)
    t = length / total_len  # fractional cut position from narrow end

    # Build the 4-vertex slice
    n0, w0 = pairs[0]
    n1, w1 = pairs[1]
    cut0 = n0 + t * (w0 - n0)
    cut1 = n1 + t * (w1 - n1)

    return np.array([n0, n1, cut1, cut0])


def _expand_clip(pts: np.ndarray, narrow_end: str, eps: float = 1e-4) -> np.ndarray:
    """
    Expand a trapezoidal clip polygon slightly outward so that floating-point
    imprecision doesn't leave a hairline layer-1 outline after boolean subtraction.

    The expansion pushes:
    - the narrow-end edge outward by *eps* (past the taper tip)
    - the cut edge outward by *eps* (into the remainder, so no gap at the seam)
    - each slanted edge outward by *eps* perpendicular to its direction

    Parameters
    ----------
    pts        : (4, 2) array — output of _taper_slice_polygon [n0, n1, cut1, cut0]
    narrow_end : '+x' | '-x' | '+y' | '-y'
    eps        : expansion amount in µm (default 0.1 nm, invisible at any scale)
    """
    pts = pts.copy().astype(float)
    axis  = 0 if narrow_end in ("+x", "-x") else 1
    perp  = 1 - axis
    sign  = 1 if narrow_end in ("+x", "+y") else -1

    # Push the two narrow-end vertices (n0=pts[0], n1=pts[1]) outward along axis
    pts[0, axis] += sign * eps
    pts[1, axis] += sign * eps

    # Push the two cut vertices (cut1=pts[2], cut0=pts[3]) inward along axis
    # (away from narrow end = into the remainder) so the seam has no gap
    pts[2, axis] -= sign * eps
    pts[3, axis] -= sign * eps

    # Push n0/cut0 outward along perp axis (top edge)
    # and n1/cut1 inward along perp axis (bottom edge)
    # Determine which pair is "top" vs "bottom" by perp coordinate
    if pts[0, perp] >= pts[1, perp]:
        pts[0, perp] += eps   # n0 is top → expand up
        pts[3, perp] += eps   # cut0 matches n0's edge
        pts[1, perp] -= eps   # n1 is bottom → expand down
        pts[2, perp] -= eps   # cut1 matches n1's edge
    else:
        pts[0, perp] -= eps
        pts[3, perp] -= eps
        pts[1, perp] += eps
        pts[2, perp] += eps

    return pts


def clip_narrow_end(
    parts: list,
    path: gdspy.Path,
    narrow_end: str,
    cfg: Config,
) -> None:
    """
    Reassign the 1 µm trapezoidal slice at the narrow end of the last
    polygon in *parts* (a gdspy.Path) to LAYER_NARROW_END (layer 11).

    The slice follows the taper's own slanted edges — not a rectangle —
    so the layer-11 region is a true trapezoid matching the taper geometry.

    The path object must have been appended to *parts* immediately before
    this call.  This function pops it, splits off the slice, re-appends
    the remainder on the original layer, and appends the slice on layer 11.

    Parameters
    ----------
    path       : the gdspy.Path just appended to *parts*
    narrow_end : '+x' | '-x' | '+y' | '-y' — which end is narrow
    cfg        : Config instance
    """
    poly      = parts.pop()
    src_layer = poly.layers[0] if hasattr(poly, "layers") else poly.layer

    # Only the last polygon of a multi-segment Path is the taper segment
    taper_pts = poly.polygons[-1] if hasattr(poly, "polygons") else poly.points

    slice_pts = _taper_slice_polygon(taper_pts, narrow_end, cfg.NARROW_END_LENGTH)
    slice_pts = _expand_clip(slice_pts, narrow_end)
    clip_poly  = gdspy.Polygon(slice_pts)

    remainder = gdspy.boolean(poly, clip_poly, "not", layer=src_layer)
    sliver    = gdspy.boolean(poly, clip_poly, "and", layer=cfg.LAYER_NARROW_END)

    if remainder is not None:
        parts.append(remainder)
    if sliver is not None:
        parts.append(sliver)


def add_taper_pad(
    parts: list,
    x0: float,
    y0: float,
    direction: str,
    layer: int,
    cfg: Config,
) -> None:
    """
    Append a smooth taper followed by a rectangular overlap pad.
    Taper runs from TAPER_WIDTH → FINAL_TAPER_WIDTH; pad overlaps the thick lead.
    """
    smooth_taper(
        parts,
        x0=x0, y0=y0,
        length=cfg.FINAL_TAPER_LENGTH,
        w0=cfg.TAPER_WIDTH, w1=cfg.FINAL_TAPER_WIDTH,
        direction=direction,
        layer=layer,
    )

    L = cfg.FINAL_TAPER_LENGTH
    W = cfg.FINAL_TAPER_WIDTH
    P = cfg.FINAL_PAD_LENGTH

    offsets = {
        "+x": ((x0 + L,     y0 - W / 2), (x0 + L + P, y0 + W / 2)),
        "-x": ((x0 - L - P, y0 - W / 2), (x0 - L,     y0 + W / 2)),
        "+y": ((x0 - W / 2, y0 + L),     (x0 + W / 2, y0 + L + P)),
        "-y": ((x0 - W / 2, y0 - L - P), (x0 + W / 2, y0 - L)),
    }
    p1, p2 = offsets[direction]
    add_rect(parts, p1, p2, layer)