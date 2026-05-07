"""
ui/ruler_overlay.py — Interactive measurement ruler for GDS Canvas Designer.

Usage
-----
  Press M (or click the ruler toolbar button) to enter RULER mode.
  Click-drag on the canvas to draw a ruler between two points.
  The ruler displays:
    • Total distance in µm
    • Horizontal (ΔX) and vertical (ΔY) components
    • Tick marks every 1 µm (major) and 0.1 µm (minor)
  ESC or pressing M again clears the ruler and returns to SELECT mode.
  Any subsequent M-drag replaces the previous ruler.

Design notes
------------
  - RulerItem is a single QGraphicsItem with ItemIgnoresTransformations=False
    so it lives in scene (DBU) space and scales with zoom.  Text labels and
    end-caps use a secondary cosmetic pen so they stay readable at all zooms.
  - The ruler is NOT part of the model / undo stack — it is a pure visual aid.
  - Snap to grid is applied to both endpoints.
"""

from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QFontMetrics
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene

from core.model import dbu_to_um, um_to_dbu

# ── Visual constants ──────────────────────────────────────────────────────────

_LINE_COLOR     = "#f59e0b"   # amber — stands out on dark canvas
_TEXT_COLOR     = "#fef3c7"   # light amber for labels
_TEXT_BG_COLOR  = "#1e293b"   # dark slate background behind labels
_ENDCAP_COLOR   = "#f59e0b"
_TICK_COLOR     = "#fbbf24"

_LINE_WIDTH_PX  = 1.5
_ENDCAP_PX      = 8.0         # height of end-cap cross-bars in screen pixels
_MAJOR_TICK_PX  = 6.0         # 1 µm ticks
_MINOR_TICK_PX  = 3.0         # 0.1 µm ticks
_FONT_SIZE_PX   = 11
_LABEL_PAD_PX   = 4           # padding around text background rect

_MINOR_TICK_DBU = um_to_dbu(0.1)
_MAJOR_TICK_DBU = um_to_dbu(1.0)

# Only draw ticks when the ruler is long enough that they won't be crowded.
# Below this scene length (DBU) skip ticks entirely.
_MIN_TICK_LENGTH_DBU = um_to_dbu(0.5)


class RulerItem(QGraphicsItem):
    """
    A single interactive ruler drawn between two scene-space endpoints.

    Both endpoints are in DBU (scene coordinates).  The item recomputes its
    bounding rect dynamically so Qt clips and repaints it correctly.

    Call update_end(pt, port_label) while the user is dragging to preview,
    then commit(pt, port_label) once they release.

    port_label_start / port_label_end are optional strings shown next to the
    endpoint dot when the ruler snapped to a port (e.g. "out  [BranchSeg]").
    """

    def __init__(self, start: QPointF, scene: QGraphicsScene,
                 port_label_start: Optional[str] = None) -> None:
        super().__init__()
        self._start = start
        self._end   = start
        self._scene = scene

        self._port_label_start: Optional[str] = port_label_start
        self._port_label_end:   Optional[str] = None

        self.setZValue(20)    # above everything else
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,    False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    # ── Public API ────────────────────────────────────────────────────────────

    def update_end(self, end: QPointF,
                   port_label: Optional[str] = None) -> None:
        self.prepareGeometryChange()
        self._end            = end
        self._port_label_end = port_label
        self.update()

    def commit(self, end: QPointF,
               port_label: Optional[str] = None) -> None:
        self.prepareGeometryChange()
        self._end            = end
        self._port_label_end = port_label
        self.update()

    # ── QGraphicsItem interface ───────────────────────────────────────────────

    def boundingRect(self) -> QRectF:
        # Generous padding (80 px worth of scene units at an assumed zoom of 1)
        # so labels and tick marks are never clipped.
        pad = um_to_dbu(5.0)
        x0  = min(self._start.x(), self._end.x()) - pad
        y0  = min(self._start.y(), self._end.y()) - pad
        x1  = max(self._start.x(), self._end.x()) + pad
        y1  = max(self._start.y(), self._end.y()) + pad
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        s = self._start
        e = self._end

        dx_dbu = e.x() - s.x()
        dy_dbu = e.y() - s.y()
        length_dbu = math.hypot(dx_dbu, dy_dbu)

        if length_dbu < 1:
            return   # nothing to draw yet

        # ── Determine screen scale (DBU → pixels) ─────────────────────────────
        # We need this to draw fixed-pixel end-caps and ticks while the line
        # itself lives in scene space.
        t = painter.transform()
        # QTransform.m11() / m22() give the x/y scale factors.
        scale_x = math.hypot(t.m11(), t.m21())   # px per DBU
        scale_y = math.hypot(t.m12(), t.m22())
        scale   = (scale_x + scale_y) / 2.0
        if scale < 1e-9:
            scale = 1e-9

        # Convert fixed pixel sizes to DBU for this zoom level
        endcap_dbu = _ENDCAP_PX  / scale
        tick_major = _MAJOR_TICK_PX / scale
        tick_minor = _MINOR_TICK_PX / scale

        # Unit vector along and perpendicular to the ruler
        ux = dx_dbu / length_dbu
        uy = dy_dbu / length_dbu
        nx = -uy          # left-normal (perpendicular)
        ny =  ux

        # ── Main line ─────────────────────────────────────────────────────────
        line_pen = QPen(QColor(_LINE_COLOR), _LINE_WIDTH_PX)
        line_pen.setCosmetic(True)
        painter.setPen(line_pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(s, e)

        # ── End caps ──────────────────────────────────────────────────────────
        cap_pen = QPen(QColor(_ENDCAP_COLOR), _LINE_WIDTH_PX + 0.5)
        cap_pen.setCosmetic(True)
        painter.setPen(cap_pen)
        for pt in (s, e):
            painter.drawLine(
                QPointF(pt.x() + nx * endcap_dbu, pt.y() + ny * endcap_dbu),
                QPointF(pt.x() - nx * endcap_dbu, pt.y() - ny * endcap_dbu),
            )

        # ── Tick marks ────────────────────────────────────────────────────────
        if length_dbu >= _MIN_TICK_LENGTH_DBU:
            tick_pen = QPen(QColor(_TICK_COLOR), 0.8)
            tick_pen.setCosmetic(True)
            painter.setPen(tick_pen)

            # Walk along the ruler at minor-tick intervals
            step = _MINOR_TICK_DBU
            t_pos = step
            while t_pos < length_dbu - step * 0.5:
                is_major = abs(round(t_pos / _MAJOR_TICK_DBU) * _MAJOR_TICK_DBU - t_pos) < step * 0.01
                half = tick_major / 2 if is_major else tick_minor / 2
                px = s.x() + ux * t_pos
                py = s.y() + uy * t_pos
                painter.drawLine(
                    QPointF(px + nx * half, py + ny * half),
                    QPointF(px - nx * half, py - ny * half),
                )
                t_pos += step

        # ── Labels ────────────────────────────────────────────────────────────
        dx_um     = dbu_to_um(int(dx_dbu))
        dy_um     = dbu_to_um(int(dy_dbu))
        total_um  = math.hypot(dx_um, dy_um)

        # Total distance label — centred on the ruler, offset perpendicular
        total_text = f"{total_um:.3f} µm"
        comp_text  = f"ΔX={dx_um:+.3f}  ΔY={dy_um:+.3f} µm"

        mid = QPointF((s.x() + e.x()) / 2.0, (s.y() + e.y()) / 2.0)

        # Offset label above the line (in screen space, translated back to scene)
        label_offset_dbu = (16 + _LABEL_PAD_PX) / scale

        self._draw_label(painter, scale, mid,
                         nx * label_offset_dbu, ny * label_offset_dbu,
                         total_text, primary=True)
        self._draw_label(painter, scale, mid,
                         -nx * (label_offset_dbu * 1.8), -ny * (label_offset_dbu * 1.8),
                         comp_text, primary=False)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _draw_label(self, painter: QPainter, scale: float,
                    anchor: QPointF, ox: float, oy: float,
                    text: str, primary: bool) -> None:
        """
        Draw a text label with a dark background at (anchor + offset).
        The label is drawn in scene space but font size is fixed in pixels.
        """
        font = QFont("monospace")
        font.setPixelSize(_FONT_SIZE_PX + (2 if primary else 0))
        painter.setFont(font)

        fm     = QFontMetrics(font)
        tw     = fm.horizontalAdvance(text)
        th     = fm.height()
        pad    = _LABEL_PAD_PX

        # Convert text pixel size back to scene units
        tw_dbu = tw  / scale
        th_dbu = th  / scale
        pd_dbu = pad / scale

        cx = anchor.x() + ox
        cy = anchor.y() + oy

        # Background rect
        bg_rect = QRectF(
            cx - tw_dbu / 2 - pd_dbu,
            cy - th_dbu / 2 - pd_dbu,
            tw_dbu + pd_dbu * 2,
            th_dbu + pd_dbu * 2,
        )
        bg_color = QColor(_TEXT_BG_COLOR)
        bg_color.setAlpha(210)
        painter.setBrush(QBrush(bg_color))
        border_pen = QPen(QColor(_LINE_COLOR), 0.5)
        border_pen.setCosmetic(True)
        painter.setPen(border_pen)
        painter.drawRoundedRect(bg_rect, pd_dbu, pd_dbu)

        # Text — we scale the painter temporarily so font pixels map correctly
        painter.save()
        painter.translate(cx, cy)
        painter.scale(1.0 / scale, 1.0 / scale)
        painter.setPen(QPen(QColor(_TEXT_COLOR)))
        painter.drawText(
            QRectF(-tw / 2.0 - pad, -th / 2.0 - pad, tw + pad * 2, th + pad * 2),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()