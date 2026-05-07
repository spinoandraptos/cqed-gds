"""
ui/panels.py — Dock panel widgets: Component Palette and Properties Panel.

Phase 2 changes:
  - ComponentPalette: Polygon and Path buttons now enabled; subtitles updated.
    Signal changed to place_mode_requested(kind, layer) — emits the mode to enter,
    not "place now at view center". The scene's state machine handles actual placement.
  - PropertiesPanel: shows vertex count for polygons, path width for paths.
    Added an editable layer spinbox (read-only in Phase 1 → editable in Phase 2).
    layer_change_requested(comp_id, new_layer) signal for undo-aware edit.
    geometry_change_requested(comp_id, field, value_dbu) signal for width/height/path_width edits.
"""

from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QSpinBox, QDoubleSpinBox, QComboBox,
    QFrame, QPushButton, QSizePolicy, QScrollArea,
    QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QPoint, QByteArray, QPointF
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter, QPen, QPainterPath, QDrag, QMouseEvent

from ui.theme import Colors, Fonts, Geometry
from core.model import GDSComponent, ComponentKind, PortSide, dbu_to_um, um_to_dbu
from core.cell_library import CELL_BY_ID


# ── Helper widgets ────────────────────────────────────────────────────────────

class SectionLabel(QLabel):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text.upper(), parent)
        self.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            font-size: {Fonts.SIZE_XS}px;
            letter-spacing: 1.5px;
            padding: 8px 0 4px 0;
        """)


class Separator(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setStyleSheet(f"color: {Colors.BG_BORDER};")
        self.setFixedHeight(1)


class ValueRow(QWidget):
    """Label + read-only value in a horizontal pair."""
    def __init__(self, label: str, value: str = "—", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)

        self._val = QLabel(value)
        self._val.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; "
            f"font-family: {Fonts.MONO_FAMILY};"
        )
        layout.addWidget(lbl)
        layout.addWidget(self._val)
        layout.addStretch()

    def set_value(self, v: str) -> None:
        self._val.setText(v)


def _layer_icon(layer: int, size: int = 14) -> QIcon:
    color = QColor(Colors.LAYER_COLORS[layer % len(Colors.LAYER_COLORS)])
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(color)
    color.setAlpha(180)
    p.setPen(color.lighter(150))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 2, 2)
    p.end()
    return QIcon(pix)


def _shape_icon(kind: "ComponentKind", size: int = 36) -> QPixmap:
    """
    Draw a clear, recognisable icon for each primitive shape.
    Returns a QPixmap with transparent background.

    Rectangle  — filled rounded rect with a bright border
    Polygon    — filled 5-sided polygon
    Path       — open dashed polyline with round caps
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    accent      = QColor("#7c3aed")
    accent_fill = QColor("#7c3aed")
    accent_fill.setAlpha(55)
    pen_bright  = QPen(QColor("#a78bfa"), 1.6)
    m = 4   # margin

    if kind == ComponentKind.RECTANGLE:
        p.setBrush(accent_fill)
        p.setPen(pen_bright)
        p.drawRoundedRect(m, m + 4, size - m * 2, size - m * 2 - 4, 2, 2)

    elif kind == ComponentKind.POLYGON:
        import math
        cx, cy, r = size / 2, size / 2 + 1, size / 2 - m
        pts = []
        for i in range(5):
            angle = math.radians(-90 + i * 72)
            pts.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))
        path = QPainterPath()
        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        p.setBrush(accent_fill)
        p.setPen(pen_bright)
        p.drawPath(path)

    elif kind == ComponentKind.PATH:
        # Dashed open polyline: three segments in a gentle Z shape
        pen = QPen(QColor("#a78bfa"), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([3, 2])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        pts = [
            QPointF(m,          size - m - 4),
            QPointF(size * 0.35, m + 6),
            QPointF(size * 0.65, size - m - 4),
            QPointF(size - m,   m + 4),
        ]
        for i in range(len(pts) - 1):
            p.drawLine(pts[i], pts[i + 1])
        # Draw solid dots at vertices
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#a78bfa"))
        for pt in pts:
            p.drawEllipse(pt, 2.0, 2.0)

    p.end()
    return pix


class _ParamSpinBox(QDoubleSpinBox):
    """
    QDoubleSpinBox that keeps focus inside the properties panel after the user
    commits a value with Enter.

    The default QDoubleSpinBox behaviour on Enter is to confirm the value and
    then return focus to whichever widget had focus before — usually the canvas
    view.  The canvas view receiving focus triggers a click-through event that
    clears the scene selection, so the selected cell is deselected immediately
    after every parameter edit.

    Override keyPressEvent: on Return/Enter, emit editingFinished normally but
    then explicitly reclaim focus so it never reaches the canvas.
    """

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()
        super().keyPressEvent(event)


def _cell_icon(cell_id: str, size: int = 36) -> QPixmap:
    """
    Dispatch to the per-cell icon painter that matches the cell's actual geometry.
    Falls back to a generic rectangle icon for unknown cell_ids.
    """
    _painters = {
        "byisk_jj":         _icon_byisk_jj,
        "manhattan_jj":     _icon_manhattan_jj,
        "taper_segment":    _icon_taper_segment,
        "taper_pad":        _icon_taper_pad,
        "smooth_taper_pad": _icon_taper_pad,
        "branch_segment":   _icon_wire,
        "turn":             _icon_turn,
        "t_junction":       _icon_t_junction,
        "wire":             _icon_wire,
        "undercut_ring":    _icon_undercut_ring,
    }
    painter_fn = _painters.get(cell_id, _icon_taper_segment)
    return painter_fn(size)


# ── Per-cell icon painters ────────────────────────────────────────────────────
# Each function draws a miniature top-view of the cell's actual shape.
# Colours follow the layer palette: L1 branch=blue, L5 junction=violet,
# L10 JJ square=yellow, L4 cap=teal, L6 cap2=green.

def _pix(size: int) -> tuple:
    """Return (pix, painter) with antialiasing enabled."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pix, p


def _filled_poly(painter: QPainter, pts: list, fill: QColor, stroke: QColor, lw: float = 1.2):
    path = QPainterPath()
    path.moveTo(pts[0])
    for pt in pts[1:]:
        path.lineTo(pt)
    path.closeSubpath()
    painter.setBrush(fill)
    painter.setPen(QPen(stroke, lw))
    painter.drawPath(path)


def _icon_byisk_jj(size: int) -> QPixmap:
    """
    Square body (L5 violet) centred, with a cap strip on the top edge (L4 teal +
    L6 green) and an L-bracket on the right edge (L4 teal outline).
    Faithfully mirrors build_byisk_jj(cap_style='top', undercut_style='right').
    """
    import math
    pix, p = _pix(size)
    m = 4
    s = size - m * 2           # body occupies ~80% of icon
    bx = m + s * 0.18          # left of body
    by_bot = size - m - s * 0.18
    by_top = m + s * 0.18
    bw = s * 0.64              # body width
    bh = s * 0.55              # body height
    cap_h  = bh * 0.18         # cap strip thickness (CAP1)
    cap2_h = bh * 0.14         # outer cap (CAP2)
    l_w    = bh * 0.18         # L arm width (same as cap_h)
    l_reach = cap_h + cap2_h   # L arm extent outward

    # L5 body (violet)
    body_fill   = QColor("#7c3aed"); body_fill.setAlpha(160)
    body_stroke = QColor("#a78bfa")
    p.setBrush(body_fill)
    p.setPen(QPen(body_stroke, 1.2))
    p.drawRoundedRect(int(bx), int(by_top), int(bw), int(bh), 1, 1)

    # CAP1 strip above body (teal)
    cap1_fill = QColor("#0d9488"); cap1_fill.setAlpha(200)
    p.setBrush(cap1_fill)
    p.setPen(QPen(QColor("#5eead4"), 1.0))
    p.drawRect(int(bx), int(by_top - cap_h), int(bw), int(cap_h))

    # CAP2 strip above CAP1 (green)
    cap2_fill = QColor("#16a34a"); cap2_fill.setAlpha(200)
    p.setBrush(cap2_fill)
    p.setPen(QPen(QColor("#86efac"), 1.0))
    p.drawRect(int(bx), int(by_top - cap_h - cap2_h), int(bw), int(cap2_h))

    # L-undercut on right side: vertical arm + horizontal arm (teal outline)
    l_vert = bh - l_w           # vertical extent = body height minus wire width
    lx = bx + bw               # right edge of body
    ly_bot = by_top + bh        # bottom of body
    ly_top_arm = ly_bot - l_vert  # top of the L vertical arm

    p.setBrush(QColor(0, 0, 0, 0))
    lpen = QPen(QColor("#5eead4"), 1.5)
    lpen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(lpen)
    # Draw L as a polyline: bottom-right → top-right → top-left (the two outer edges)
    p.drawPolyline([
        QPointF(lx,             ly_bot),
        QPointF(lx + l_reach,   ly_bot),
        QPointF(lx + l_reach,   ly_top_arm),
        QPointF(lx,             ly_top_arm),
    ])

    p.end()
    return pix


def _icon_manhattan_jj(size: int) -> QPixmap:
    """
    Horizontal lead (L5 violet) → JJ square (L10 amber) → right arm (L4 teal + L6 green)
    and upward arm (L4 teal + L6 green). Faithfully mirrors build_manhattan_jj geometry.
    """
    pix, p = _pix(size)
    m = 3

    # Layout proportions (all in px, left-to-right = horizontal lead direction)
    total_w = size - m * 2
    sq_frac  = 0.18            # JJ square as fraction of total width
    lead_frac = 0.30           # horizontal lead
    ext_frac  = sq_frac        # right L5 continuation = 1 square-width
    e2_frac   = 0.07           # CAP1 strip
    e3_frac   = 0.18           # CAP2 bar

    lw = total_w * lead_frac
    sq = total_w * sq_frac
    e2 = total_w * e2_frac
    e3 = total_w * e3_frac

    lead_x  = m
    sq_x    = lead_x + lw
    ext_x   = sq_x + sq        # right L5 start
    cap1_x  = ext_x + sq
    cap2_x  = cap1_x + e2

    # Vertical centre for horizontal lead
    cy   = size / 2
    lead_hw = total_w * 0.07   # half-width of lead (thin)
    sq_hw   = sq / 2           # half-height of JJ square (= sq)

    # Colours
    c_l5   = QColor("#7c3aed"); c_l5.setAlpha(180)
    c_l10  = QColor("#d97706"); c_l10.setAlpha(220)   # amber
    c_cap1 = QColor("#0d9488"); c_cap1.setAlpha(200)  # teal
    c_cap2 = QColor("#16a34a"); c_cap2.setAlpha(200)  # green
    s_l5   = QColor("#a78bfa")
    s_l10  = QColor("#fcd34d")
    s_cap1 = QColor("#5eead4")
    s_cap2 = QColor("#86efac")

    # Horizontal lead (L5)
    p.setBrush(c_l5); p.setPen(QPen(s_l5, 0.8))
    p.drawRect(int(lead_x), int(cy - lead_hw), int(lw), int(lead_hw * 2))

    # JJ square (L10 amber)
    p.setBrush(c_l10); p.setPen(QPen(s_l10, 0.8))
    p.drawRect(int(sq_x), int(cy - sq_hw), int(sq), int(sq))

    # Right continuation L5
    p.setBrush(c_l5); p.setPen(QPen(s_l5, 0.8))
    p.drawRect(int(ext_x), int(cy - lead_hw), int(sq), int(lead_hw * 2))

    # Right CAP1 strip
    p.setBrush(c_cap1); p.setPen(QPen(s_cap1, 0.8))
    p.drawRect(int(cap1_x), int(cy - sq_hw), int(e2), int(sq))

    # Right CAP2 bar
    p.setBrush(c_cap2); p.setPen(QPen(s_cap2, 0.8))
    p.drawRect(int(cap2_x), int(cy - sq_hw), int(e3), int(sq))

    # Top continuation L5 (upward arm above JJ square)
    top_ext_y  = cy - sq_hw - sq
    p.setBrush(c_l5); p.setPen(QPen(s_l5, 0.8))
    p.drawRect(int(sq_x), int(top_ext_y), int(sq), int(sq))

    # Top CAP1
    top_cap1_y = top_ext_y - e2
    p.setBrush(c_cap1); p.setPen(QPen(s_cap1, 0.8))
    p.drawRect(int(sq_x), int(top_cap1_y), int(sq), int(e2))

    # Top CAP2
    top_cap2_y = top_cap1_y - e3
    p.setBrush(c_cap2); p.setPen(QPen(s_cap2, 0.8))
    p.drawRect(int(sq_x), int(top_cap2_y), int(sq), int(e3))

    # Downward lead (L5) below JJ square
    down_y = cy + sq_hw
    down_len = lw * 0.9
    p.setBrush(c_l5); p.setPen(QPen(s_l5, 0.8))
    p.drawRect(int(sq_x), int(down_y), int(sq), int(down_len))

    p.end()
    return pix


def _icon_taper_segment(size: int) -> QPixmap:
    """
    Trapezoid: narrow on left, wide on right (L1 blue), with a small
    contrasting slice at the narrow tip (L11 orange) — matches taper_segment geometry.
    """
    pix, p = _pix(size)
    m = 4
    W = size - m * 2
    H = size - m * 2

    # Narrow end half-height and wide end half-height
    nh = H * 0.10
    wh = H * 0.42

    # Main trapezoid (L1 blue)
    trap_pts = [
        QPointF(m,     size/2 - nh),
        QPointF(m,     size/2 + nh),
        QPointF(m + W, size/2 + wh),
        QPointF(m + W, size/2 - wh),
    ]
    c_l1 = QColor("#2563eb"); c_l1.setAlpha(160)
    _filled_poly(p, trap_pts, c_l1, QColor("#93c5fd"), 1.2)

    # Narrow-tip clip slice (L11, orange) — small rect at left edge
    clip_w = W * 0.14
    c_l11 = QColor("#ea580c"); c_l11.setAlpha(220)
    clip_pts = [
        QPointF(m,           size/2 - nh),
        QPointF(m,           size/2 + nh),
        QPointF(m + clip_w,  size/2 + nh * 1.4),
        QPointF(m + clip_w,  size/2 - nh * 1.4),
    ]
    _filled_poly(p, clip_pts, c_l11, QColor("#fed7aa"), 0.8)

    p.end()
    return pix


def _icon_taper_pad(size: int) -> QPixmap:
    """
    Two-stage shape (both L1 blue): cosine-curved taper + wide flat pad rectangle.

    The taper uses a cosine width profile (matching smooth_taper() in primitives.py
    and build_smooth_taper_pad() in cell_library.py) so it is visually distinct
    from the linear _icon_taper_segment.  The pad is a plain rectangle abutting
    the wide end of the taper.
    """
    import math
    pix, p = _pix(size)
    m = 4
    W = size - m * 2

    nh = W * 0.09   # narrow half-height (entry)
    wh = W * 0.38   # wide  half-height  (exit / pad)
    taper_w = W * 0.48   # taper portion pixel-width
    pad_w   = W * 0.42   # pad   portion pixel-width

    c_l1 = QColor("#2563eb"); c_l1.setAlpha(160)
    s_l1 = QColor("#93c5fd")

    # ── Cosine taper (N-segment polygon) ─────────────────────────────────────
    # w(t) = nh + (wh - nh) * 0.5 * (1 - cos(pi*t)),  t in [0,1]
    N = 24
    upper = [
        QPointF(m + taper_w * t,
                size/2 - (nh + (wh - nh) * 0.5 * (1 - math.cos(math.pi * t))))
        for t in (i / N for i in range(N + 1))
    ]
    lower = [
        QPointF(m + taper_w * t,
                size/2 + (nh + (wh - nh) * 0.5 * (1 - math.cos(math.pi * t))))
        for t in (i / N for i in range(N, -1, -1))
    ]
    taper_pts = upper + lower
    _filled_poly(p, taper_pts, c_l1, s_l1, 1.2)

    # ── Flat pad (rectangle) ─────────────────────────────────────────────────
    pad_pts = [
        QPointF(m + taper_w,            size/2 - wh),
        QPointF(m + taper_w,            size/2 + wh),
        QPointF(m + taper_w + pad_w,    size/2 + wh),
        QPointF(m + taper_w + pad_w,    size/2 - wh),
    ]
    _filled_poly(p, pad_pts, c_l1, s_l1, 1.2)

    p.end()
    return pix


def _icon_turn(size: int) -> QPixmap:
    """
    Quarter-circle arc band (L1 blue): entry from the left, exit downward.
    Drawn as a filled annular sector — matches build_turn(entry=+x, turn=r) appearance.
    """
    import math
    pix, p = _pix(size)
    m = 3

    # Arc parameters in pixel space
    # Centre of curvature at bottom-left of icon (entry from left → turn right → exit down)
    cx = m
    cy = size - m

    # Make the arc fill most of the icon
    r_mid  = (size - m * 2) * 0.72
    hw     = r_mid * 0.38   # band half-width

    r_out  = r_mid + hw
    r_in   = r_mid - hw

    # Sweep from 0° (right/east) to -90° (up/north) — entry +x, exit -y (upward in Qt)
    # In Qt coords Y increases downward, so: east=0°, north=-90°, south=+90°
    start_angle_deg = 0.0     # pointing right = entry direction +x
    end_angle_deg   = -90.0   # pointing up (Qt: north = negative Y = -90°)

    N = 24
    outer_pts = []
    inner_pts = []
    for i in range(N + 1):
        t = i / N
        angle = math.radians(start_angle_deg + (end_angle_deg - start_angle_deg) * t)
        ca, sa = math.cos(angle), math.sin(angle)
        outer_pts.append(QPointF(cx + r_out * ca, cy + r_out * sa))
        inner_pts.append(QPointF(cx + r_in  * ca, cy + r_in  * sa))

    fan_pts = outer_pts + list(reversed(inner_pts))
    c_l1 = QColor("#2563eb"); c_l1.setAlpha(160)
    _filled_poly(p, fan_pts, c_l1, QColor("#93c5fd"), 1.2)

    p.end()
    return pix

def _icon_wire(size: int) -> QPixmap:
    """
    Lead Segment icon (L1 blue): a plain horizontal rectangle matching
    build_wire(direction='+x') geometry.
    Port dots at left-centre and right-centre match the actual 'start' and
    'end' port positions (Point(0,0) and Point(tx*L, 0) in body-local coords,
    which land at the mid-height of each end face).
    """
    pix, p = _pix(size)
    m = 4
    hw = (size - m * 2) * 0.15   # half wire-width in pixels
    cy = size / 2

    c_l1   = QColor("#2563eb"); c_l1.setAlpha(160)
    s_l1   = QColor("#93c5fd")
    c_port = QColor("#93c5fd")

    # Wire body
    rect_pts = [
        QPointF(m,        cy - hw),
        QPointF(m,        cy + hw),
        QPointF(size - m, cy + hw),
        QPointF(size - m, cy - hw),
    ]
    _filled_poly(p, rect_pts, c_l1, s_l1, 1.2)

    # Port dots at left-centre and right-centre (entry and exit face midpoints)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c_port)
    dot_r = hw * 0.6
    p.drawEllipse(QPointF(m,        cy), dot_r, dot_r)
    p.drawEllipse(QPointF(size - m, cy), dot_r, dot_r)

    p.end()
    return pix


def _icon_t_junction(size: int) -> QPixmap:
    """
    Miniature top-view T-junction (L1 blue), stem_dir='+y'.

    Directly mirrors the build_t_junction polygon construction in Qt pixel
    space (y increases downward).  The cell origin is the stem entry point
    at the bottom-centre of the icon.

    For stem_dir='+y':
      fwd = (0,+1) in cell-frame, but in Qt y-down fwd is (0,-1) (upward).
      right_perp = (+1, 0),  left_perp = (-1, 0)  (unchanged — x is the same).

    Arc centres in Qt pixel space (stem origin = bottom-centre):
      r_cx = cx_mid + R,  r_cy = stem_y   (right arc centre)
      l_cx = cx_mid - R,  l_cy = stem_y   (left  arc centre)

    Inner arc angles (build_t_junction uses atan2 in math space; translated):
      right inner: a_start = atan2(-rpy,-rpx) = atan2(0,-1) = 180°
                   a_end   = atan2(-fy, fx)   = atan2(-1, 0) = 270°  (up in Qt)
      left  inner (reversed): from 270° back to 0°/360°

    Bar corners in Qt pixel space (y-down means fwd-y = -1):
      r_outer_corner = (r_cx + (R+hw),  r_cy - (R-hw))
      r_bar_top      = (r_cx + (R+hw),  r_cy - (R+hw))
      l_bar_top      = (l_cx - (R+hw),  l_cy - (R+hw))
      l_outer_corner = (l_cx - (R+hw),  l_cy - (R-hw))
    """
    import math
    pix, p = _pix(size)
    m = 3

    W      = size - m * 2
    hw     = W * 0.13
    R      = W * 0.32
    cx_mid = size / 2
    stem_y = size - m        # stem entry at bottom of icon (Qt y-down)

    r_cx, r_cy = cx_mid + R, stem_y
    l_cx, l_cy = cx_mid - R, stem_y

    N = 24

    def arc_pts(cx, cy, radius, a0_deg, a1_deg):
        pts = []
        for i in range(N + 1):
            t = i / N
            a = math.radians(a0_deg + (a1_deg - a0_deg) * t)
            pts.append(QPointF(cx + radius * math.cos(a),
                               cy + radius * math.sin(a)))
        return pts

    # inner_right: 180° → 270°  (left of r_centre → top of r_centre = upward in Qt)
    r_inner     = arc_pts(r_cx, r_cy, R - hw, 180, 270)
    # inner_left reversed: 270° → 360°
    l_inner_rev = arc_pts(l_cx, l_cy, R - hw, 270, 360)

    r_outer_corner = QPointF(r_cx + (R + hw), r_cy - (R - hw))
    r_bar_top      = QPointF(r_cx + (R + hw), r_cy - (R + hw))
    l_bar_top      = QPointF(l_cx - (R + hw), l_cy - (R + hw))
    l_outer_corner = QPointF(l_cx - (R + hw), l_cy - (R - hw))

    all_pts = (
        r_inner
        + [r_outer_corner, r_bar_top, l_bar_top, l_outer_corner]
        + l_inner_rev
    )

    path = QPainterPath()
    path.moveTo(all_pts[0])
    for pt in all_pts[1:]:
        path.lineTo(pt)
    path.closeSubpath()

    c_l1 = QColor("#2563eb"); c_l1.setAlpha(160)
    p.setBrush(c_l1)
    p.setPen(QPen(QColor("#93c5fd"), 1.2))
    p.drawPath(path)

    p.end()
    return pix


def _icon_undercut_ring(size: int) -> QPixmap:
    """
    Top-view icon for the undercut_ring cell.

    Draws a hollow square frame on L2 (orange-red) — four strips forming a
    perimeter ring with mitred corners — exactly as the ring appears on canvas.
    A faint interior background hints at the target cell it surrounds.
    """
    pix, p = _pix(size)
    m = 4
    t = max(2.0, size * 0.12)   # ring strip thickness in icon pixels

    outer_x = float(m)
    outer_y = float(m)
    outer_w = float(size - m * 2)
    outer_h = float(size - m * 2)

    # Faint interior fill (suggests the enclosed cell)
    interior_fill = QColor("#334155")
    interior_fill.setAlpha(60)
    p.setBrush(interior_fill)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(int(outer_x + t), int(outer_y + t),
               int(outer_w - t * 2), int(outer_h - t * 2))

    # Ring strips on L2 (orange-red, matching LAYER_UNDERCUT_RING display colour)
    ring_fill   = QColor("#b45309")   # amber-700
    ring_fill.setAlpha(210)
    ring_stroke = QColor("#fbbf24")   # amber-400

    p.setBrush(ring_fill)
    p.setPen(QPen(ring_stroke, 1.0))

    # Top strip  (owns corner squares)
    p.drawRect(int(outer_x),     int(outer_y),
               int(outer_w),     int(t))
    # Bottom strip (owns corner squares)
    p.drawRect(int(outer_x),     int(outer_y + outer_h - t),
               int(outer_w),     int(t))
    # Left strip  (inner height only)
    p.drawRect(int(outer_x),     int(outer_y + t),
               int(t),           int(outer_h - t * 2))
    # Right strip (inner height only)
    p.drawRect(int(outer_x + outer_w - t), int(outer_y + t),
               int(t),                     int(outer_h - t * 2))

    p.end()
    return pix


class DraggableCellButton(QPushButton):
    """
    Palette tile for one parametric cell.

    Mouse-move-while-pressed starts a Qt drag carrying:
      MIME type : application/x-gds-cell
      Payload   : "<cell_id>:<json_defaults>"

    The canvas view decodes this in dropEvent and calls
    scene.drop_cell(cell_id, scene_pos, params).
    """

    MIME_TYPE = "application/x-gds-cell"

    def __init__(self, cdef, parent=None) -> None:
        super().__init__(parent)
        self._cdef       = cdef
        self._drag_start: Optional[QPoint] = None

        self.setFixedHeight(58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # ── Outer horizontal layout: icon | text column ───────────────────────
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 7, 12, 7)
        outer.setSpacing(10)

        # Icon
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_cell_icon(cdef.cell_id, size=36))
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setStyleSheet("background: transparent;")
        outer.addWidget(icon_lbl)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        top = QLabel(cdef.name)
        top.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; background: transparent;"
        )
        bot = QLabel("Drag to canvas to place")
        bot.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; background: transparent;"
        )
        text_col.addWidget(top)
        text_col.addWidget(bot)
        outer.addLayout(text_col)

        self.setStyleSheet(f"""
            QPushButton {{
                background: {Colors.BG_ELEVATED};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: {Geometry.BORDER_RADIUS}px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: {Colors.BG_OVERLAY};
                border-color: {Colors.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {Colors.ACCENT_GLOW};
                border-color: {Colors.ACCENT};
            }}
        """)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.pos() - self._drag_start).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        import json
        payload = f"{self._cdef.cell_id}:{json.dumps(self._cdef.defaults)}".encode()

        mime = QMimeData()
        mime.setData(self.MIME_TYPE, QByteArray(payload))

        pix = QPixmap(120, 32)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setOpacity(0.80)
        p.fillRect(pix.rect(), QColor("#1e293b"))
        p.setPen(QColor("#7c3aed"))
        p.drawRect(0, 0, pix.width() - 1, pix.height() - 1)
        p.setPen(QColor("#c4b5fd"))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, f"⬡ {self._cdef.name}")
        p.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


# ── Component Palette ─────────────────────────────────────────────────────────

class ComponentPalette(QWidget):
    """
    Left dock: unified scrollable panel with three sections in order:
      1. Active Layer selector
      2. Shapes  (Rectangle / Polygon / Path — drag or click to place)
      3. Cell Library  (parametric cells grouped by category — drag to place)

    Replaces the previous two-tab layout (Shapes tab + Cells tab) with a
    single continuous panel.  No QTabWidget needed.
    """

    place_mode_requested = pyqtSignal(object, int)   # ComponentKind, layer

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PALETTE_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Fixed header
        root.addWidget(self._build_header())

        # Single scrollable body containing all sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        body = QWidget()
        body.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        body_lay.addWidget(self._build_layer_section())
        body_lay.addWidget(Separator())
        body_lay.addWidget(self._build_cells_section())
        body_lay.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(2)
        title = QLabel("Palette")
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 13px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        sub = QLabel("Drag shapes or cells onto the canvas")
        sub.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        hl.addWidget(title)
        hl.addWidget(sub)
        return header

    # ── Layer selector ────────────────────────────────────────────────────────

    def _build_layer_section(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(4)
        lay.addWidget(SectionLabel("Active Layer"))

        self._layer_combo = QComboBox()
        for i in range(8):
            self._layer_combo.addItem(_layer_icon(i), f"Layer {i}", i)
        self._layer_combo.setCurrentIndex(0)
        self._layer_combo.setStyleSheet(f"""
            QComboBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: {Geometry.BORDER_RADIUS}px;
                padding: 8px 12px;
                font-size: {Fonts.SIZE_SM}px;
                color: {Colors.TEXT_PRIMARY};
                min-height: 36px;
            }}
            QComboBox::drop-down {{ border: none; width: 26px; }}
        """)
        lay.addWidget(self._layer_combo)
        return w

    @property
    def active_layer(self) -> int:
        return self._layer_combo.currentData()

    # ── Shapes section ────────────────────────────────────────────────────────

    def _build_shapes_section(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(6)
        lay.addWidget(SectionLabel("Shapes"))

        shapes = [
            (ComponentKind.POLYGON, "Polygon", "Click vertices, Enter/dbl-click to close"),
        ]
        for kind, label, sub in shapes:
            lay.addWidget(self._make_shape_button(kind, label, sub))
        return w

    def _make_shape_button(self, kind: ComponentKind, label: str, sub: str) -> "DraggableShapeButton":
        btn = DraggableShapeButton(kind, self)
        btn.setFixedHeight(58)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # ── Outer horizontal layout: icon | text column ───────────────────────
        outer = QHBoxLayout(btn)
        outer.setContentsMargins(10, 7, 12, 7)
        outer.setSpacing(10)

        # Icon
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_shape_icon(kind, size=36))
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setStyleSheet("background: transparent;")
        outer.addWidget(icon_lbl)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        top = QLabel(label)
        top.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"font-size: {Fonts.SIZE_SM}px; background: transparent;"
        )
        bot = QLabel(sub)
        bot.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; background: transparent;"
        )
        bot.setWordWrap(True)
        text_col.addWidget(top)
        text_col.addWidget(bot)
        outer.addLayout(text_col)

        btn.setStyleSheet(f"""
            QPushButton {{
                background: {Colors.BG_ELEVATED};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: {Geometry.BORDER_RADIUS}px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: {Colors.BG_OVERLAY};
                border-color: {Colors.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {Colors.ACCENT_GLOW};
                border-color: {Colors.ACCENT};
            }}
        """)

        # Click-to-enter-mode fallback (drag is handled by DraggableShapeButton)
        btn.clicked.connect(
            lambda _, k=kind: self.place_mode_requested.emit(k, self.active_layer)
        )
        return btn

    # ── Cell Library section ──────────────────────────────────────────────────

    def _build_cells_section(self) -> QWidget:
        """
        Inline version of CellLibraryPanel: all CELL_CATALOGUE entries grouped
        by category, each rendered as a DraggableCellButton.
        Scrolling is handled by the parent QScrollArea so no inner scroll needed.
        """
        from core.cell_library import CELL_CATALOGUE

        w = QWidget()
        w.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                               Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        lay.setSpacing(4)

        # Group cells by category, preserving insertion order
        seen: list[str] = []
        by_cat: dict[str, list] = {}
        for cdef in CELL_CATALOGUE:
            if cdef.cell_id == "undercut_ring":
                continue
            if cdef.category not in by_cat:
                seen.append(cdef.category)
                by_cat[cdef.category] = []
            by_cat[cdef.category].append(cdef)

        for cat in seen:
            lay.addWidget(SectionLabel(cat))
            for cdef in by_cat[cat]:
                lay.addWidget(DraggableCellButton(cdef))
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {Colors.BG_BORDER}; margin-top: 4px;")
            lay.addWidget(sep)

        return w


class DraggableShapeButton(QPushButton):
    """
    Shape palette button that starts a Qt drag on mouse-move-while-pressed.
    MIME type: application/x-gds-shape
    Payload:   "<kind_value>:<layer>" as UTF-8, e.g. "1:0" for RECTANGLE on layer 0
    """

    MIME_TYPE = "application/x-gds-shape"

    def __init__(self, kind: ComponentKind, palette: "ComponentPalette") -> None:
        super().__init__(palette)
        self._kind    = kind
        self._palette = palette
        self._drag_start: Optional[QPoint] = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.pos() - self._drag_start).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        layer   = self._palette.active_layer
        payload = f"{self._kind.value}:{layer}".encode()

        mime = QMimeData()
        mime.setData(self.MIME_TYPE, QByteArray(payload))

        # Build a small semi-transparent drag pixmap
        pix = QPixmap(80, 32)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setOpacity(0.75)
        p.fillRect(pix.rect(), QColor("#334155"))
        p.setPen(QColor("#94a3b8"))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, self._kind.name.capitalize())
        p.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


# ── Properties Panel ──────────────────────────────────────────────────────────

def _comp_short_label(comp: GDSComponent) -> str:
    """
    Return a human-readable one-line description of a component, avoiding
    raw IDs as the primary label.  Examples:
      "Rectangle  2.0 × 0.4 µm  L1"
      "Polygon  8 pts  L0"
      "Path  5 pts · w=0.5 µm  L2"
    """
    from core.model import dbu_to_um as _d2u
    layer = f"L{comp.layer}"
    if comp.kind == ComponentKind.RECTANGLE:
        w = _d2u(comp.width);  h = _d2u(comp.height)
        return f"Rectangle  {w:.2f} × {h:.2f} µm  {layer}"
    elif comp.kind == ComponentKind.POLYGON:
        n = len(comp.points) if comp.points else 0
        return f"Polygon  {n} pts  {layer}"
    else:  # PATH
        n = len(comp.points) if comp.points else 0
        pw = f" · w={_d2u(comp.path_width):.2f} µm" if comp.path_width else ""
        return f"Path  {n} pts{pw}  {layer}"


class MemberCard(QWidget):
    """
    Compact editable card for one group member shown inside the group view.
    Emits the same signals as PropertiesPanel so MainWindow needs no changes.

    Hover-highlight: hovering the card emits component_hover_requested(comp_id)
    so the canvas can flash a beacon on the corresponding shape.  Leave emits
    component_hover_requested("") to clear.
    """
    layer_change_requested    = pyqtSignal(str, int)
    geometry_change_requested = pyqtSignal(str, str, int)
    component_hover_requested = pyqtSignal(str)   # comp_id or "" to clear

    def __init__(self, comp: GDSComponent, design, parent=None) -> None:
        super().__init__(parent)
        self._comp   = comp
        self._design = design
        self._base_style = f"""
            QWidget {{
                background: {Colors.BG_ELEVATED};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 4px;
            }}
        """
        self._hover_style = f"""
            QWidget {{
                background: {Colors.BG_OVERLAY};
                border: 1px solid #facc15;
                border-radius: 4px;
            }}
        """
        self.setStyleSheet(self._base_style)
        self.setMouseTracking(True)
        self._build(comp, design)

    def enterEvent(self, event) -> None:
        self.setStyleSheet(self._hover_style)
        self.component_hover_requested.emit(self._comp.id)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setStyleSheet(self._base_style)
        self.component_hover_requested.emit("")
        super().leaveEvent(event)

    def _build(self, comp: GDSComponent, design) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)

        # ── Header: kind icon + human-readable label + layer swatch ──────────
        header = QHBoxLayout()
        swatch = QLabel("█")
        color  = Colors.LAYER_COLORS[comp.layer % len(Colors.LAYER_COLORS)]
        swatch.setStyleSheet(
            f"color: {color}; font-size: 14px; background: transparent; border: none;"
        )
        # Human-readable label as the primary text; ID in smaller muted text below
        label_col = QVBoxLayout()
        label_col.setSpacing(1)
        label_col.setContentsMargins(0, 0, 0, 0)
        kind_lbl = QLabel(_comp_short_label(comp))
        kind_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_SM}px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        id_lbl = QLabel(f"id: {comp.id}")
        id_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS - 1}px; "
            f"font-family: {Fonts.MONO_FAMILY}; background: transparent; border: none;"
        )
        label_col.addWidget(kind_lbl)
        label_col.addWidget(id_lbl)

        # Hover hint icon (eye symbol) — static, just visually hints the interaction
        eye_lbl = QLabel("👁")
        eye_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px; "
            f"background: transparent; border: none;"
        )
        eye_lbl.setToolTip("Hover to highlight this shape on the canvas")

        header.addWidget(swatch)
        header.addLayout(label_col)
        header.addStretch()
        header.addWidget(eye_lbl)
        lay.addLayout(header)

        # ── Inline rows ───────────────────────────────────────────────────────
        def inline(label: str, value: str) -> None:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                f"background: transparent; border: none;"
            )
            lbl.setFixedWidth(72)
            val = QLabel(value)
            val.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_XS}px; "
                f"font-family: {Fonts.MONO_FAMILY}; background: transparent; border: none;"
            )
            row.addWidget(lbl); row.addWidget(val); row.addStretch()
            lay.addLayout(row)

        bb = comp.bbox
        inline("Origin", f"{dbu_to_um(comp.origin.x):.2f}, {dbu_to_um(comp.origin.y):.2f} µm")
        inline("BBox",
               f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f}) → "
               f"({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})")

        # Connections
        if design is not None:
            sides = design.connected_sides(comp.id)
            if sides:
                order  = [PortSide.NORTH, PortSide.SOUTH, PortSide.EAST, PortSide.WEST]
                labels = {PortSide.NORTH:"N", PortSide.SOUTH:"S",
                          PortSide.EAST:"E",  PortSide.WEST:"W"}
                inline("Connected",
                       "  ·  ".join(labels[s] for s in order if s in sides))

        # ── Editable fields ───────────────────────────────────────────────────
        spin_style = f"""
            QDoubleSpinBox, QSpinBox {{
                background: {Colors.BG_BASE}; border: 1px solid {Colors.BG_BORDER};
                border-radius: 3px; color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_XS}px; padding: 2px 4px;
            }}
            QDoubleSpinBox:hover, QSpinBox:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
            QSpinBox::up-button,       QSpinBox::down-button {{ width: 14px; }}
        """

        def spin_row(label: str, widget) -> None:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                f"background: transparent; border: none;"
            )
            lbl.setFixedWidth(72)
            widget.setFixedWidth(100)
            widget.setStyleSheet(spin_style)
            row.addWidget(lbl); row.addWidget(widget); row.addStretch()
            lay.addLayout(row)

        # Layer
        # Use editingFinished (not valueChanged) so the signal only fires when
        # the user commits the value — not on every arrow-key / keystroke, which
        # was triggering _on_model_changed -> panel rebuild -> focus loss each time.
        layer_sb = QSpinBox()
        layer_sb.setRange(0, 63)
        layer_sb.setValue(comp.layer)
        layer_sb.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        layer_sb.editingFinished.connect(
            lambda s=layer_sb, cid=comp.id: self.layer_change_requested.emit(cid, s.value())
        )
        spin_row("Layer", layer_sb)

        # Kind-specific editable fields
        if comp.kind == ComponentKind.RECTANGLE:
            w_sb = QDoubleSpinBox()
            w_sb.setRange(0.001, 10_000); w_sb.setDecimals(3)
            w_sb.setSuffix(" µm"); w_sb.setValue(dbu_to_um(comp.width))
            w_sb.editingFinished.connect(
                lambda s=w_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "width", um_to_dbu(s.value()))
            )
            spin_row("Width", w_sb)

            h_sb = QDoubleSpinBox()
            h_sb.setRange(0.001, 10_000); h_sb.setDecimals(3)
            h_sb.setSuffix(" µm"); h_sb.setValue(dbu_to_um(comp.height))
            h_sb.editingFinished.connect(
                lambda s=h_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "height", um_to_dbu(s.value()))
            )
            spin_row("Height", h_sb)

        elif comp.kind == ComponentKind.PATH and comp.path_width:
            pw_sb = QDoubleSpinBox()
            pw_sb.setRange(0.001, 1_000); pw_sb.setDecimals(3)
            pw_sb.setSuffix(" µm"); pw_sb.setValue(dbu_to_um(comp.path_width))
            pw_sb.editingFinished.connect(
                lambda s=pw_sb, cid=comp.id:
                    self.geometry_change_requested.emit(cid, "path_width", um_to_dbu(s.value()))
            )
            spin_row("Path W", pw_sb)

        elif comp.kind in (ComponentKind.POLYGON, ComponentKind.PATH):
            inline("Vertices", str(comp.vertex_count))


class PropertiesPanel(QWidget):
    """
    Right dock — two modes:
      • Single component selected  → existing single-component view (unchanged)
      • Group selected             → group view: header + scrollable MemberCard list
    """

    layer_change_requested    = pyqtSignal(str, int)
    geometry_change_requested = pyqtSignal(str, str, int)
    # Emitted when a cell-group parameter spinbox changes:
    # (group_id, cell_id, param_key, new_value_as_float_or_str)
    cell_param_change_requested = pyqtSignal(str, str, str, object)
    # Emitted when the per-object undercut toggle changes: (obj_id, excluded: bool)
    # (obj_id, value) — value is bool (excluded) for objects, float (µm) for "__offset__"
    undercut_exclusion_changed = pyqtSignal(str, object)
    # Emitted when a member card is hovered — comp_id, or "" to clear highlight
    component_hover_requested  = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Geometry.PROPERTIES_WIDTH)
        self.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._current_comp_id: Optional[str] = None
        self._current_group_id: Optional[str] = None
        self._cell_params_widget = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Stack: page 0 = single component, page 1 = group, page 2 = multi,
        #           page 3 = empty (nothing selected) ─────────────────────────
        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_single_page())
        self._stack.addWidget(self._build_group_page())
        self._stack.addWidget(self._build_multi_page())
        self._stack.addWidget(self._build_empty_page())
        root.addWidget(self._stack)

        self.clear()

    # ── Empty page (nothing selected) ────────────────────────────────────────

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl = QLabel("No selection")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_SM}px;"
        )
        lay.addWidget(lbl)
        return page

    # ── Single-component page (identical to original) ─────────────────────────

    def _build_single_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
                              Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        cl.setSpacing(2)

        cl.addWidget(SectionLabel("Identity"))
        self._row_id   = ValueRow("ID")
        self._row_kind = ValueRow("Kind")
        cl.addWidget(self._row_id)
        cl.addWidget(self._row_kind)

        layer_row = QWidget()
        lr = QHBoxLayout(layer_row)
        lr.setContentsMargins(0, 2, 0, 2); lr.setSpacing(8)
        lbl = QLabel("Layer")
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)
        self._layer_spin = QSpinBox()
        self._layer_spin.setRange(0, 63)
        self._layer_spin.setEnabled(False)
        self._layer_spin.setFixedWidth(72)
        self._layer_spin.valueChanged.connect(self._on_layer_changed)
        lr.addWidget(lbl); lr.addWidget(self._layer_spin); lr.addStretch()
        cl.addWidget(layer_row)
        cl.addWidget(Separator())

        cl.addWidget(SectionLabel("Geometry"))
        self._row_x = ValueRow("X origin")
        self._row_y = ValueRow("Y origin")
        cl.addWidget(self._row_x); cl.addWidget(self._row_y)
        _, self._row_w_spin  = self._make_dim_row("Width",      "width",      cl)
        _, self._row_h_spin  = self._make_dim_row("Height",     "height",     cl)
        self._row_verts      = ValueRow("Vertices")
        cl.addWidget(self._row_verts)
        _, self._row_pw_spin = self._make_dim_row("Path width", "path_width", cl)
        cl.addWidget(Separator())

        cl.addWidget(SectionLabel("Bounding Box"))
        self._row_bbox = ValueRow("Extents")
        cl.addWidget(self._row_bbox)

        cl.addWidget(Separator())
        cl.addWidget(SectionLabel("Connections"))
        self._row_connections = ValueRow("Connected")
        cl.addWidget(self._row_connections)

        cl.addWidget(Separator())
        cl.addWidget(SectionLabel("Undercut Ring"))
        self._undercut_row = self._make_undercut_row()
        cl.addWidget(self._undercut_row)

        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root.addWidget(scroll)
        return page

    def _make_dim_row(self, label_text: str, field: str, layout) -> tuple:
        """
        Build a label + QDoubleSpinBox row, add it to layout, return (row_widget, spinbox).
        The spinbox emits geometry_change_requested on editingFinished (Enter or focus-out),
        not on every keystroke — so the undo stack stays clean.
        """
        row = QWidget()
        hl  = QHBoxLayout(row)
        hl.setContentsMargins(0, 2, 0, 2)
        hl.setSpacing(8)

        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")
        lbl.setFixedWidth(84)

        sb = QDoubleSpinBox()
        sb.setRange(0.001, 10_000.0)   # µm: 1 nm minimum, 10 mm maximum
        sb.setDecimals(3)
        sb.setSuffix(" µm")
        sb.setSingleStep(0.5)
        sb.setFixedWidth(110)
        sb.setEnabled(False)
        sb.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_SM}px;
                padding: 3px 6px;
            }}
            QDoubleSpinBox:enabled:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; }}
        """)
        sb.editingFinished.connect(lambda f=field, s=sb: self._on_dim_changed(f, s))

        hl.addWidget(lbl)
        hl.addWidget(sb)
        hl.addStretch()
        layout.addWidget(row)
        return row, sb

    def _make_undercut_row(self, shared: bool = True) -> QWidget:
        """
        Build the undercut ring toggle + offset spinbox widget.

        shared=True  → stores widgets as self._uc_btn / self._uc_spin
                        (used by the single-component page).
        shared=False → stores widgets as self._grp_uc_btn / self._grp_uc_spin
                        (used by the group page header).
        Returns the outer QWidget.
        """
        row = QWidget()
        vl  = QVBoxLayout(row)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(20)

        btn = QPushButton("Generate Undercut")
        btn.setCheckable(True)
        btn.setChecked(False)
        btn.setFixedHeight(24)
        btn.setEnabled(False)
        btn.setStyleSheet(self._uc_btn_style(False))

        # Offset row: label + spinbox side by side
        offset_row = QWidget()
        hl = QHBoxLayout(offset_row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        hl.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        uc_lbl = QLabel("offset")
        uc_lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px;")

        spin = QDoubleSpinBox()
        spin.setRange(0.05, 5.0)
        spin.setSingleStep(0.1)
        spin.setDecimals(2)
        spin.setSuffix(" µm")
        spin.setValue(0.8)
        spin.setFixedWidth(88)
        spin.setEnabled(False)
        spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_XS}px;
                padding: 2px 4px;
            }}
            QDoubleSpinBox:enabled:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 14px; }}
        """)

        if shared:
            self._uc_btn  = btn
            self._uc_spin = spin
            btn.clicked.connect(self._on_uc_btn_clicked)
            spin.valueChanged.connect(self._on_uc_spin_changed)
        else:
            self._grp_uc_btn  = btn
            self._grp_uc_spin = spin
            btn.clicked.connect(self._on_grp_uc_btn_clicked)
            spin.valueChanged.connect(self._on_grp_uc_spin_changed)

        hl.addWidget(uc_lbl)
        hl.addWidget(spin)
        hl.addStretch()

        vl.addWidget(btn)
        vl.addWidget(offset_row)
        return row

    @staticmethod
    def _uc_btn_style(checked: bool) -> str:
        if checked:
            return (
                f"QPushButton {{ background: {Colors.ACCENT}; color: #fff; "
                f"border: 1px solid {Colors.ACCENT}; border-radius: 4px; "
                f"font-size: {Fonts.SIZE_XS}px; padding: 2px 6px; }}"
                f"QPushButton:hover {{ background: {Colors.ACCENT_DIM}; }}"
            )
        return (
            f"QPushButton {{ background: {Colors.BG_BASE}; color: {Colors.TEXT_MUTED}; "
            f"border: 1px solid {Colors.BG_BORDER}; border-radius: 4px; "
            f"font-size: {Fonts.SIZE_XS}px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ background: {Colors.BG_OVERLAY}; "
            f"border-color: {Colors.ACCENT_DIM}; }}"
            f"QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; "
            f"background: {Colors.BG_BASE}; }}"
        )

    def _on_uc_btn_clicked(self, checked: bool) -> None:
        """Component-page undercut toggle — checked = ring is showing."""
        self._uc_btn.setStyleSheet(self._uc_btn_style(checked))
        self._uc_btn.setText("Remove Undercut" if checked else "Generate Undercut")
        if self._current_comp_id:
            self.undercut_exclusion_changed.emit(self._current_comp_id, not checked)

    def _on_uc_spin_changed(self, value: float) -> None:
        """Forward offset changes from the component page."""
        self.undercut_exclusion_changed.emit("__offset__", value)

    def _on_grp_uc_btn_clicked(self, checked: bool) -> None:
        """Group-page undercut toggle — checked = ring is showing."""
        self._grp_uc_btn.setStyleSheet(self._uc_btn_style(checked))
        self._grp_uc_btn.setText("Remove Undercut" if checked else "Generate Undercut")
        if self._current_group_id:
            self.undercut_exclusion_changed.emit(self._current_group_id, not checked)

    def _on_grp_uc_spin_changed(self, value: float) -> None:
        """Forward offset changes from the group page."""
        self.undercut_exclusion_changed.emit("__offset__", value)

    def _update_undercut_ui(self, obj_id: str, overlay) -> None:
        """
        Sync the correct toggle button + spinbox pair to the overlay's current
        state for the given component/group ID.
        Called from show_component (uses _uc_btn/_uc_spin) and
        show_group (uses _grp_uc_btn/_grp_uc_spin).
        """
        # Determine which widget pair is active based on which page is shown.
        on_group_page = (self._stack.currentIndex() == 1)
        btn  = self._grp_uc_btn  if on_group_page else self._uc_btn
        spin = self._grp_uc_spin if on_group_page else self._uc_spin

        if overlay is None:
            btn.setEnabled(False)
            spin.setEnabled(False)
            return

        global_on   = overlay.is_enabled
        is_excluded = overlay.is_excluded(obj_id)
        # The button reflects whether THIS object has its ring enabled (not
        # excluded). The global toggle is a separate concern — turning the ring
        # on per-object will auto-enable the global overlay in MainWindow.
        ring_on = not is_excluded

        btn.blockSignals(True)
        btn.setChecked(ring_on)
        btn.setText("Remove Undercut" if ring_on else "Generate Undercut")
        btn.setStyleSheet(self._uc_btn_style(ring_on))
        btn.setEnabled(True)
        btn.blockSignals(False)

        spin.blockSignals(True)
        spin.setValue(overlay.offset_um)
        spin.setEnabled(True)
        spin.blockSignals(False)

    # ── Group page ────────────────────────────────────────────────────────────

    def _build_group_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Fixed header
        self._grp_header = QWidget()
        self._grp_header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(self._grp_header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(3)
        self._grp_name_lbl  = QLabel("Group")
        self._grp_name_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 14px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        self._grp_meta_lbl  = QLabel("")
        self._grp_meta_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        self._grp_bbox_lbl  = QLabel("")
        self._grp_bbox_lbl.setStyleSheet(self._grp_meta_lbl.styleSheet())
        hl.addWidget(self._grp_name_lbl)
        hl.addWidget(self._grp_meta_lbl)
        hl.addWidget(self._grp_bbox_lbl)
        hl.addSpacing(10)
        self._grp_undercut_row = self._make_undercut_row(shared=False)
        hl.addWidget(self._grp_undercut_row)
        root.addWidget(self._grp_header)

        # Scrollable member cards
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        self._cards_container = QWidget()
        self._cards_container.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch()

        self._cards_scroll.setWidget(self._cards_container)
        root.addWidget(self._cards_scroll)
        return page

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._stack.setCurrentIndex(3)
        self._current_comp_id  = None
        self._current_group_id = None
        if hasattr(self, "_cell_params_widget") and self._cell_params_widget is not None:
            _w = self._cell_params_widget
            self._cell_params_widget = None   # clear ref FIRST, before any Qt call
            try:
                _w.setParent(None)
            except RuntimeError:
                pass   # C++ object already deleted — nothing to do
        for sb in (self._layer_spin, self._row_w_spin,
                   self._row_h_spin, self._row_pw_spin):
            sb.blockSignals(True); sb.setValue(0)
            sb.setEnabled(False);  sb.blockSignals(False)
        for r in [self._row_id, self._row_kind, self._row_x, self._row_y,
                  self._row_verts, self._row_bbox, self._row_connections]:
            r.set_value("—")

    def show_component(self, comp: GDSComponent, design=None, overlay=None) -> None:
        self._stack.setCurrentIndex(0)
        self._current_comp_id = comp.id
        bb = comp.bbox

        self._row_id.set_value(comp.id)
        self._row_kind.set_value(comp.kind.name.capitalize())

        self._layer_spin.blockSignals(True)
        self._layer_spin.setValue(comp.layer)
        self._layer_spin.setEnabled(True)
        self._layer_spin.blockSignals(False)

        self._row_x.set_value(f"{dbu_to_um(comp.origin.x):.3f} µm")
        self._row_y.set_value(f"{dbu_to_um(comp.origin.y):.3f} µm")

        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(True)
        is_rect = comp.kind == ComponentKind.RECTANGLE
        self._row_w_spin.setEnabled(is_rect)
        self._row_h_spin.setEnabled(is_rect)
        if is_rect:
            self._row_w_spin.setValue(dbu_to_um(comp.width))
            self._row_h_spin.setValue(dbu_to_um(comp.height))
        else:
            self._row_w_spin.setValue(0.0); self._row_h_spin.setValue(0.0)
        for sb in (self._row_w_spin, self._row_h_spin):
            sb.blockSignals(False)

        if comp.kind in (ComponentKind.POLYGON, ComponentKind.PATH):
            self._row_verts.set_value(str(comp.vertex_count))
        else:
            self._row_verts.set_value("—")

        self._row_pw_spin.blockSignals(True)
        is_path = comp.kind == ComponentKind.PATH
        self._row_pw_spin.setEnabled(is_path)
        self._row_pw_spin.setValue(
            dbu_to_um(comp.path_width or 0) if is_path else 0.0
        )
        self._row_pw_spin.blockSignals(False)

        self._row_bbox.set_value(
            f"({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f})"
        )

        if design is not None:
            sides = design.connected_sides(comp.id)
            if sides:
                order  = [PortSide.NORTH, PortSide.SOUTH,
                          PortSide.EAST,  PortSide.WEST]
                labels = {PortSide.NORTH:"N", PortSide.SOUTH:"S",
                          PortSide.EAST:"E",  PortSide.WEST:"W"}
                self._row_connections.set_value(
                    "  ·  ".join(labels[s] for s in order if s in sides)
                )
            else:
                self._row_connections.set_value("None")
        else:
            self._row_connections.set_value("—")

        self._update_undercut_ui(comp.id, overlay)

    def show_group(self, group, design, overlay=None) -> None:
        """Switch to group view and populate member cards + optional cell params."""
        self._stack.setCurrentIndex(1)
        self._current_comp_id = None
        self._current_group_id = group.id

        # Header
        members = [design.get(cid) for cid in group.member_ids
                   if design.get(cid)]
        bb = group.bbox_from(design.components)

        self._grp_name_lbl.setText(f"⬡  {group.name}")
        self._grp_meta_lbl.setText(
            f"{len(members)} component{'s' if len(members) != 1 else ''}  ·  "
            f"id {group.id}"
        )
        self._grp_bbox_lbl.setText(
            f"BBox  ({dbu_to_um(bb.x_min):.1f}, {dbu_to_um(bb.y_min):.1f})"
            f" → ({dbu_to_um(bb.x_max):.1f}, {dbu_to_um(bb.y_max):.1f}) µm"
        )

        # ── Cell params section (shown when group matches a catalogue cell) ───
        # Must be called BEFORE rebuilding cards since it inserts into _cards_layout.
        # Also remove any stale cell_params_widget from the layout first (it is now
        # inside _cards_layout at index 0, not above the scroll area).
        if hasattr(self, "_cell_params_widget") and self._cell_params_widget is not None:
            _w = self._cell_params_widget
            self._cell_params_widget = None   # clear ref FIRST, before any Qt call
            try:
                _w.setParent(None)
            except RuntimeError:
                pass   # C++ object already deleted — nothing to do
        self._rebuild_cell_params(group)

        # Rebuild member cards
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for comp in members:
            card = MemberCard(comp, design)
            card.layer_change_requested.connect(self.layer_change_requested)
            card.geometry_change_requested.connect(self.geometry_change_requested)
            card.component_hover_requested.connect(self.component_hover_requested)
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, card
            )

        self._update_undercut_ui(group.id, overlay)

    def _rebuild_cell_params(self, group) -> None:
        """
        Render editable parameter sections for all cell sub-groups contained in
        *group*, covering every grouping combination:

          item+item   — no cell sub-groups have a cell_id → no param sections shown
          cell alone  — one sub-group with cell_id → one param section
          cell+cell   — two sub-groups each with cell_id → two param sections
          cell+item   — one cell sub-group shows params; item sub-group is silent

        The legacy path (group.cell_id set but no _cell_subgroups) is still
        handled for files saved before this change: a synthetic single-entry
        _cell_subgroups list is constructed from the legacy attrs so the same
        rendering loop works unchanged.

        All widgets are inserted at index 0 of _cards_layout (inside the
        QScrollArea) so that params + member cards scroll together.
        """
        # The caller (show_group) clears any existing _cell_params_widget before
        # calling here, so we assert it is already None.  The guard below is kept
        # as a safety net for direct calls (e.g. from undo paths).
        # IMPORTANT: hide() before setParent(None) to avoid a Qt flash where
        # the widget briefly becomes a top-level window during reparenting.
        if hasattr(self, "_cell_params_widget") and self._cell_params_widget is not None:
            _w = self._cell_params_widget
            self._cell_params_widget = None   # clear ref FIRST, before any Qt call
            try:
                _w.setParent(None)
            except RuntimeError:
                pass   # C++ object already deleted — nothing to do

        # ── Resolve sub-groups list ───────────────────────────────────────────
        # Prefer the explicit _cell_subgroups (set by all modern commands).
        # Fall back to a synthetic single-entry list for legacy files that only
        # have group.cell_id / group._cell_params.
        cell_subgroups = getattr(group, "_cell_subgroups", None)
        if not cell_subgroups:
            legacy_cell_id = getattr(group, "cell_id", None)
            if legacy_cell_id:
                cell_subgroups = [
                    {
                        "name":                group.name,
                        "cell_id":             legacy_cell_id,
                        "cell_params":         dict(getattr(group, "_cell_params", {})),
                        "cell_rotation_steps": 0,
                        "member_ids":          list(group.member_ids),
                    }
                ]
            else:
                return   # item+item group — no cell params to show

        # Filter down to only sub-groups that have a real cell_id
        cell_sgs = [
            (sg_index, sg)
            for sg_index, sg in enumerate(cell_subgroups)
            if sg.get("cell_id") and CELL_BY_ID.get(sg["cell_id"]) is not None
        ]
        if not cell_sgs:
            return   # all sub-groups are plain items — nothing to render

        # ── Shared styles ─────────────────────────────────────────────────────
        spin_style = f"""
            QDoubleSpinBox, QSpinBox, QComboBox {{
                background: {Colors.BG_BASE};
                border: 1px solid {Colors.BG_BORDER};
                border-radius: 3px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {Fonts.SIZE_XS}px;
                padding: 2px 4px;
            }}
            QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {Colors.ACCENT_DIM}; }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 14px; }}
        """
        _STRING_OPTIONS: dict[str, list[str]] = {
            "cap_style":      ["top", "side"],
            "undercut_style": ["right", "top"],
        }

        # ── Outer container ───────────────────────────────────────────────────
        w = QWidget()
        w.setStyleSheet(f"""
            QWidget {{
                background: {Colors.BG_ELEVATED};
                border-bottom: 1px solid {Colors.BG_BORDER};
            }}
        """)
        outer_lay = QVBoxLayout(w)
        outer_lay.setContentsMargins(Geometry.PANEL_PADDING, 8,
                                     Geometry.PANEL_PADDING, 8)
        outer_lay.setSpacing(0)

        # Section header (always shown once at the top)
        header_lbl = QLabel("Cell Parameters")
        header_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_SM}px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        hint_lbl = QLabel("Edit a value to re-place that cell in-place")
        hint_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        outer_lay.addWidget(header_lbl)
        outer_lay.addWidget(hint_lbl)

        group_id   = group.id
        multi_cell = len(cell_sgs) > 1

        for sg_index, sg in cell_sgs:
            cdef   = CELL_BY_ID[sg["cell_id"]]
            # Encode the sub-group index into cell_id so the main-window slot
            # can route to ReplaceSubgroupCellCmd for merged groups, and to
            # ReplaceCellCmd for plain single-cell groups.
            # For single-cell groups (_cell_subgroups has exactly 1 entry and
            # the index is 0), the receiver checks for the colon suffix and
            # routes accordingly — so this encoding is always safe.
            encoded_cell_id = f"{cdef.cell_id}:{sg_index}"

            # Current params: catalogue defaults → stored overrides
            current_params = dict(cdef.defaults)
            current_params.update(sg.get("cell_params", {}))

            # Sub-group divider label (only when there are multiple cell sub-groups)
            if multi_cell:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet(f"color: {Colors.BG_BORDER}; margin: 4px 0;")
                outer_lay.addWidget(sep)

                sg_lbl = QLabel(f"▸ {sg.get('name', cdef.name)}")
                sg_lbl.setStyleSheet(
                    f"color: {Colors.ACCENT}; font-size: {Fonts.SIZE_XS}px; "
                    f"font-weight: bold; background: transparent; border: none; "
                    f"padding-top: 4px;"
                )
                outer_lay.addWidget(sg_lbl)

            # ── One row per parameter ─────────────────────────────────────────
            for key, default in cdef.defaults.items():
                current_val = current_params.get(key, default)
                row = QHBoxLayout()
                row.setContentsMargins(0, 2, 0, 2)

                lbl = QLabel(key.replace("_", " "))
                lbl.setStyleSheet(
                    f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
                    f"background: transparent; border: none;"
                )
                lbl.setFixedWidth(90)

                if isinstance(default, float):
                    sb = _ParamSpinBox()
                    sb.setDecimals(3)
                    sb.setRange(0.001, 1000.0)
                    sb.setSingleStep(0.1)
                    sb.setSuffix(" µm")
                    sb.setValue(current_val)
                    sb.setFixedWidth(110)
                    sb.setStyleSheet(spin_style)
                    sb.editingFinished.connect(
                        lambda _k=key, _sb=sb, _gid=group_id, _cid=encoded_cell_id:
                            self.cell_param_change_requested.emit(_gid, _cid, _k, _sb.value())
                    )
                    row.addWidget(lbl)
                    row.addWidget(sb)
                    row.addStretch()

                elif isinstance(default, str):
                    options = _STRING_OPTIONS.get(key)
                    if options:
                        combo = QComboBox()
                        combo.addItems(options)
                        combo.setCurrentText(str(current_val))
                        combo.setFixedWidth(110)
                        combo.setStyleSheet(spin_style)
                        combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                        combo.currentTextChanged.connect(
                            lambda val, _k=key, _gid=group_id, _cid=encoded_cell_id:
                                self.cell_param_change_requested.emit(_gid, _cid, _k, val)
                        )
                        row.addWidget(lbl)
                        row.addWidget(combo)
                        row.addStretch()
                    else:
                        val_lbl = QLabel(str(current_val))
                        val_lbl.setStyleSheet(
                            f"color: {Colors.TEXT_PRIMARY}; font-size: {Fonts.SIZE_XS}px; "
                            f"background: transparent; border: none;"
                        )
                        row.addWidget(lbl)
                        row.addWidget(val_lbl)
                        row.addStretch()

                outer_lay.addLayout(row)

        self._cell_params_widget = w
        # Insert at the TOP of the scrollable cards area (index 0) so that
        # both cell params and member cards scroll together.  Previously this
        # was inserted above the QScrollArea (outside it), which meant the
        # panel could overflow without scrolling when there are many members.
        self._cards_layout.insertWidget(0, w)

    # ── Multi-selection page ──────────────────────────────────────────────────

    def _build_multi_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet(
            f"background: {Colors.BG_ELEVATED}; "
            f"border-bottom: 1px solid {Colors.BG_BORDER};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(Geometry.PANEL_PADDING, 10,
                              Geometry.PANEL_PADDING, 10)
        hl.setSpacing(3)
        self._multi_title = QLabel("Multiple Selected")
        self._multi_title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: 14px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        self._multi_subtitle = QLabel("")
        self._multi_subtitle.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; "
            f"background: transparent; border: none;"
        )
        hl.addWidget(self._multi_title)
        hl.addWidget(self._multi_subtitle)
        root.addWidget(header)

        self._multi_scroll = QScrollArea()
        self._multi_scroll.setWidgetResizable(True)
        self._multi_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._multi_scroll.setStyleSheet(f"background: {Colors.BG_SURFACE};")

        self._multi_container = QWidget()
        self._multi_container.setStyleSheet(f"background: {Colors.BG_SURFACE};")
        self._multi_layout = QVBoxLayout(self._multi_container)
        self._multi_layout.setContentsMargins(
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING,
            Geometry.PANEL_PADDING, Geometry.PANEL_PADDING)
        self._multi_layout.setSpacing(6)
        self._multi_layout.addStretch()

        self._multi_scroll.setWidget(self._multi_container)
        root.addWidget(self._multi_scroll)
        return page

    def show_multi_selection(self, components: list, design=None) -> None:
        """Switch to multi-select page and show a compact card per component."""
        self._stack.setCurrentIndex(2)
        self._current_comp_id = None

        n = len(components)
        self._multi_title.setText(f"{n} Components Selected")
        layers = sorted({c.layer for c in components})
        self._multi_subtitle.setText(
            f"Layers: {', '.join(f'L{l}' for l in layers)}"
        )

        while self._multi_layout.count() > 1:
            item = self._multi_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for comp in components:
            card = MemberCard(comp, design)
            card.layer_change_requested.connect(self.layer_change_requested)
            card.geometry_change_requested.connect(self.geometry_change_requested)
            card.component_hover_requested.connect(self.component_hover_requested)
            self._multi_layout.insertWidget(self._multi_layout.count() - 1, card)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_layer_changed(self, value: int) -> None:
        if self._current_comp_id:
            self.layer_change_requested.emit(self._current_comp_id, value)

    def _on_dim_changed(self, field: str, spinbox: QDoubleSpinBox) -> None:
        if self._current_comp_id:
            self.geometry_change_requested.emit(
                self._current_comp_id, field, um_to_dbu(spinbox.value())
            )