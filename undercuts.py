"""
undercuts.py — Cap and undercut patterns for Josephson-junction squares.

Each function appends geometry to *parts* in-place.

Naming convention
-----------------
add_top_caps      : symmetric double-layer cap above the square
add_side_caps     : symmetric double-layer cap to the right of the square
add_L_undercut_right : L-shaped undercut opening to the right
add_L_undercut_top   : L-shaped undercut opening upward
"""

from __future__ import annotations

import gdspy

from config import Config
from primitives import add_rect


def add_top_caps(parts: list, cx: float, cy: float, cfg: Config,
                 hx: float | None = None, hy: float | None = None) -> None:
    """Two-layer cap centred above the square at (cx, cy).

    hx : half-width  (defaults to cfg.SQUARE_SIZE / 2)
    hy : half-height (defaults to cfg.SQUARE_SIZE / 2)
    """
    if hx is None: hx = cfg.SQUARE_SIZE / 2
    if hy is None: hy = cfg.SQUARE_SIZE / 2
    left, right = cx - hx, cx + hx
    top = cy + hy

    add_rect(parts, (left, top),             (right, top + cfg.CAP_H),            cfg.LAYER_CAP1)
    add_rect(parts, (left, top + cfg.CAP_H), (right, top + cfg.CAP_H + cfg.CAP2_H), cfg.LAYER_CAP2)


def add_side_caps(parts: list, cx: float, cy: float, cfg: Config,
                  hx: float | None = None, hy: float | None = None) -> None:
    """Two-layer cap centred to the right of the square at (cx, cy).

    hx : half-width  (defaults to cfg.SQUARE_SIZE / 2)
    hy : half-height (defaults to cfg.SQUARE_SIZE / 2)
    """
    if hx is None: hx = cfg.SQUARE_SIZE / 2
    if hy is None: hy = cfg.SQUARE_SIZE / 2
    right = cx + hx
    bottom, top = cy - hy, cy + hy

    add_rect(parts, (right,             bottom), (right + cfg.CAP_H,             top), cfg.LAYER_CAP1)
    add_rect(parts, (right + cfg.CAP_H, bottom), (right + cfg.CAP_H + cfg.CAP2_H, top), cfg.LAYER_CAP2)


def add_L_undercut_right(parts: list, cx: float, cy: float, cfg: Config,
                         hx: float | None = None, hy: float | None = None) -> None:
    """
    L-shaped undercut that opens to the right of the square.
    CAP1 forms the L outline; CAP2 fills the interior minus the L.

    hx : half-width  (defaults to cfg.SQUARE_SIZE / 2)
    hy : half-height (defaults to cfg.SQUARE_SIZE / 2)
    The vertical extent of the L is driven by hy (square height - wire width).
    """
    if hx is None: hx = cfg.SQUARE_SIZE / 2
    if hy is None: hy = cfg.SQUARE_SIZE / 2
    right  = cx + hx
    bottom = cy - hy
    l_vert = hy * 2 - cfg.WIRE_WIDTH   # square_y - wire_width

    c1_v = gdspy.Rectangle(
        (right,             bottom),
        (right + cfg.CAP_H, bottom + l_vert),
        layer=cfg.LAYER_CAP1,
    )
    c1_h = gdspy.Rectangle(
        (right,              bottom + l_vert - cfg.CAP_H),
        (right + cfg.L_HORZ, bottom + l_vert),
        layer=cfg.LAYER_CAP1,
    )
    box = gdspy.Rectangle(
        (right,              bottom),
        (right + cfg.L_HORZ, bottom + l_vert),
        layer=cfg.LAYER_CAP2,
    )
    c2_fill = gdspy.boolean(box, [c1_v, c1_h], "not", layer=cfg.LAYER_CAP2)

    parts += [c1_v, c1_h, c2_fill]


def add_L_undercut_top(parts: list, cx: float, cy: float, cfg: Config,
                       hx: float | None = None, hy: float | None = None) -> None:
    """
    L-shaped undercut that opens upward from the square.
    CAP1 forms the L outline; CAP2 fills the interior minus the L.

    hx : half-width  (defaults to cfg.SQUARE_SIZE / 2)
    hy : half-height (defaults to cfg.SQUARE_SIZE / 2)
    The horizontal extent of the L is driven by hx (square width - wire width).
    """
    if hx is None: hx = cfg.SQUARE_SIZE / 2
    if hy is None: hy = cfg.SQUARE_SIZE / 2
    left   = cx - hx
    top    = cy + hy
    l_vert = hx * 2 - cfg.WIRE_WIDTH   # square_x - wire_width

    c1_v = gdspy.Rectangle(
        (left,                top),
        (left + l_vert,       top + cfg.CAP_H),
        layer=cfg.LAYER_CAP1,
    )
    c1_h = gdspy.Rectangle(
        (left + l_vert - cfg.CAP_H, top),
        (left + l_vert,              top + cfg.L_HORZ),
        layer=cfg.LAYER_CAP1,
    )
    box = gdspy.Rectangle(
        (left,          top),
        (left + l_vert, top + cfg.L_HORZ),
        layer=cfg.LAYER_CAP2,
    )
    c2_fill = gdspy.boolean(box, [c1_v, c1_h], "not", layer=cfg.LAYER_CAP2)

    parts += [c1_v, c1_h, c2_fill]