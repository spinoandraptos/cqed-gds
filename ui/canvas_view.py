"""
ui/canvas_view.py — QGraphicsView subclass.

Handles:
  - Smooth zoom via Ctrl+scroll (geometric zoom, not linear)
  - Pan via middle-button drag or Space+drag
  - Zoom-to-fit (F key)
  - Zoom limits (1 nm/px → 1 mm/px)
  - Crosshair cursor overlay
  - Adaptive grid density (hides minor grid when zoomed out)

Fix (drag bug):
  The view previously used RubberBandDrag as its default drag mode.
  That caused Qt to intercept every left-button drag at the view level and
  start a rubber-band, which meant mouseMoveEvent was never forwarded to
  ComponentItem during a drag — so _on_item_move was never called and
  nothing moved.

  Solution: default drag mode is now NoDrag so scene items receive all
  mouse events unobstructed.  Rubber-band selection on empty canvas is
  restored by temporarily switching to RubberBandDrag in mousePressEvent
  only when no item is under the cursor, and switching back to NoDrag on
  release.
"""

from __future__ import annotations

import json

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

        self._pan_active       = False
        self._pan_start        = QPointF()
        self._space_held       = False
        self._current_zoom     = 1.0       # px per DBU unit
        self._rubber_banding   = False     # True while an empty-canvas drag is live
        self._had_items        = False     # flips True after the first item lands

        self._setup_view()
        self._fit_all()

        # Auto-fit when the very first component is placed so it's immediately
        # visible at a comfortable zoom. scene_changed fires after every model
        # mutation; we disconnect after the first non-empty canvas so we never
        # fight the user's subsequent zoom choices.
        if hasattr(scene, "scene_changed"):
            scene.scene_changed.connect(self._on_scene_changed_first_item)

    # ── First-item auto-fit ───────────────────────────────────────────────────

    def _on_scene_changed_first_item(self) -> None:
        """
        Called on every scene_changed until the canvas has items.
        On the first change that produces a non-empty bounding rect, zoom-to-fit
        so the newly placed component is centred and clearly visible, then
        disconnect — we must never override the user's zoom after that.
        """
        if self._had_items:
            return
        items_rect = self.scene().itemsBoundingRect()
        if items_rect.isNull() or items_rect.isEmpty():
            return
        self._had_items = True
        self._fit_all()
        # Disconnect: user is now in control of zoom
        try:
            self.scene().scene_changed.disconnect(self._on_scene_changed_first_item)
        except (RuntimeError, TypeError):
            pass  # already disconnected or scene gone — safe to ignore

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
        # NoDrag by default — scene items must receive mouse events unobstructed.
        # Rubber-band is enabled temporarily in mousePressEvent when clicking
        # empty canvas (see below).
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setBackgroundBrush(QColor(Colors.CANVAS_BG))
        self.setAcceptDrops(True)

    def _fit_all(self) -> None:
        """Zoom-to-fit all items on the canvas, or a default rect if empty.

        Empty canvas uses a tight 50×50 µm working area centred on the origin
        so the first dropped component is immediately visible at a comfortable
        zoom rather than the full scene extent (which makes a 2 µm shape a
        single pixel).
        """
        items_rect = self.scene().itemsBoundingRect()
        if items_rect.isNull() or items_rect.isEmpty():
            ext = um_to_dbu(25)   # ±25 µm → 50×50 µm window
            fit_rect = QRectF(-ext, -ext, ext * 2, ext * 2)
        else:
            # Add 10% padding on each side so shapes aren't flush to the edge
            pad_x = items_rect.width()  * 0.10 if items_rect.width()  > 0 else um_to_dbu(10)
            pad_y = items_rect.height() * 0.10 if items_rect.height() > 0 else um_to_dbu(10)
            fit_rect = items_rect.adjusted(-pad_x, -pad_y, pad_x, pad_y)
        self.fitInView(fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._current_zoom = self.transform().m11()
        self.zoom_changed.emit(self._current_zoom)

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
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def _do_pan(self, pos: QPointF) -> None:
        delta = pos - self._pan_start
        self._pan_start = pos
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - int(delta.x())
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() - int(delta.y())
        )

    # ── Rubber-band helpers ───────────────────────────────────────────────────

    def _item_under(self, viewport_pos) -> bool:
        """Return True if there is any interactive item under the viewport position."""
        scene_pos = self.mapToScene(viewport_pos.toPoint())
        items = self.scene().items(scene_pos)
        # Ignore purely decorative items that have no mouse buttons accepted
        for item in items:
            if item.acceptedMouseButtons() != Qt.MouseButton.NoButton:
                return True
        return False

    def _start_rubber_band(self) -> None:
        """Temporarily enable rubber-band drag for an empty-canvas drag gesture."""
        self._rubber_banding = True
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def _end_rubber_band(self) -> None:
        """Restore NoDrag after rubber-band selection finishes."""
        self._rubber_banding = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    # ── Drag-and-drop (receive from palette) ──────────────────────────────────

    _MIME_SHAPE = "application/x-gds-shape"
    _MIME_CELL  = "application/x-gds-cell"
    # Legacy alias so old code that references _MIME still works
    _MIME = _MIME_SHAPE

    def dragEnterEvent(self, event) -> None:
        md = event.mimeData()
        if md.hasFormat(self._MIME_SHAPE) or md.hasFormat(self._MIME_CELL):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        md = event.mimeData()
        if md.hasFormat(self._MIME_SHAPE) or md.hasFormat(self._MIME_CELL):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        md        = event.mimeData()
        scene_pos = self.mapToScene(event.position().toPoint())

        if md.hasFormat(self._MIME_SHAPE):
            raw   = md.data(self._MIME_SHAPE).data().decode()
            parts = raw.split(":")
            if len(parts) != 2:
                event.ignore()
                return
            kind_val = int(parts[0])
            layer    = int(parts[1])
            self.scene().drop_shape(kind_val, layer, scene_pos)
            event.acceptProposedAction()

        elif md.hasFormat(self._MIME_CELL):
            raw   = md.data(self._MIME_CELL).data().decode()
            # payload: "<cell_id>:<json_params>"
            sep   = raw.index(":")          # first colon separates id from json
            cell_id = raw[:sep]
            params  = json.loads(raw[sep + 1:])
            self.scene().drop_cell(cell_id, scene_pos, params=params)
            event.acceptProposedAction()

        else:
            event.ignore()

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
        # ── Pan: middle-button or Space+left ──────────────────────────────────
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_held
        ):
            self._start_pan(event.position())
            event.accept()
            return

        # ── Left click on empty canvas → start rubber-band selection ─────────
        # Only arm rubber-band when no interactive item is under the cursor so
        # that clicks/drags on items are delivered straight to the scene/items.
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._item_under(event.position())):
            self._start_rubber_band()
            # Fall through to super() so Qt can start the rubber-band gesture.

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pan_active:
            self._do_pan(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # End pan
        if self._pan_active and (
            event.button() == Qt.MouseButton.MiddleButton or
            event.button() == Qt.MouseButton.LeftButton
        ):
            self._end_pan()
            event.accept()
            return

        # End rubber-band — let super() commit the selection first, then clean up
        if self._rubber_banding and event.button() == Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            self._end_rubber_band()
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