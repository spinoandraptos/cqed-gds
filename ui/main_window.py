"""
ui/main_window.py — Top-level QMainWindow.

Assembles:
  - Menu bar with full keyboard shortcuts
  - Toolbar with tool buttons
  - Canvas scene + view (center)
  - Component palette dock (left)
  - Properties panel dock (right)
  - Status bar with cursor coords, zoom, component count
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QHBoxLayout, QMainWindow, QDockWidget, QToolBar, QStatusBar,
    QLabel, QVBoxLayout, QWidget, QSizePolicy, QMessageBox,
    QApplication,
)
from PyQt6.QtGui import (
    QAction, QKeySequence, QFont, QColor, QPalette,
    QIcon, QPixmap, QPainter,
)
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot
import qtawesome as qta

from ui.theme import Colors, Fonts, Geometry, apply_theme
from ui.canvas_scene import CanvasScene
from ui.canvas_view import CanvasView
from ui.panels import ComponentPalette, PropertiesPanel
from core.model import DesignScene, GDSComponent, ComponentKind
from core.commands import CommandStack


def _text_icon(text: str, color: str = Colors.TEXT_SECONDARY) -> QIcon:
    """Create a simple text-label icon for toolbar buttons."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont("monospace", 11, QFont.Weight.Medium)
    p.setFont(font)
    p.setPen(QColor(color))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):

    TITLE_BASE = "GDS Canvas Designer"

    def __init__(self) -> None:
        super().__init__()

        # ── Model ─────────────────────────────────────────────────────────────
        self._design = DesignScene(name="TOP")
        self._scene  = CanvasScene(self._design)
        self._view   = CanvasView(self._scene)

        # ── Apply theme before building UI ────────────────────────────────────
        apply_theme(QApplication.instance())

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_window()
        self._build_menu_bar()
        self._build_status_bar()
        self._build_central_widget()
        self._wire_signals()

        # ── Window state ──────────────────────────────────────────────────────
        self._update_title()
        self._update_undo_actions()

    # ── Window setup ─────────────────────────────────────────────────────────

    def _build_window(self) -> None:
        self.setWindowTitle(self.TITLE_BASE)
        self.setMinimumSize(900, 600)
        self.showMaximized()

    # ── Menu bar ─────────────────────────────────────────────────────────────

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("File")
        self._act_new    = self._action("New Design",    "Ctrl+N",  lambda: self._new_design())
        self._act_save   = self._action("Save",          "Ctrl+S",  lambda: self._save())
        self._act_saveas = self._action("Save As…",      "Ctrl+Shift+S", lambda: self._save_as())
        self._act_export = self._action("Export GDS…",   "Ctrl+E",  lambda: self._export_gds())
        self._act_quit   = self._action("Quit",          "Ctrl+Q",  self.close)
        for act in [self._act_new, self._act_save, self._act_saveas, None,
                    self._act_export, None, self._act_quit]:
            if act is None:
                file_menu.addSeparator()
            else:
                file_menu.addAction(act)

        # Edit
        edit_menu = mb.addMenu("Edit")
        self._act_undo   = self._action("Undo",         "Ctrl+Z",  self._undo)
        self._act_redo   = self._action("Redo",         "Ctrl+Shift+Z", self._redo)
        self._act_selall = self._action("Select All",   "Ctrl+A",  self._select_all)
        self._act_delete = self._action("Delete",       "Delete",  self._delete_selected)
        for act in [self._act_undo, self._act_redo, None,
                    self._act_selall, self._act_delete]:
            if act is None:
                edit_menu.addSeparator()
            else:
                edit_menu.addAction(act)

        # View
        view_menu = mb.addMenu("View")
        self._act_fit    = self._action("Fit All",      "F",       self._view.zoom_fit)
        self._act_zin    = self._action("Zoom In",      "Ctrl+=",  self._view.zoom_in)
        self._act_zout   = self._action("Zoom Out",     "Ctrl+-",  self._view.zoom_out)
        for act in [self._act_fit, None, self._act_zin, self._act_zout]:
            if act is None:
                view_menu.addSeparator()
            else:
                view_menu.addAction(act)

        # Help
        help_menu = mb.addMenu("Help")
        help_menu.addAction(self._action("About…", "", self._about))
        help_menu.addAction(self._action("Keyboard Shortcuts", "?", self._shortcuts_help))

    @staticmethod
    def _action(label: str, shortcut: str, slot) -> QAction:
        act = QAction(label)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        return act

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        self._toolbar = QToolBar("Main Toolbar")
        self._toolbar.setMovable(False)
        self._toolbar.setFloatable(False)
        self._toolbar.setIconSize(QSize(18, 18))
        self._toolbar.setFixedHeight(Geometry.TOOLBAR_HEIGHT)
        self._toolbar.setStyleSheet(
            f"QToolBar {{ border-bottom: 1px solid {Colors.BG_BORDER}; }}"
        )

        # Undo / Redo
        self._tb_undo = self._toolbar_button(self._toolbar, "fa5s.undo",  "Undo  (Ctrl+Z)",       self._undo)
        self._tb_redo = self._toolbar_button(self._toolbar, "fa5s.redo",  "Redo  (Ctrl+Shift+Z)", self._redo)
        self._toolbar.addSeparator()

        # Zoom
        self._toolbar_button(self._toolbar, "fa5s.search-plus",       "Zoom In  (+)",  self._view.zoom_in)
        self._toolbar_button(self._toolbar, "fa5s.search-minus",      "Zoom Out  (−)", self._view.zoom_out)
        self._toolbar_button(self._toolbar, "fa5s.expand-arrows-alt", "Fit All  (F)",  self._view.zoom_fit)
        self._toolbar.addSeparator()

        # Place
        self._toolbar_button(self._toolbar, "fa5s.vector-square", "Place Rectangle",        self._quick_place_rect)
        self._toolbar.addSeparator()

        # Delete
        self._toolbar_button(self._toolbar, "fa5s.trash-alt", "Delete Selected  (Del)", self._delete_selected, color=Colors.ERROR)

        # Spacer + zoom label
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)

        self._tb_zoom_label = QLabel("100%")
        self._tb_zoom_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; "
            f"font-family: {Fonts.MONO_FAMILY}; "
            f"padding-right: 12px;"
        )
        self._toolbar.addWidget(self._tb_zoom_label)

    def _toolbar_button(self, tb: QToolBar, icon_name: str, tip: str, slot, color: str = Colors.TEXT_SECONDARY) -> QAction:
        icon = qta.icon(icon_name, color=color, color_active=Colors.ACCENT)
        act = QAction(icon, "", self)
        act.setToolTip(tip)
        act.triggered.connect(slot)
        tb.addAction(act)
        return act
    
    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        sb.setFixedHeight(Geometry.STATUSBAR_HEIGHT)

        def stat_label(text: str = "") -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; "
                f"font-family: {Fonts.MONO_FAMILY}; "
                f"font-size: {Fonts.SIZE_XS}px; "
                f"padding: 0 10px; "
                f"border-right: 1px solid {Colors.BG_BORDER};"
            )
            return lbl

        self._sb_mode    = stat_label("SELECT")
        self._sb_cursor  = stat_label("X: 0.000  Y: 0.000 µm")
        self._sb_zoom    = stat_label("ZOOM  100%")
        self._sb_count   = stat_label("0 components")
        self._sb_layer   = stat_label("LAYER  0")
        self._sb_msg     = QLabel("Ready")
        self._sb_msg.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"font-size: {Fonts.SIZE_XS}px; "
            f"padding: 0 10px;"
        )

        for w in [self._sb_mode, self._sb_cursor, self._sb_zoom, self._sb_count, self._sb_layer]:
            sb.addWidget(w)
        sb.addPermanentWidget(self._sb_msg)

    def _flash_status(self, msg: str, duration_ms: int = 2500) -> None:
        self._sb_msg.setText(msg)
        QTimer.singleShot(duration_ms, lambda: self._sb_msg.setText("Ready"))

    def _build_central_widget(self) -> None:
        # Panels
        self._palette = ComponentPalette()
        self._props   = PropertiesPanel()

        # Center column: toolbar on top, canvas below, statusbar at bottom
        self._build_toolbar()   # still builds self._toolbar, just don't add it yet

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addWidget(self._toolbar)
        center_layout.addWidget(self._view)

        # Three-column root layout
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._palette)
        root_layout.addWidget(center)
        root_layout.addWidget(self._props)

        self.setCentralWidget(root)

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        # Canvas → status bar cursor coords
        self._scene.cursor_moved.connect(self._on_cursor_moved)

        # Canvas → properties panel
        self._scene.item_selected.connect(self._on_item_selected)

        # Canvas → title bar dirty indicator
        self._scene.scene_changed.connect(self._on_scene_changed)

        # Component palette → place component
        self._palette.place_requested.connect(self._on_place_requested)

        # View zoom → toolbar label
        self._view.zoom_changed.connect(self._on_zoom_changed)

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(float, float)
    def _on_cursor_moved(self, x_um: float, y_um: float) -> None:
        self._sb_cursor.setText(f"X: {x_um:+9.3f}  Y: {y_um:+9.3f} µm")

    @pyqtSlot(str)
    def _on_item_selected(self, comp_id: str) -> None:
        if comp_id:
            comp = self._design.get(comp_id)
            if comp:
                self._props.show_component(comp)
                self._sb_layer.setText(f"LAYER  {comp.layer}")
                return
        self._props.clear()

    def _on_scene_changed(self) -> None:
        count = len(self._design.components)
        self._sb_count.setText(f"{count} component{'s' if count != 1 else ''}")
        self._update_title()
        self._update_undo_actions()

    @pyqtSlot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        pct = int(zoom * 100_000)     # 1 px = 1 DBU (nm), so px/nm × 1000 → px/µm
        # Better: just show a readable zoom multiplier
        label = f"ZOOM  {zoom * 100_000:.0f}×"  if zoom < 0.0001 else f"ZOOM  {zoom * 1000:.2f} px/µm"
        self._sb_zoom.setText(label)
        self._tb_zoom_label.setText(f"{zoom * 1000:.2f} px/µm")

    @pyqtSlot(int, int)
    def _on_place_requested(self, kind: int, layer: int) -> None:
        """Place a default-sized component at the view center."""
        center = self._view.mapToScene(
            self._view.viewport().rect().center()
        )
        x_um = center.x() / 1000.0
        y_um = center.y() / 1000.0

        if ComponentKind(kind) == ComponentKind.RECTANGLE:
            comp = self._scene.place_rectangle(
                origin_x_um = x_um - 5,
                origin_y_um = y_um - 2.5,
                width_um    = 10.0,
                height_um   = 5.0,
                layer       = layer,
            )
            self._flash_status(f"Placed rectangle on layer {layer}")
        else:
            self._flash_status("Shape not yet implemented")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _undo(self) -> None:
        desc = self._scene.cmd_stack.undo()
        if desc:
            self._flash_status(f"Undo: {desc}")
        self._update_undo_actions()

    def _redo(self) -> None:
        desc = self._scene.cmd_stack.redo()
        if desc:
            self._flash_status(f"Redo: {desc}")
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        can_undo = self._scene.cmd_stack.can_undo
        can_redo = self._scene.cmd_stack.can_redo
        self._act_undo.setEnabled(can_undo)
        self._act_redo.setEnabled(can_redo)
        self._tb_undo.setEnabled(can_undo)
        self._tb_redo.setEnabled(can_redo)
        if can_undo:
            self._act_undo.setText(f"Undo  {self._scene.cmd_stack.undo_description}")
        else:
            self._act_undo.setText("Undo")
        if can_redo:
            self._act_redo.setText(f"Redo  {self._scene.cmd_stack.redo_description}")
        else:
            self._act_redo.setText("Redo")

    def _select_all(self) -> None:
        for item in self._scene.items():
            item.setSelected(True)

    def _delete_selected(self) -> None:
        from core.commands import RemoveComponent
        selected = [
            item.component
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        ]
        for comp in selected:
            self._scene.cmd_stack.execute(RemoveComponent(comp))
        if selected:
            self._flash_status(f"Deleted {len(selected)} component{'s' if len(selected)>1 else ''}")

    def _quick_place_rect(self) -> None:
        self._on_place_requested(ComponentKind.RECTANGLE, self._palette.active_layer)

    def _new_design(self) -> None:
        if self._design.is_dirty:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "Discard unsaved changes and start a new design?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Discard:
                return
        self._design.clear()
        self._scene.cmd_stack.clear()
        self._design.is_dirty = False
        self._scene._on_model_changed()
        self._flash_status("New design created")

    def _save(self) -> None:
        # Phase 1: save as JSON project file (stub)
        self._flash_status("Save not yet implemented — coming Phase 4")

    def _save_as(self) -> None:
        self._flash_status("Save As not yet implemented — coming Phase 4")

    def _export_gds(self) -> None:
        self._flash_status("GDS export not yet implemented — coming Phase 4")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "GDS Canvas Designer",
            "<b>GDS Canvas Designer</b><br>"
            "Version 0.1.0 — Phase 1 Skeleton<br><br>"
            "An open-source GDSII layout editor.<br>"
            "Built with PyQt6 + gdstk.",
        )

    def _shortcuts_help(self) -> None:
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            "<b>Navigation</b><br>"
            "F — Fit all<br>"
            "+ / − — Zoom in / out<br>"
            "Ctrl+Scroll — Zoom<br>"
            "Middle-drag or Space+drag — Pan<br><br>"
            "<b>Edit</b><br>"
            "Ctrl+Z — Undo<br>"
            "Ctrl+Shift+Z — Redo<br>"
            "Ctrl+A — Select all<br>"
            "Delete — Delete selected<br><br>"
            "<b>Place</b><br>"
            "Click ▭ in toolbar or palette → Rectangle<br>",
        )

    # ── Title bar ─────────────────────────────────────────────────────────────

    def _update_title(self) -> None:
        dirty = " •" if self._design.is_dirty else ""
        self.setWindowTitle(f"{self.TITLE_BASE} — {self._design.name}{dirty}")

    # ── Close guard ───────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._design.is_dirty:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Quit anyway?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        event.accept()
