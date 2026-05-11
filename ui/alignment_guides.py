"""
ui/alignment_guides.py — Smart alignment guide overlay for CanvasScene.

Behaviour (Photoshop / PowerPoint-style):
  - While dragging, thin coloured lines snap across the full viewport whenever
    a moving item's edge or centre aligns (within SNAP_THRESHOLD DBU) with any
    stationary item's edge or centre.
  - Lines are cosmetic (zoom-immune, always 1 px wide) and never affect the
    model or the command stack.
  - Guides are cleared instantly on mouse-release.

Integration — three steps in canvas_scene.py
  1.  from ui.alignment_guides import AlignmentGuideOverlay
      (add to the existing import block)

  2.  In CanvasScene.__init__, after self._ruler_item is initialised:
          self._align_guides = AlignmentGuideOverlay(self)

  3.  In CanvasScene._on_unified_move, just before the final `self.update()` /
      return (or right after the port-snap block, before the method ends):
          self._align_guides.update_guides(
              self._orig_comp_positions,
              self._orig_group_positions,
          )

  4.  In CanvasScene._on_unified_release, right after clear_all_port_highlights():
          self._align_guides.clear()

That is everything.  No changes to the model, commands, or any other file.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Optional

from PyQt6.QtCore import QLineF, QRectF, Qt
from PyQt6.QtGui import QColor, QPen
from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsScene

if TYPE_CHECKING:
    from ui.canvas_scene import CanvasScene, ComponentItem
    from core.model import ComponentGroup, Point


# ── Tuning constants ──────────────────────────────────────────────────────────

# How close (in DBU) two edges/centres must be before a guide fires.
# 500 DBU = 0.5 µm — feels natural; tighten if grids are small.
SNAP_THRESHOLD_DBU: int = 500

# Guide line colour and transparency.
_GUIDE_COLOR_V = "#38bdf8"   # cyan-blue  — vertical  guides
_GUIDE_COLOR_H = "#f472b6"   # pink       — horizontal guides
_GUIDE_ALPHA   = 220          # 0-255


# ── Helper: bounding-box key values ──────────────────────────────────────────

def _x_keys(x_min: int, x_max: int) -> List[int]:
    """Left edge, centre, right edge."""
    return [x_min, (x_min + x_max) // 2, x_max]

def _y_keys(y_min: int, y_max: int) -> List[int]:
    """Top edge, centre, bottom edge."""
    return [y_min, (y_min + y_max) // 2, y_max]


# ── AlignmentGuideOverlay ─────────────────────────────────────────────────────

class AlignmentGuideOverlay:
    """
    Manages a pool of QGraphicsLineItem guides on the parent CanvasScene.

    Usage
    -----
    overlay = AlignmentGuideOverlay(scene)

    # Every mouse-move during a drag:
    overlay.update_guides(orig_comp_positions, orig_group_positions)

    # On mouse-release:
    overlay.clear()
    """

    def __init__(self, scene: "CanvasScene") -> None:
        self._scene = scene
        self._v_lines: List[QGraphicsLineItem] = []   # vertical   (x=const)
        self._h_lines: List[QGraphicsLineItem] = []   # horizontal (y=const)

    # ── Public API ────────────────────────────────────────────────────────────

    def update_guides(
        self,
        orig_comp_positions: Dict["ComponentItem", "Point"],
        orig_group_positions: Dict[str, Dict[str, "Point"]],
    ) -> None:
        """
        Recompute and redraw guides.  Call on every mouseMoveEvent during drag.

        Parameters match the CanvasScene drag-state dicts exactly:
          orig_comp_positions  — {ComponentItem: original Point}
          orig_group_positions — {group_id: {comp_id: original Point}}
        """
        self.clear()

        scene = self._scene
        design = scene._design

        # ── Identify the set of moving component IDs ──────────────────────────
        moving_ids: set[str] = set()
        for item in orig_comp_positions:
            moving_ids.add(item._comp.id)
        for origins in orig_group_positions.values():
            moving_ids.update(origins.keys())

        if not moving_ids:
            return

        # ── Gather moving bboxes (current live positions) ─────────────────────
        moving_x_vals: List[int] = []
        moving_y_vals: List[int] = []
        for cid in moving_ids:
            comp = design.get(cid)
            if comp is None:
                continue
            bb = comp.bbox
            moving_x_vals.extend(_x_keys(bb.x_min, bb.x_max))
            moving_y_vals.extend(_y_keys(bb.y_min, bb.y_max))

        if not moving_x_vals:
            return

        # ── Gather stationary bboxes ──────────────────────────────────────────
        stationary_x: List[int] = []
        stationary_y: List[int] = []
        for comp in design.components:
            if comp.id in moving_ids:
                continue
            bb = comp.bbox
            stationary_x.extend(_x_keys(bb.x_min, bb.x_max))
            stationary_y.extend(_y_keys(bb.y_min, bb.y_max))

        if not stationary_x:
            return

        # ── Compute scene extent for guide line length ────────────────────────
        sr = scene.sceneRect()
        x_lo, x_hi = sr.left(),  sr.right()
        y_lo, y_hi = sr.top(),   sr.bottom()

        thr = SNAP_THRESHOLD_DBU

        # ── Vertical guides (shared X) ────────────────────────────────────────
        fired_vx: set[int] = set()
        for mx in moving_x_vals:
            for sx in stationary_x:
                if abs(mx - sx) <= thr and sx not in fired_vx:
                    fired_vx.add(sx)
                    self._add_v_line(sx, y_lo, y_hi)

        # ── Horizontal guides (shared Y) ──────────────────────────────────────
        fired_hy: set[int] = set()
        for my in moving_y_vals:
            for sy in stationary_y:
                if abs(my - sy) <= thr and sy not in fired_hy:
                    fired_hy.add(sy)
                    self._add_h_line(sy, x_lo, x_hi)

    def clear(self) -> None:
        """Remove all guide lines from the scene."""
        for line in self._v_lines + self._h_lines:
            self._scene.removeItem(line)
        self._v_lines.clear()
        self._h_lines.clear()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _make_pen(self, color_hex: str) -> QPen:
        color = QColor(color_hex)
        color.setAlpha(_GUIDE_ALPHA)
        pen = QPen(color, 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)           # always 1 px regardless of zoom
        pen.setDashPattern([6, 4])      # subtle dash so it doesn't obscure geometry
        return pen

    def _add_v_line(self, x: int, y_lo: float, y_hi: float) -> None:
        line = QGraphicsLineItem(float(x), y_lo, float(x), y_hi)
        line.setPen(self._make_pen(_GUIDE_COLOR_V))
        line.setZValue(50)              # float above all component items
        line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(line)
        self._v_lines.append(line)

    def _add_h_line(self, y: int, x_lo: float, x_hi: float) -> None:
        line = QGraphicsLineItem(x_lo, float(y), x_hi, float(y))
        line.setPen(self._make_pen(_GUIDE_COLOR_H))
        line.setZValue(50)
        line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(line)
        self._h_lines.append(line)
