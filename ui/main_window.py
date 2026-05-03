"""
ui/main_window.py — Top-level QMainWindow.

Phase 2 changes over Phase 1:
  - Toolbar: three mutually-exclusive tool buttons (Select / Rect / Polygon / Path)
    using QToolButton.setCheckable + an action group so only one is active at a time.
  - Status bar left segment shows current placement mode label from scene.
  - ESC shortcut in menu + always available globally (scene handles it too).
  - _on_place_mode_requested() — receives signal from palette, calls scene.set_mode().
  - _on_layer_change_requested() — wraps EditComponent in undo stack.
  - Palette signal renamed: place_mode_requested (not place_requested).
"""

from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QHBoxLayout, QMainWindow, QToolBar, QLabel, QVBoxLayout,
    QWidget, QSizePolicy, QMessageBox, QApplication, QToolButton,
    QDialog, QDialogButtonBox, QFileDialog
)
from PyQt6.QtGui import (
    QAction, QActionGroup, QKeySequence, QFont, QColor,
    QIcon, QPixmap, QPainter,
)
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot
import qtawesome as qta
from pathlib import Path
from ui.theme import Colors, Fonts, Geometry, apply_theme
from ui.canvas_scene import CanvasScene, PlacementMode, GroupItem
from ui.canvas_view import CanvasView
from ui.panels import ComponentPalette, PropertiesPanel
from core.model import DesignScene, GDSComponent, ComponentKind, ComponentGroup
from core.commands import CommandStack, EditComponent, GroupComponents, UngroupComponents
from ui.export_dialog import ExportResultDialog
from core.exporter import export_gds, ExportError
from core.serialiser import save, load, SerialisationError
from ui.sweep_dialog import SweepDialog
class MainWindow(QMainWindow):

    TITLE_BASE = "GDS Canvas Designer"

    def __init__(self) -> None:
        super().__init__()

        self._design = DesignScene(name="layout")
        self._current_file: Optional[Path] = None
        self._scene  = CanvasScene(self._design)
        self._view   = CanvasView(self._scene)

        apply_theme(QApplication.instance())

        self._build_window()
        self._build_menu_bar()
        self._build_status_bar()
        self._build_central_widget()
        self._wire_signals()

        self._update_title()
        self._update_undo_actions()

    # ── Window ────────────────────────────────────────────────────────────────

    def _build_window(self) -> None:
        self.setWindowTitle(self.TITLE_BASE)
        self.showMaximized()

    # ── Menu bar ──────────────────────────────────────────────────────────────

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("File")
        self._act_new    = self._action("New Design",      "Ctrl+N",         self._new_design)
        self._act_open = self._action("Open…", "Ctrl+O", self._open)
        self._act_save   = self._action("Save",            "Ctrl+S",         self._save)
        self._act_saveas = self._action("Save As…",        "Ctrl+Shift+S",   self._save_as)
        self._act_export = self._action("Export GDS…",     "Ctrl+E",         self._export_gds)
        self._act_quit   = self._action("Quit",            "Ctrl+Q",         self.close)
        for a in [self._act_new, self._act_open, None,
                self._act_save, self._act_saveas, None,
                self._act_export, None, self._act_quit]:
            file_menu.addSeparator() if a is None else file_menu.addAction(a)

        # Edit
        edit_menu = mb.addMenu("Edit")
        self._act_undo   = self._action("Undo",            "Ctrl+Z",         self._undo)
        self._act_redo   = self._action("Redo",            "Ctrl+Shift+Z",   self._redo)
        self._act_selall = self._action("Select All",      "Ctrl+A",         self._select_all)
        self._act_delete = self._action("Delete",          "Delete",         self._delete_selected)
        self._act_sweep = self._action("Sweep Parameter…", "Ctrl+W", self._sweep)
        self._act_group   = self._action("Group",   "Ctrl+G",       self._group_selected)
        self._act_ungroup = self._action("Ungroup", "Ctrl+Shift+G", self._ungroup_selected)
        self._act_escape = self._action("Cancel / Select", "Escape",         self._escape)
        for a in [self._act_undo, self._act_redo, None,
                self._act_selall, self._act_delete,
                self._act_sweep, self._act_group, self._act_ungroup,
                None, self._act_escape]:
            edit_menu.addSeparator() if a is None else edit_menu.addAction(a)

        # View
        view_menu = mb.addMenu("View")
        self._act_fit  = self._action("Fit All",           "F",              self._view.zoom_fit)
        self._act_zin  = self._action("Zoom In",           "Ctrl+=",         self._view.zoom_in)
        self._act_zout = self._action("Zoom Out",          "Ctrl+-",         self._view.zoom_out)
        for a in [self._act_fit, None, self._act_zin, self._act_zout]:
            view_menu.addSeparator() if a is None else view_menu.addAction(a)

        # Help
        help_menu = mb.addMenu("Help")
        self._about = self._action("About…", "", self._about)
        self._shortcuts = self._action("Keyboard Shortcuts", "Ctrl+H", self._shortcuts_help)
        for a in [self._about, None, self._shortcuts]:
            help_menu.addSeparator() if a is None else help_menu.addAction(a)
            
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

        # ── Undo / Redo ───────────────────────────────────────────────────────
        self._tb_undo = self._tb_button("fa5s.undo",  "Undo  Ctrl+Z",       self._undo)
        self._tb_redo = self._tb_button("fa5s.redo",  "Redo  Ctrl+Shift+Z", self._redo)
        self._toolbar.addSeparator()

        # ── Tool group: Select | Rect | Polygon | Path ────────────────────────
        # Checkable buttons, mutually exclusive via QActionGroup
        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)

        self._tb_select = self._tb_tool_button(
            "fa5s.mouse-pointer", "Select  (Esc)",
            lambda: self._enter_mode(PlacementMode.SELECT)
        )
        self._tb_select.setChecked(True)
        self._toolbar.addSeparator()

        # ── Zoom ─────────────────────────────────────────────────────────────
        self._tb_button("fa5s.search-plus",       "Zoom In  (+)",  self._view.zoom_in)
        self._tb_button("fa5s.search-minus",      "Zoom Out  (−)", self._view.zoom_out)
        self._tb_button("fa5s.expand-arrows-alt", "Fit All  (F)",  self._view.zoom_fit)
        self._toolbar.addSeparator()

        # ── Delete ────────────────────────────────────────────────────────────
        self._tb_button("fa5s.trash-alt", "Delete Selected  (Del)",
                        self._delete_selected, color=Colors.ERROR)
        
        # ── Sweep Parameter ────────────────────────────────────────────────────────────
        self._toolbar.addSeparator()
        self._tb_button("fa5s.sliders-h", "Sweep Parameter  (Ctrl+W)", self._sweep,
                color=Colors.ACCENT)
        
        # ── Export GDS ────────────────────────────────────────────────────────────
        self._toolbar.addSeparator()
        self._tb_button("fa5s.file-export", "Export GDS  (Ctrl+E)", self._export_gds, color=Colors.ACCENT)

        # ── Spacer + zoom readout ─────────────────────────────────────────────
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)

        self._tb_zoom_label = QLabel("100%")
        self._tb_zoom_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; padding-right: 12px;"
        )
        self._toolbar.addWidget(self._tb_zoom_label)

    def _tb_button(self, icon_name: str, tip: str, slot,
                   color: str = Colors.TEXT_SECONDARY) -> QAction:
        icon = qta.icon(icon_name, color=color, color_active=Colors.ACCENT)
        act  = QAction(icon, "", self)
        act.setToolTip(tip)
        act.triggered.connect(slot)
        self._toolbar.addAction(act)
        return act

    def _tb_tool_button(self, icon_name: str, tip: str, slot) -> QAction:
        """Checkable exclusive tool button."""
        icon = qta.icon(icon_name,
                        color=Colors.TEXT_SECONDARY,
                        color_active=Colors.ACCENT,
                        color_checked=Colors.ACCENT)
        act = QAction(icon, "", self)
        act.setToolTip(tip)
        act.setCheckable(True)
        act.triggered.connect(slot)
        self._tool_group.addAction(act)
        self._toolbar.addAction(act)
        return act

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        sb.setFixedHeight(Geometry.STATUSBAR_HEIGHT)

        def stat_label(text: str = "") -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-family: {Fonts.MONO_FAMILY}; "
                f"font-size: {Fonts.SIZE_XS}px; padding: 0 10px; "
                f"border-right: 1px solid {Colors.BG_BORDER};"
            )
            return lbl

        self._sb_mode   = stat_label("SELECT")
        self._sb_cursor = stat_label("X: 0.000  Y: 0.000 µm")
        self._sb_zoom   = stat_label("ZOOM")
        self._sb_count  = stat_label("0 components")
        self._sb_layer  = stat_label("LAYER  0")
        self._sb_msg    = QLabel("Ready")
        self._sb_msg.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; padding: 0 10px;"
        )

        for w in [self._sb_mode, self._sb_cursor, self._sb_zoom,
                  self._sb_count, self._sb_layer]:
            sb.addWidget(w)
        sb.addPermanentWidget(self._sb_msg)

    def _flash_status(self, msg: str, ms: int = 2500) -> None:
        self._sb_msg.setText(msg)
        QTimer.singleShot(ms, lambda: self._sb_msg.setText("Ready"))

    # ── Central widget ────────────────────────────────────────────────────────

    def _build_central_widget(self) -> None:
        self._palette = ComponentPalette()
        self._props   = PropertiesPanel()
        self._build_toolbar()

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._toolbar)
        cl.addWidget(self._view)

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._palette)
        rl.addWidget(center)
        rl.addWidget(self._props)

        self.setCentralWidget(root)

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        self._scene.cursor_moved.connect(self._on_cursor_moved)
        self._scene.item_selected.connect(self._on_item_selected)
        self._scene.item_hovered.connect(self._on_item_hovered)
        self._scene.scene_changed.connect(self._on_scene_changed)
        self._scene.mode_changed.connect(self._on_mode_changed)
        self._scene.connections_changed.connect(self._refresh_props_for_selection)
        self._scene.group_edit_entered.connect(
            lambda gid: self._flash_status(
                f"Editing group — click outside to exit", ms=0
            )
        )
        self._scene.group_edit_exited.connect(
            lambda: self._flash_status("Exited group edit")
        )
        self._scene.group_selected.connect(self._on_group_selected)



        # Phase 2: palette requests a mode, not an immediate placement
        self._palette.place_mode_requested.connect(self._on_place_mode_requested)

        # Phase 2: properties panel layer edit
        self._props.layer_change_requested.connect(self._on_layer_change_requested)

        # Phase 3: properties panel geometry edits (width / height / path_width)
        self._props.geometry_change_requested.connect(self._on_geometry_change_requested)

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
                self._props.show_component(comp, self._design)
                self._sb_layer.setText(f"LAYER  {comp.layer}")
                return
        self._props.clear()

    def _on_scene_changed(self) -> None:
        count = len(self._design.components)
        self._sb_count.setText(f"{count} component{'s' if count != 1 else ''}")
        self._update_title()
        self._update_undo_actions()

    @pyqtSlot(str)
    def _on_mode_changed(self, label: str) -> None:
        self._sb_mode.setText(label.split("  —")[0])   # show first segment in status bar

    @pyqtSlot(object, int)
    def _on_place_mode_requested(self, kind: ComponentKind, layer: int) -> None:
        """Palette button clicked — enter the corresponding placement mode."""
        mode_map = {
            ComponentKind.RECTANGLE: PlacementMode.PLACE_RECT,
            ComponentKind.POLYGON:   PlacementMode.PLACE_POLYGON,
            ComponentKind.PATH:      PlacementMode.PLACE_PATH,
        }
        self._enter_mode(mode_map[kind], layer)

    @pyqtSlot(str)
    def _on_item_hovered(self, comp_id: str) -> None:
        if comp_id:
            comp = self._design.get(comp_id)
            if comp:
                self._sb_msg.setText(
                    f"{comp.kind.name.capitalize()}  id={comp.id}  layer={comp.layer}"
                )
        else:
            self._sb_msg.setText("Ready")

    @pyqtSlot(str, int)
    def _on_layer_change_requested(self, comp_id: str, new_layer: int) -> None:
        """Properties panel layer spin changed — wrap in undo-able command."""
        comp = self._design.get(comp_id)
        if comp and comp.layer != new_layer:
            self._scene.cmd_stack.execute(EditComponent(comp, layer=new_layer))
            # Use the public API — never reach into _items directly.
            self._scene.refresh_item_style(comp_id)
            self._flash_status(f"Layer → {new_layer}")

    @pyqtSlot(str, str, int)
    def _on_geometry_change_requested(self, comp_id: str, field: str, value_dbu: int) -> None:
        """Properties panel dimension spinbox committed — wrap in undo-able command."""
        comp = self._design.get(comp_id)
        if comp is None:
            return
        # Guard: editingFinished fires even when nothing changed (click in, click out).
        if getattr(comp, field, None) == value_dbu:
            return
        self._scene.cmd_stack.execute(EditComponent(comp, **{field: value_dbu}))
        # Tell the Qt delegate to re-read the model — geometry changed.
        item = self._scene.item_for(comp_id)
        if item:
            item.sync_from_model()
        # Refresh the panel so bbox and spinbox values reflect the new state.
        self._props.show_component(comp, self._design)
        label = field.replace("_", " ").title()
        self._flash_status(f"{label} → {value_dbu / 1000:.3f} µm")

    @pyqtSlot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        label = f"ZOOM  {zoom * 1000:.2f} px/µm"
        self._sb_zoom.setText(label)
        self._tb_zoom_label.setText(f"{zoom * 1000:.2f} px/µm")

    @pyqtSlot()
    def _sweep(self) -> None:
        selected = [
            item.component
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        ]
        if len(selected) != 1:
            QMessageBox.information(
                self, "Sweep", "Select exactly one component to sweep."
            )
            return
        dlg = SweepDialog(selected[0], self._design, self._scene.cmd_stack, self)
        dlg.exec()

    @pyqtSlot()
    def _refresh_props_for_selection(self) -> None:
        """Re-populate the properties panel after a wiring change."""
        selected = self._scene.selectedItems()
        if len(selected) == 1 and hasattr(selected[0], "component"):
            self._props.show_component(selected[0].component, self._design)

    @pyqtSlot()
    def _group_selected(self) -> None:
        selected = [
            item.component
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        ]
        if len(selected) < 2:
            QMessageBox.information(
                self, "Group", "Select two or more components to group."
            )
            return

        # Validate: all must share the same layer
        layers = {c.layer for c in selected}
        if len(layers) > 1:
            QMessageBox.warning(
                self, "Group",
                "All components must be on the same layer to group.\n"
                f"Selected layers: {sorted(layers)}"
            )
            return

        # Name: ask or auto-generate
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Group Name", "Group name:",
            text=f"group_{len(self._design.groups) + 1}"
        )
        if not ok or not name.strip():
            return

        comp_ids = [c.id for c in selected]
        self._scene.cmd_stack.execute(GroupComponents(comp_ids, name.strip()))
        self._flash_status(f"Grouped {len(selected)} components as '{name.strip()}'")

    @pyqtSlot()
    def _ungroup_selected(self) -> None:
        # Find groups that are selected or whose members are selected
        selected_comp_ids = {
            item.component.id
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        }
        # Also check if a GroupItem is directly selected
        selected_group_ids = {
            item.group.id
            for item in self._scene.selectedItems()
            if isinstance(item, GroupItem)  # import GroupItem at top
        }
        # Add groups whose members are all selected
        for g in self._design.groups:
            if any(cid in selected_comp_ids for cid in g.member_ids):
                selected_group_ids.add(g.id)

        if not selected_group_ids:
            QMessageBox.information(self, "Ungroup", "No grouped components selected.")
            return

        for gid in selected_group_ids:
            group = self._design.get_group(gid)
            if group:
                self._scene.cmd_stack.execute(UngroupComponents(group))
        self._flash_status(f"Ungrouped {len(selected_group_ids)} group(s)")

    @pyqtSlot(str)
    def _on_group_selected(self, group_id: str) -> None:
        group = self._design.get_group(group_id)
        if group:
            self._props.show_group(group, self._design)

    # ── Mode helpers ──────────────────────────────────────────────────────────

    def _enter_mode(self, mode: PlacementMode, layer: Optional[int] = None) -> None:
        if layer is None:
            layer = self._palette.active_layer
        self._scene.set_mode(mode, layer)  

        # Select mode uses the standard arrow; placement modes use the crosshair.
        if mode == PlacementMode.SELECT:
            self._tb_select.setChecked(True)
            self._view.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self._view.setCursor(Qt.CursorShape.CrossCursor)

    def _escape(self) -> None:
        self._scene.cancel_placement()
        self._tb_select.setChecked(True)

    # ── Edit actions ──────────────────────────────────────────────────────────

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
        self._act_undo.setText(
            f"Undo  {self._scene.cmd_stack.undo_description}" if can_undo else "Undo"
        )
        self._act_redo.setText(
            f"Redo  {self._scene.cmd_stack.redo_description}" if can_redo else "Redo"
        )

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
            self._flash_status(
                f"Deleted {len(selected)} component{'s' if len(selected) > 1 else ''}"
            )

    # ── File actions ──────────────────────────────────────────────────────────
    def _new_design(self) -> None:
        if not self._maybe_save_before("start a new design"):
            return
        self._design.clear()
        self._scene.cmd_stack.clear()
        self._scene.reset()
        self._current_file = None
        self._update_title()
        self._flash_status("New design created")

    def _save(self) -> None:
        if self._current_file:
            self._do_save(self._current_file)
        else:
            self._save_as()

    def _save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Design",
            str(self._current_file or f"{self._design.name}.json"),
            "GDS Canvas Files (*.json);;All Files (*)",
        )
        if path:
            self._do_save(Path(path))

    def _do_save(self, path: Path) -> None:
        try:
            save(self._design, path)
            self._current_file  = path
            self._design.is_dirty = False
            self._update_title()
            self._flash_status(f"Saved → {path.name}")
        except SerialisationError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))

    def _open(self) -> None:
        if not self._maybe_save_before("open a file"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Design", "",
            "GDS Canvas Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            new_design = load(path)
        except SerialisationError as exc:
            QMessageBox.critical(self, "Open Failed", str(exc))
            return

        # Swap in the loaded design
        self._design = new_design
        self._scene._design = new_design
        self._scene.cmd_stack = type(self._scene.cmd_stack)(new_design)
        self._scene.cmd_stack.connect_change(self._scene._on_model_changed)
        self._scene._on_model_changed()

        self._current_file = Path(path)
        self._update_title()
        self._flash_status(f"Opened {Path(path).name}")
        self._view.zoom_fit()

    def _maybe_save_before(self, action: str) -> bool:
        """
        If the design is dirty, prompt Save / Discard / Cancel.
        Returns True if the caller should proceed, False if the user cancelled.
        """
        if not self._design.is_dirty:
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            f"Save changes before you {action}?",
            QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Save:
            self._save()
            # If save was cancelled (e.g. no path chosen yet and user dismissed dialog)
            return not self._design.is_dirty
        if reply == QMessageBox.StandardButton.Discard:
            return True
        return False   # Cancel

    def _update_title(self) -> None:
        name  = self._current_file.name if self._current_file else self._design.name
        dirty = " •" if self._design.is_dirty else ""
        self.setWindowTitle(f"{self.TITLE_BASE} — {name}{dirty}")

    def closeEvent(self, event) -> None:
        if self._maybe_save_before("quit"):
            event.accept()
        else:
            event.ignore()
        
    def _export_gds(self) -> None:
        if not self._design.components:
            QMessageBox.warning(self, "Export GDS", "Nothing to export — add some shapes first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export GDS", f"{self._design.name}.gds",
            "GDS Files (*.gds);;All Files (*)",
        )
        if not path:
            return

        # Identity map: app layer N → GDS (layer=N, datatype=0)
        layers = {c.layer for c in self._design.components}
        layer_map = {layer: (layer, 0) for layer in layers}

        try:
            summary = export_gds(self._design, path, layer_map)
        except ExportError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
            return

        self._flash_status(f"Exported {summary['shapes']} shapes to {path}")
        result_dlg = ExportResultDialog(summary, self)
        result_dlg.exec()

    # ── About / help ──────────────────────────────────────────────────────────

    def _about(self) -> None:
        QMessageBox.about(
            self, "GDS Canvas Designer",
            "<b>GDS Canvas Designer</b><br>"
            "Version 0.2.0 — Phase 2<br><br>"
            "EDA layout editor built with PyQt6.",
        )

    def _shortcuts_help(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)
        lbl = QLabel(
            "<b>Placing Shapes</b><br>"
            "Drag Rectangle / Polygon / Path from the left panel onto the canvas<br><br>"
            "<b>Polygon / Path placement</b><br>"
            "Left-click - add vertex<br>"
            "Double-click or Enter - commit shape<br>"
            "Esc - cancel<br><br>"
            "<b>Navigation</b><br>"
            "F - Fit all | + / - Zoom<br>"
            "Ctrl+Scroll - Zoom<br>"
            "Middle-drag or Space+drag - Pan<br><br>"
            "<b>Edit</b><br>"
            "Ctrl+Z / Ctrl+Shift+Z - Undo / Redo<br>"
            "Ctrl+A - Select all | Delete - Delete selected<br>"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lbl.setContentsMargins(12, 12, 12, 12)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(lbl)
        layout.addWidget(buttons)
        dlg.exec()

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