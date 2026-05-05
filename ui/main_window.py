"""
ui/main_window.py — Top-level QMainWindow for GDS Canvas Designer.

Responsibilities:
  - Menu bar, toolbar, status bar, and central widget layout.
  - Wiring scene/view/panel signals to application-level slots.
  - File I/O (new, open, save, save-as, export).
  - Edit actions (undo/redo, copy/paste/duplicate, group/ungroup, delete, sweep).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import qtawesome as qta
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox,
    QSizePolicy, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from core.cell_library import CELL_BY_ID, Point, place_cell
from core.clipboard import Clipboard
from core.commands import (
    BatchCommand, EditComponent, GroupComponents, MergeGroups,
    RemoveComponent, RemoveGroup, ReplaceCellCmd, UngroupComponents,
)
from core.exporter import ExportError, export_gds
from core.model import ComponentKind, DesignScene
from core.serialiser import SerialisationError, load, save
from ui.canvas_scene import CanvasScene, GroupItem, PlacementMode
from ui.canvas_view import CanvasView
from ui.cell_palette import CellLibraryPanel
from ui.export_dialog import ExportResultDialog
from ui.panels import ComponentPalette, PropertiesPanel
from ui.sweep_dialog import CellSweepDialog, GroupSweepDialog, SweepDialog
from ui.theme import Colors, Fonts, Geometry, apply_theme


class MainWindow(QMainWindow):

    TITLE_BASE = "GDS Canvas Designer"

    # Maps ComponentKind → the PlacementMode the palette requests.
    _KIND_TO_MODE: dict[ComponentKind, PlacementMode] = {
        ComponentKind.RECTANGLE: PlacementMode.PLACE_RECT,
        ComponentKind.POLYGON:   PlacementMode.PLACE_POLYGON,
        ComponentKind.PATH:      PlacementMode.PLACE_PATH,
    }

    def __init__(self) -> None:
        super().__init__()

        self._design            = DesignScene(name="layout")
        self._current_file:     Optional[Path] = None
        self._selected_group_id: Optional[str] = None

        self._scene = CanvasScene(self._design)
        self._view  = CanvasView(self._scene)

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
        self._build_file_menu(mb)
        self._build_edit_menu(mb)
        self._build_view_menu(mb)
        self._build_help_menu(mb)

    def _build_file_menu(self, mb) -> None:
        menu = mb.addMenu("File")
        self._act_new    = self._action("New Design",    "Ctrl+N",       self._new_design)
        self._act_open   = self._action("Open…",         "Ctrl+O",       self._open)
        self._act_save   = self._action("Save",          "Ctrl+S",       self._save)
        self._act_saveas = self._action("Save As…",      "Ctrl+Shift+S", self._save_as)
        self._act_export = self._action("Export GDS…",   "Ctrl+E",       self._export_gds)
        self._act_quit   = self._action("Quit",          "Ctrl+Q",       self.close)
        self._populate_menu(menu, [
            self._act_new, self._act_open, None,
            self._act_save, self._act_saveas, None,
            self._act_export, None,
            self._act_quit,
        ])

    def _build_edit_menu(self, mb) -> None:
        menu = mb.addMenu("Edit")
        self._act_undo      = self._action("Undo",               "Ctrl+Z",       self._undo)
        self._act_redo      = self._action("Redo",               "Ctrl+Shift+Z", self._redo)
        self._act_copy      = self._action("Copy",               "Ctrl+C",       self._copy)
        self._act_paste     = self._action("Paste",              "Ctrl+V",       self._paste)
        self._act_duplicate = self._action("Duplicate",          "Ctrl+D",       self._duplicate)
        self._act_selall    = self._action("Select All",         "Ctrl+A",       self._select_all)
        self._act_delete    = self._action("Delete",             "Delete",       self._delete_selected)
        self._act_sweep     = self._action("Sweep Parameter…",   "Ctrl+W",       self._sweep)
        self._act_group     = self._action("Group",              "Ctrl+G",       self._group_selected)
        self._act_ungroup   = self._action("Ungroup",            "Ctrl+Shift+G", self._ungroup_selected)
        self._act_rot_cw    = self._action("Rotate 90° CW",      "R",            lambda: self._scene.rotate_selection(ccw=False))
        self._act_rot_ccw   = self._action("Rotate 90° CCW",     "Shift+R",      lambda: self._scene.rotate_selection(ccw=True))
        self._act_escape    = self._action("Cancel / Select",    "Escape",       self._escape)
        self._populate_menu(menu, [
            self._act_undo, self._act_redo, None,
            self._act_copy, self._act_paste, self._act_duplicate, None,
            self._act_selall, self._act_delete,
            self._act_sweep, self._act_group, self._act_ungroup,
            self._act_rot_cw, self._act_rot_ccw, None,
            self._act_escape,
        ])

    def _build_view_menu(self, mb) -> None:
        menu = mb.addMenu("View")
        self._act_fit  = self._action("Fit All",  "F",      self._view.zoom_fit)
        self._act_zin  = self._action("Zoom In",  "Ctrl+=", self._view.zoom_in)
        self._act_zout = self._action("Zoom Out", "Ctrl+-", self._view.zoom_out)
        self._populate_menu(menu, [self._act_fit, None, self._act_zin, self._act_zout])

    def _build_help_menu(self, mb) -> None:
        menu = mb.addMenu("Help")
        self._act_about     = self._action("About…",             "",       self._about)
        self._act_shortcuts = self._action("Keyboard Shortcuts", "Ctrl+H", self._shortcuts_help)
        self._populate_menu(menu, [self._act_about, None, self._act_shortcuts])

    @staticmethod
    def _action(label: str, shortcut: str, slot) -> QAction:
        act = QAction(label)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        return act

    @staticmethod
    def _populate_menu(menu, items: list) -> None:
        """Add actions to a menu; None inserts a separator."""
        for item in items:
            if item is None:
                menu.addSeparator()
            else:
                menu.addAction(item)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        self._toolbar = QToolBar("Main Toolbar")
        self._toolbar.setMovable(False)
        self._toolbar.setFloatable(False)
        self._toolbar.setIconSize(QSize(18, 18))
        self._toolbar.setFixedHeight(Geometry.TOOLBAR_HEIGHT)

        self._tb_undo = self._tb_button("fa5s.undo",  "Undo  Ctrl+Z",       self._undo)
        self._tb_redo = self._tb_button("fa5s.redo",  "Redo  Ctrl+Shift+Z", self._redo)
        self._toolbar.addSeparator()

        self._tb_select = self._tb_button(
            "fa5s.mouse-pointer", "Select  (Esc)",
            lambda: self._enter_mode(PlacementMode.SELECT),
        )
        self._toolbar.addSeparator()

        self._tb_button("fa5s.search-plus",       "Zoom In  (+)",  self._view.zoom_in)
        self._tb_button("fa5s.search-minus",      "Zoom Out  (−)", self._view.zoom_out)
        self._tb_button("fa5s.expand-arrows-alt", "Fit All  (F)",  self._view.zoom_fit)
        self._toolbar.addSeparator()

        self._tb_group   = self._tb_button("fa5s.object-group",   "Group Selected  (Ctrl+G)",         self._group_selected)
        self._tb_ungroup = self._tb_button("fa5s.object-ungroup", "Ungroup Selected  (Ctrl+Shift+G)", self._ungroup_selected)
        self._toolbar.addSeparator()

        self._tb_rot_cw  = self._tb_button("fa5s.redo-alt",  "Rotate 90° CW  (R)",       lambda: self._scene.rotate_selection(ccw=False))
        self._tb_rot_ccw = self._tb_button("fa5s.undo-alt",  "Rotate 90° CCW  (Shift+R)", lambda: self._scene.rotate_selection(ccw=True))
        self._toolbar.addSeparator()

        self._tb_button("fa5s.trash-alt",  "Delete Selected  (Del)",    self._delete_selected, color=Colors.ERROR)
        self._toolbar.addSeparator()
        self._tb_button("fa5s.sliders-h",  "Sweep Parameter  (Ctrl+W)", self._sweep,           color=Colors.ACCENT)
        self._toolbar.addSeparator()
        self._tb_button("fa5s.file-export","Export GDS  (Ctrl+E)",       self._export_gds,      color=Colors.ACCENT)

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

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        sb.setFixedHeight(Geometry.STATUSBAR_HEIGHT)

        self._sb_mode   = self._stat_label("SELECT")
        self._sb_cursor = self._stat_label("X: 0.000  Y: 0.000 µm")
        self._sb_zoom   = self._stat_label("ZOOM")
        self._sb_count  = self._stat_label("0 components")
        self._sb_layer  = self._stat_label("LAYER  0")

        self._sb_msg = QLabel("Ready")
        self._sb_msg.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_XS}px; padding: 0 10px;"
        )

        for w in [self._sb_mode, self._sb_cursor, self._sb_zoom, self._sb_count, self._sb_layer]:
            sb.addWidget(w)
        sb.addPermanentWidget(self._sb_msg)

    @staticmethod
    def _stat_label(text: str = "") -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-family: {Fonts.MONO_FAMILY}; "
            f"font-size: {Fonts.SIZE_XS}px; padding: 0 10px; "
            f"border-right: 1px solid {Colors.BG_BORDER};"
        )
        return lbl

    def _flash_status(self, msg: str, ms: int = 2500) -> None:
        self._sb_msg.setText(msg)
        QTimer.singleShot(ms, lambda: self._sb_msg.setText("Ready"))

    # ── Central widget ────────────────────────────────────────────────────────

    def _build_central_widget(self) -> None:
        self._palette      = ComponentPalette()
        self._cell_palette = CellLibraryPanel()
        self._props        = PropertiesPanel()
        self._build_toolbar()

        left_tabs = QTabWidget()
        left_tabs.setFixedWidth(220)
        left_tabs.setStyleSheet(
            "QTabWidget::pane { border: none; margin: 0; padding: 0; }"
            "QTabBar::tab { padding: 6px 12px; font-size: 11px; }"
        )
        left_tabs.addTab(self._palette,      "Shapes")
        left_tabs.addTab(self._cell_palette, "Cells")

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
        rl.addWidget(left_tabs)
        rl.addWidget(center)
        rl.addWidget(self._props)

        self.setCentralWidget(root)

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        scene = self._scene

        scene.cursor_moved.connect(self._on_cursor_moved)
        scene.item_selected.connect(self._on_item_selected)
        scene.item_hovered.connect(self._on_item_hovered)
        scene.scene_changed.connect(self._on_scene_changed)
        scene.mode_changed.connect(self._on_mode_changed)
        scene.connections_changed.connect(self._refresh_props_for_selection)
        scene.group_selected.connect(self._on_group_selected)
        scene.group_edit_entered.connect(
            lambda _: self._flash_status("Editing group — click outside to exit")
        )
        scene.group_edit_exited.connect(
            lambda: self._flash_status("Exited group edit")
        )

        self._palette.place_mode_requested.connect(self._on_place_mode_requested)
        self._props.cell_param_change_requested.connect(self._on_cell_param_change_requested)
        self._props.layer_change_requested.connect(self._on_layer_change_requested)
        self._props.geometry_change_requested.connect(self._on_geometry_change_requested)
        self._view.zoom_changed.connect(self._on_zoom_changed)

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(float, float)
    def _on_cursor_moved(self, x_um: float, y_um: float) -> None:
        self._sb_cursor.setText(f"X: {x_um:+9.3f}  Y: {y_um:+9.3f} µm")

    @pyqtSlot(str)
    def _on_item_selected(self, comp_id: str) -> None:
        self._selected_group_id = None
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
        self._sb_mode.setText(label.split("  —")[0])

    @pyqtSlot(object, int)
    def _on_place_mode_requested(self, kind: ComponentKind, layer: int) -> None:
        self._enter_mode(self._KIND_TO_MODE[kind], layer)

    @pyqtSlot(str, str, str, object)
    def _on_cell_param_change_requested(self, group_id: str, cell_id: str,
                                        param_key: str, new_value) -> None:
        """
        Re-place a parametric cell in-place with an updated parameter value.
        Wrapped in a single undo-able ReplaceCellCmd so the change is atomic.
        """
        group = self._design.get_group(group_id)
        if group is None:
            return
        cdef = CELL_BY_ID.get(cell_id)
        if cdef is None:
            return

        # Build updated params: defaults → any stored overrides → this change.
        params = dict(cdef.defaults)
        if hasattr(group, "_cell_params"):
            params.update(group._cell_params)
        params[param_key] = new_value

        bb     = group.bbox_from(self._design.components)
        origin = Point(bb.x_min, bb.y_min)

        try:
            new_result = place_cell(cell_id, origin, params=params)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Cell Parameter", str(exc))
            return

        old_comp_ids   = list(group.member_ids)
        old_comps      = [c for c in (self._design.get(cid) for cid in old_comp_ids) if c]
        old_group_name = group.name

        self._scene.cmd_stack.execute(
            ReplaceCellCmd(
                self._design, self._scene, new_result, cdef,
                param_key, cell_id, params,
                group_id, old_group_name, old_comp_ids, old_comps,
            )
        )
        self._flash_status(f"Updated {cdef.name}: {param_key} = {new_value}")

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
        comp = self._design.get(comp_id)
        if comp and comp.layer != new_layer:
            self._scene.cmd_stack.execute(EditComponent(comp, layer=new_layer))
            self._scene.refresh_item_style(comp_id)
            self._flash_status(f"Layer → {new_layer}")

    @pyqtSlot(str, str, int)
    def _on_geometry_change_requested(self, comp_id: str, field: str, value_dbu: int) -> None:
        comp = self._design.get(comp_id)
        if comp is None:
            return
        if getattr(comp, field, None) == value_dbu:
            return
        self._scene.cmd_stack.execute(EditComponent(comp, **{field: value_dbu}))
        item = self._scene.item_for(comp_id)
        if item:
            item.sync_from_model()
        self._props.show_component(comp, self._design)
        self._flash_status(f"{field.replace('_', ' ').title()} → {value_dbu / 1000:.3f} µm")

    @pyqtSlot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        label = f"ZOOM  {zoom * 1000:.2f} px/µm"
        self._sb_zoom.setText(label)
        self._tb_zoom_label.setText(f"{zoom * 1000:.2f} px/µm")

    @pyqtSlot()
    def _refresh_props_for_selection(self) -> None:
        selected = self._scene.selectedItems()
        if len(selected) == 1 and hasattr(selected[0], "component"):
            self._props.show_component(selected[0].component, self._design)

    @pyqtSlot(str)
    def _on_group_selected(self, group_id: str) -> None:
        self._selected_group_id = group_id
        group = self._design.get_group(group_id)
        if group:
            self._props.show_group(group, self._design)

    # ── Mode helpers ──────────────────────────────────────────────────────────

    def _enter_mode(self, mode: PlacementMode, layer: Optional[int] = None) -> None:
        self._scene.set_mode(mode, layer if layer is not None else self._palette.active_layer)
        cursor = Qt.CursorShape.ArrowCursor if mode == PlacementMode.SELECT else Qt.CursorShape.CrossCursor
        self._view.setCursor(cursor)
        if mode == PlacementMode.SELECT:
            self._tb_select.setChecked(True)

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
        stack    = self._scene.cmd_stack
        can_undo = stack.can_undo
        can_redo = stack.can_redo

        self._act_undo.setEnabled(can_undo)
        self._act_redo.setEnabled(can_redo)
        self._tb_undo.setEnabled(can_undo)
        self._tb_redo.setEnabled(can_redo)
        self._act_undo.setText(f"Undo  {stack.undo_description}" if can_undo else "Undo")
        self._act_redo.setText(f"Redo  {stack.redo_description}" if can_redo else "Redo")

    def _select_all(self) -> None:
        for item in self._scene.items():
            item.setSelected(True)

    def _copy(self) -> None:
        self._scene.copy_selection()
        Clipboard.instance().reset_paste_count()

    def _paste(self) -> None:
        self._scene.paste()

    def _duplicate(self) -> None:
        self._scene.duplicate_selection()

    def _delete_selected(self) -> None:
        sel  = self._scene.selectedItems()
        cmds = []
        deleted_ids: set[str] = set()

        # Groups first: delete the group record and all its member components.
        for item in sel:
            if not isinstance(item, GroupItem):
                continue
            group = item.group
            cmds.append(RemoveGroup(group))
            for cid in group.member_ids:
                if cid not in deleted_ids:
                    comp = self._design.get(cid)
                    if comp:
                        cmds.append(RemoveComponent(comp))
                        deleted_ids.add(cid)

        # Loose components not already covered by a group deletion.
        for item in sel:
            if not hasattr(item, "component"):
                continue
            comp = item.component
            if comp.id not in deleted_ids:
                cmds.append(RemoveComponent(comp))
                deleted_ids.add(comp.id)

        if not cmds:
            return

        cmd = cmds[0] if len(cmds) == 1 else BatchCommand(cmds, f"Delete {len(deleted_ids)} item(s)")
        self._scene.cmd_stack.execute(cmd)
        self._flash_status(f"Deleted {len(deleted_ids)} item{'s' if len(deleted_ids) != 1 else ''}")

    @pyqtSlot()
    def _group_selected(self) -> None:
        selected_items = self._scene.selectedItems()

        selected_groups = [
            g for g in (
                self._design.get_group(item.group.id)
                for item in selected_items
                if isinstance(item, GroupItem)
            )
            if g is not None
        ]
        selected_comps = [
            item.component
            for item in selected_items
            if hasattr(item, "component")
        ]

        if selected_groups:
            # Merge mode: requires ≥2 total items (groups + loose components).
            if len(selected_groups) < 2 and not selected_comps:
                QMessageBox.information(
                    self, "Group",
                    "Select two or more groups (or groups + components) to merge.",
                )
                return

            grouped_ids = {cid for g in selected_groups for cid in g.member_ids}
            extra_ids   = [c.id for c in selected_comps if c.id not in grouped_ids]

            name, ok = QInputDialog.getText(
                self, "Merge Groups", "New group name:",
                text=f"group_{len(self._design.groups) + 1}",
            )
            if not ok or not name.strip():
                return

            self._scene.cmd_stack.execute(MergeGroups(selected_groups, extra_ids, name.strip()))
            n = len(selected_groups)
            suffix = (f" + {len(extra_ids)} component{'s' if len(extra_ids) != 1 else ''}"
                      if extra_ids else "")
            self._flash_status(f"Merged {n} group{'s' if n != 1 else ''}{suffix} → '{name.strip()}'")
            return

        # No groups in selection — group bare components.
        if len(selected_comps) < 2:
            QMessageBox.information(
                self, "Group",
                "Select two or more components — or two or more groups — to group.",
            )
            return

        name, ok = QInputDialog.getText(
            self, "Group Name", "Group name:",
            text=f"group_{len(self._design.groups) + 1}",
        )
        if not ok or not name.strip():
            return

        self._scene.cmd_stack.execute(GroupComponents([c.id for c in selected_comps], name.strip()))
        self._flash_status(f"Grouped {len(selected_comps)} components as '{name.strip()}'")

    @pyqtSlot()
    def _ungroup_selected(self) -> None:
        selected_comp_ids = {
            item.component.id
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        }
        target_group_ids = {
            item.group.id
            for item in self._scene.selectedItems()
            if isinstance(item, GroupItem)
        }
        # Also include groups whose members appear in the selection.
        for g in self._design.groups:
            if any(cid in selected_comp_ids for cid in g.member_ids):
                target_group_ids.add(g.id)

        if not target_group_ids:
            QMessageBox.information(self, "Ungroup", "No grouped components selected.")
            return

        for gid in target_group_ids:
            group = self._design.get_group(gid)
            if group:
                self._scene.cmd_stack.execute(UngroupComponents(group))

        self._flash_status(f"Ungrouped {len(target_group_ids)} group(s)")

    @pyqtSlot()
    def _sweep(self) -> None:
        """
        Route to the appropriate sweep dialog based on what is selected:
          1. A parametric cell group  → CellSweepDialog
          2. A user-drawn group       → GroupSweepDialog
          3. A single loose component → SweepDialog
        """
        group = self._resolve_sweep_group()

        if group is not None:
            if getattr(group, "cell_id", None):
                CellSweepDialog(group, self._design, self._scene, self).exec()
            else:
                GroupSweepDialog(group, self._design, self._scene.cmd_stack, self).exec()
            return

        selected = [
            item.component
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        ]
        if len(selected) != 1:
            QMessageBox.information(self, "Sweep", "Select exactly one component or group to sweep.")
            return
        SweepDialog(selected[0], self._design, self._scene.cmd_stack, self).exec()

    def _resolve_sweep_group(self):
        """
        Return the ComponentGroup to sweep, or None.

        Prefers the explicitly group-selected ID; falls back to inferring the
        group from the selected component items (handles the common case of
        clicking into a cell group and pressing Ctrl+W without clicking the
        group border).
        """
        if self._selected_group_id:
            group = self._design.get_group(self._selected_group_id)
            if group:
                return group

        selected_comps = [
            item.component
            for item in self._scene.selectedItems()
            if hasattr(item, "component")
        ]
        if not selected_comps:
            return None

        candidate = self._design.group_of(selected_comps[0].id)
        if candidate is None:
            return None

        # All selected components must belong to the same group.
        all_same = all(
            self._design.group_of(c.id) is not None
            and self._design.group_of(c.id).id == candidate.id
            for c in selected_comps
        )
        return candidate if all_same else None

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
            self._current_file    = path
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

        self._design              = new_design
        self._scene._design       = new_design
        self._scene.cmd_stack     = type(self._scene.cmd_stack)(new_design)
        self._scene.cmd_stack.connect_change(self._scene._on_model_changed)
        self._scene._on_model_changed()

        self._current_file = Path(path)
        self._update_title()
        self._flash_status(f"Opened {Path(path).name}")
        self._view.zoom_fit()

    def _maybe_save_before(self, action: str) -> bool:
        """
        Prompt Save / Discard / Cancel when the design has unsaved changes.
        Returns True if the caller should proceed, False if the user cancelled.
        """
        if not self._design.is_dirty:
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            f"Save changes before you {action}?",
            QMessageBox.StandardButton.Save    |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Save:
            self._save()
            return not self._design.is_dirty   # False if save was itself cancelled
        return reply == QMessageBox.StandardButton.Discard

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
        layers    = {c.layer for c in self._design.components}
        layer_map = {layer: (layer, 0) for layer in layers}
        try:
            summary = export_gds(self._design, path, layer_map)
        except ExportError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
            return
        self._flash_status(f"Exported {summary['shapes']} shapes to {path}")
        ExportResultDialog(summary, self).exec()

    # ── Help dialogs ──────────────────────────────────────────────────────────

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
            "Ctrl+C - Copy | Ctrl+V - Paste | Ctrl+D - Duplicate<br>"
            "Ctrl+A - Select all | Delete - Delete selected<br>"
            "R - Rotate 90° CW | Shift+R - Rotate 90° CCW<br>"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lbl.setContentsMargins(12, 12, 12, 12)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(lbl)
        layout.addWidget(buttons)
        dlg.exec()