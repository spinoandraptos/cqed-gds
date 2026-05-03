"""
ui/canvas_view.py — QGraphicsView subclass.

Handles:
  - Smooth zoom via Ctrl+scroll (geometric zoom, not linear)
  - Pan via middle-button drag or Space+drag
  - Zoom-to-fit (F key)
  - Zoom limits (1 nm/px → 1 mm/px)
  - Crosshair cursor overlay
  - Adaptive grid density (hides minor grid when zoomed out)
"""

from __future__ import annotations

from PyQt6.QtWidgets import QGraphicsView, QFrame
from PyQt6.QtGui import (
    QPainter, QWheelEvent, QMouseEvent, QKeyEvent,
    QCursor, QColor, QPen,
)
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal

from ui.theme import Colors
from core.model import um_to_dbu


# Zoom limits: min = 5 px per major grid (10 µm), max = fits 5 mm on screen
ZOOM_MIN = 0.00005   # 1 screen-px ≈ 20 mm  (fully zoomed out)
ZOOM_MAX = 50.0      # 1 screen-px ≈ 20 nm  (fully zoomed in)
ZOOM_STEP = 1.18     # zoom factor per scroll tick


class CanvasView(QGraphicsView):
    """
    Rendering viewport for the canvas scene.
    Keeps all view-only state: zoom level, pan offset, cursor position.
    """

    zoom_changed = pyqtSignal(float)   # current px-per-DBU ratio

    def __init__(self, scene, parent=None) -> None:
        super().__init__(scene, parent)

        self._pan_active     = False
        self._pan_start      = QPointF()
        self._space_held     = False
        self._current_zoom   = 1.0       # px per DBU unit

        self._setup_view()
        self._fit_all()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_view(self) -> None:
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing |
            QPainter.RenderHint.SmoothPixmapTransform,
        )
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setBackgroundBrush(QColor(Colors.CANVAS_BG))

    def _fit_all(self) -> None:
        """Zoom-to-fit the entire scene rect."""
        ext = um_to_dbu(200)   # show ±200 µm on startup
        self.fitInView(
            QRectF(-ext, -ext, ext * 2, ext * 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._current_zoom = self.transform().m11()

    # ── Zoom ──────────────────────────────────────────────────────────────────

    def _zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        new_zoom = self._current_zoom * factor
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, new_zoom))
        actual_factor = new_zoom / self._current_zoom
        self._current_zoom = new_zoom
        self.scale(actual_factor, actual_factor)
        self.zoom_changed.emit(self._current_zoom)

    def zoom_in(self)  -> None: self._zoom_by(ZOOM_STEP)
    def zoom_out(self) -> None: self._zoom_by(1.0 / ZOOM_STEP)
    def zoom_fit(self) -> None: self._fit_all()

    @property
    def zoom_level(self) -> float:
        return self._current_zoom

    # ── Pan ───────────────────────────────────────────────────────────────────

    def _start_pan(self, pos: QPointF) -> None:
        self._pan_active = True
        self._pan_start  = pos
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def _end_pan(self) -> None:
        self._pan_active = False
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def _do_pan(self, pos: QPointF) -> None:
        delta = pos - self._pan_start
        self._pan_start = pos
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - int(delta.x())
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() - int(delta.y())
        )

    # ── Event overrides ───────────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
            self._zoom_by(factor)
        else:
            # Plain scroll → pan vertically
            delta = event.angleDelta().y()
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta // 3
            )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_held
        ):
            self._start_pan(event.position())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pan_active:
            self._do_pan(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._pan_active and (
            event.button() == Qt.MouseButton.MiddleButton or
            event.button() == Qt.MouseButton.LeftButton
        ):
            self._end_pan()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif event.key() == Qt.Key.Key_F:
            self._fit_all()
        elif event.key() == Qt.Key.Key_Plus or event.key() == Qt.Key.Key_Equal:
            self._zoom_by(ZOOM_STEP)
        elif event.key() == Qt.Key.Key_Minus:
            self._zoom_by(1.0 / ZOOM_STEP)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = False
            if not self._pan_active:
                self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            super().keyReleaseEvent(event)
