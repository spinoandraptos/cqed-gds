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
import copy as _copy

import qtawesome as qta
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSlot
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox,
    QSizePolicy, QToolBar, QVBoxLayout, QWidget,
)

from core.cell_library import CELL_BY_ID, Point, place_cell
from core.clipboard import Clipboard
from core.commands import (
    BatchCommand, EditComponent, GroupComponents, MergeGroups,
    MoveComponent, MoveGroup, RemoveComponent, RemoveGroup, ReplaceCellCmd,
    ReplaceSubgroupCellCmd, UngroupComponents,
)
from core.model import ComponentKind, DesignScene
from core.serialiser import SerialisationError, load, save
from ui.canvas_scene import CanvasScene, GroupItem, PlacementMode
from ui.canvas_view import CanvasView
from ui.export_dialog import ExportDialog
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
        # Re-entrancy guard: prevents the panel's spinbox repopulation (triggered
        # by group_selected after a param edit) from firing cell_param_change_requested
        # a second time for the same action and corrupting the canvas.
        self._param_edit_in_progress: bool = False

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

        self._act_ruler = self._action("Ruler / Measure", "M", self._toggle_ruler)
        self._act_ruler.setCheckable(True)
        self._act_ruler.setChecked(False)

        self._act_undercut = self._action(
            "Show Undercut Ring  (0.8 µm)", "U",
            self._toggle_undercut,
        )
        self._act_undercut.setCheckable(True)
        self._act_undercut.setChecked(False)

        self._populate_menu(menu, [
            self._act_fit, None,
            self._act_zin, self._act_zout, None,
            self._act_ruler, None,
            self._act_undercut,
        ])

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

        self._tb_ruler = self._tb_button(
            "fa5s.ruler", "Measure / Ruler  (M)",
            self._toggle_ruler, color=Colors.ACCENT,
        )
        self._tb_ruler.setCheckable(True)
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
        self._props.undercut_exclusion_changed.connect(self._on_undercut_exclusion_changed)
        self._props.component_hover_requested.connect(self._scene.highlight_component)
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
                self._props.show_component(comp, self._design,
                                           overlay=self._scene._undercut)
                self._sb_layer.setText(f"LAYER  {comp.layer}")
                return
        self._props.clear()

    def _on_scene_changed(self) -> None:
        count = len(self._design.components)
        self._sb_count.setText(f"{count} component{'s' if count != 1 else ''}")
        self._update_title()
        self._update_undo_actions()

    # ── Undercut ring ─────────────────────────────────────────────────────────

    def _toggle_ruler(self) -> None:
        """Toggle ruler/measure mode (shortcut: M)."""
        from ui.canvas_scene import PlacementMode
        if self._scene.mode == PlacementMode.RULER:
            # Already in ruler mode — ESC out and clear.
            self._scene.cancel_placement()
            self._act_ruler.setChecked(False)
            self._tb_ruler.setChecked(False)
            self._flash_status("Ruler cleared")
        else:
            # Enter ruler mode; clear any placement ghost first.
            self._scene.set_mode(PlacementMode.RULER)
            self._act_ruler.setChecked(True)
            self._tb_ruler.setChecked(True)
            self._flash_status("Ruler: click-drag to measure  |  M or ESC to clear")

    def _toggle_undercut(self) -> None:
        """Toggle the undercut ring overlay and keep the menu item in sync."""
        overlay = self._scene._undercut
        overlay.toggle()
        on = overlay.is_enabled
        self._act_undercut.setChecked(on)
        label = f"Show Undercut Ring  ({overlay.offset_um:.2f} µm)"
        self._act_undercut.setText(label)
        self._flash_status(
            f"Undercut ring {'ON' if on else 'OFF'} — {overlay.offset_um:.2f} µm"
        )

    @pyqtSlot(str, object)
    def _on_undercut_exclusion_changed(self, obj_id: str, value) -> None:
        """
        Handle per-object undercut ring toggle or offset change from the
        properties panel.

        obj_id="__offset__" means the offset spinbox changed; value is the
        new offset in µm (float).  Otherwise value is a bool (excluded flag).
        """
        overlay = self._scene._undercut
        if obj_id == "__offset__":
            overlay.set_offset_um(float(value))
            label = f"Show Undercut Ring  ({overlay.offset_um:.2f} µm)"
            self._act_undercut.setText(label)
        else:
            excluded = bool(value)
            overlay.set_excluded(obj_id, excluded)
            # If the user is turning the ring ON for this object, make sure the
            # global overlay is also enabled — otherwise set_excluded alone has
            # no visible effect.
            if not excluded and not overlay.is_enabled:
                overlay.enable(True)
                self._act_undercut.setChecked(True)
                self._act_undercut.setText(
                    f"Show Undercut Ring  ({overlay.offset_um:.2f} µm)"
                )


    @pyqtSlot(str)
    def _on_mode_changed(self, label: str) -> None:
        self._sb_mode.setText(label.split("  —")[0])

    @pyqtSlot(object, int)
    def _on_place_mode_requested(self, kind: ComponentKind, layer: int) -> None:
        self._enter_mode(self._KIND_TO_MODE[kind], layer)

    @pyqtSlot(str, str, str, object)
    def _on_cell_param_change_requested(self, group_id: str, cell_id_encoded: str,
                                        param_key: str, new_value) -> None:
        """
        Re-place a parametric cell in-place with an updated parameter value.

        Handles two cases:

        1. Single-cell group (group.cell_id is set, no _cell_subgroups):
           Delegates to ReplaceCellCmd — original behaviour unchanged.

        2. Merged group (_cell_subgroups present):
           cell_id_encoded = "<real_cell_id>:<sg_index>" (set by the panel).
           Only the components belonging to that sub-group are replaced.
           All other sub-groups' components are left untouched.
           Uses ReplaceSubgroupCellCmd so the operation is fully undo-able.

        After either replace command executes, connected neighbours are nudged
        to stay aligned with the new port positions on the rebuilt anchor.
        """
        # Guard against re-entrant calls: when we re-select the new GroupItem after
        # replacing a cell, show_group() repopulates spinboxes via setValue(), which
        # fires valueChanged → cell_param_change_requested again with the current
        # (already-committed) value.  That second invocation would run another
        # ReplaceCellCmd on the already-updated model, duplicating components.
        if self._param_edit_in_progress:
            return
        self._param_edit_in_progress = True
        try:
            self._do_cell_param_change(group_id, cell_id_encoded, param_key, new_value)
        finally:
            self._param_edit_in_progress = False

    def _do_cell_param_change(self, group_id: str, cell_id_encoded: str,
                               param_key: str, new_value) -> None:
        """Inner body of _on_cell_param_change_requested — never re-entered."""
        group = self._design.get_group(group_id)
        if group is None:
            return

        # ── Decode cell_id — may carry a sub-group index suffix ────────────────
        # Merged-group panels encode cell_id as "<real_cell_id>:<sg_index>".
        # Single-cell groups emit the plain cell_id (no colon suffix).
        sg_index: int | None = None
        cell_id = cell_id_encoded
        if ":" in cell_id_encoded:
            parts = cell_id_encoded.rsplit(":", 1)
            if parts[1].isdigit():
                cell_id  = parts[0]
                sg_index = int(parts[1])

        cdef = CELL_BY_ID.get(cell_id)
        if cdef is None:
            return

        # ── Case 2: merged group with _cell_subgroups ──────────────────────────
        cell_subgroups = getattr(group, "_cell_subgroups", [])
        if sg_index is not None and 0 <= sg_index < len(cell_subgroups):
            sg = cell_subgroups[sg_index]

            # Build updated params for this sub-group only.
            params = dict(cdef.defaults)
            params.update(sg.get("cell_params", {}))
            params[param_key] = new_value

            # Recover the sub-group's cell origin from its first live member
            # using the same dry-run strategy as the single-cell legacy path.
            sg_comp_ids  = list(sg.get("member_ids", []))
            old_sg_comps = [c for cid in sg_comp_ids
                            if (c := self._design.get(cid)) is not None]
            first_comp   = old_sg_comps[0] if old_sg_comps else None

            origin = None
            if first_comp is not None:
                zero = Point(0, 0)
                original_params = dict(cdef.defaults)
                original_params.update(sg.get("cell_params", {}))
                try:
                    dry_result = place_cell(cell_id, zero, params=original_params)
                    dry_anchor = dry_result.components[0]
                    origin     = Point(
                        first_comp.origin.x - dry_anchor.origin.x,
                        first_comp.origin.y - dry_anchor.origin.y,
                    )
                except Exception:
                    pass
            if origin is None:
                if old_sg_comps:
                    origin = Point(
                        min(c.bbox.x_min for c in old_sg_comps),
                        min(c.bbox.y_min for c in old_sg_comps),
                    )
                else:
                    origin = Point(0, 0)

            # Snapshot old subgroup bbox centre BEFORE building the new cell,
            # so we can land the rebuilt+rotated cell in the same place.
            if old_sg_comps:
                sg_x_min = min(c.bbox.x_min for c in old_sg_comps)
                sg_y_min = min(c.bbox.y_min for c in old_sg_comps)
                sg_x_max = max(c.bbox.x_max for c in old_sg_comps)
                sg_y_max = max(c.bbox.y_max for c in old_sg_comps)
                old_sg_bbox_cx = (sg_x_min + sg_x_max) // 2
                old_sg_bbox_cy = (sg_y_min + sg_y_max) // 2
            else:
                old_sg_bbox_cx, old_sg_bbox_cy = origin.x, origin.y

            # Always build at (0,0) — rotation and translation applied below.
            try:
                new_result = place_cell(cell_id, Point(0, 0), params=params)
            except (KeyError, ValueError) as exc:
                QMessageBox.warning(self, "Cell Parameter", str(exc))
                return

            rotation_steps = sg.get("cell_rotation_steps", 0)
            if rotation_steps:
                from core.commands import _rotate_component_in_place
                # Rotate around the new cell's OWN bbox centre (it sits near
                # the origin since we built it at Point(0,0)).  This is the
                # only pivot that produces the correct orientation regardless
                # of how cell dimensions changed — the old world-space pivot
                # (_cell_rotation_cx/cy) is unrelated to the new geometry.
                xs_min = min(c.bbox.x_min for c in new_result.components)
                ys_min = min(c.bbox.y_min for c in new_result.components)
                xs_max = max(c.bbox.x_max for c in new_result.components)
                ys_max = max(c.bbox.y_max for c in new_result.components)
                own_cx = (xs_min + xs_max) // 2
                own_cy = (ys_min + ys_max) // 2
                for comp in new_result.components:
                    _rotate_component_in_place(comp, own_cx, own_cy, rotation_steps)

            # Translate so the rotated cell's bbox centre lands on the old
            # subgroup bbox centre.
            comps = new_result.components
            xs_min = min(c.bbox.x_min for c in comps)
            ys_min = min(c.bbox.y_min for c in comps)
            xs_max = max(c.bbox.x_max for c in comps)
            ys_max = max(c.bbox.y_max for c in comps)
            new_cx = (xs_min + xs_max) // 2
            new_cy = (ys_min + ys_max) // 2
            dx = old_sg_bbox_cx - new_cx
            dy = old_sg_bbox_cy - new_cy
            if dx or dy:
                for comp in comps:
                    comp.move_by(dx, dy)

            # Build the updated _cell_subgroups entry (member_ids filled by cmd).
            updated_sg_entry = {
                "name":                sg.get("name", cdef.name),
                "cell_id":             cell_id,
                "cell_params":         dict(params),
                "cell_rotation_steps": rotation_steps,
                "member_ids":          [],   # ReplaceSubgroupCellCmd.execute fills this
            }

            import copy as _copy
            old_sg_comps_snapshot = [_copy.deepcopy(c) for c in old_sg_comps]

            # Snapshot connections on the old anchor BEFORE removal destroys them
            old_anchor_sg = old_sg_comps[0] if old_sg_comps else None
            conn_snap = self._snapshot_anchor_connections(old_anchor_sg, group=group)

            cmd = ReplaceSubgroupCellCmd(
                group_id            = group_id,
                sg_index            = sg_index,
                old_sg_comp_ids     = sg_comp_ids,
                old_sg_comps        = old_sg_comps_snapshot,
                new_cell_result     = new_result,
                updated_sg_entry    = updated_sg_entry,
                old_sg_entry        = dict(sg),
                old_all_member_ids  = list(group.member_ids),
                cdef_name           = cdef.name,
                param_key           = param_key,
                new_value           = new_value,
            )
            self._scene.cmd_stack.execute(cmd)

            # Nudge connected neighbours to match new port positions and
            # re-establish connections on the new anchor component.
            new_anchor_sg = next(
                (self._design.get(cid)
                 for cid in (updated_sg_entry.get("member_ids") or [])
                 if self._design.get(cid) and self._design.get(cid).ports),
                None,
            )
            new_sg_members = [
                self._design.get(cid)
                for cid in (updated_sg_entry.get("member_ids") or [])
                if self._design.get(cid)
            ]
            move_cmds = self._restore_connections_and_nudge(
                conn_snap, new_anchor_sg,
                excluded_ids=set(group.member_ids),
                new_members=new_sg_members,
            )
            if move_cmds:
                # Record the moves as an additional undo entry
                # (the replace cmd itself is already on the stack)
                batch = BatchCommand(
                    move_cmds,
                    f"Nudge {len(move_cmds)} neighbour(s) after cell param edit",
                )
                self._scene.cmd_stack.push(batch)

            # Re-select the same GroupItem so the panel stays visible.
            gi = self._scene._group_items.get(group_id)
            if gi is not None:
                self._scene.clearSelection()
                gi.setSelected(True)
                self._scene.group_selected.emit(group_id)

            self._flash_status(f"Updated {cdef.name}: {param_key} = {new_value}")
            return

        # ── Case 1: single-cell group (original behaviour, fully preserved) ────
        # Build updated params: defaults → any stored overrides → this change.
        params = dict(cdef.defaults)
        if hasattr(group, "_cell_params"):
            params.update(group._cell_params)
        params[param_key] = new_value

        # Snapshot the CURRENT live bbox centre of the group BEFORE anything is
        # removed.  ReplaceCellCmd uses this to position the rebuilt cell:
        # it places the new cell axis-aligned at (0,0), rotates it around its
        # OWN bbox centre by rotation_steps, then translates so its bbox centre
        # lands exactly on old_bbox_centre.  This is robust regardless of what
        # _cell_origin or rot_cx/rot_cy contain.
        #
        # Read rotation_steps from all available sources in priority order:
        #  1. _cell_subgroups[0]["cell_rotation_steps"] — kept in sync by both
        #     RotateGroup and ReplaceCellCmd; stored on the group data dict so
        #     it survives even if the group object is replaced by get_group().
        #  2. group._cell_rotation_steps — direct attr set by RotateGroup.
        #  3. Geometry detection fallback — compare live bbox dimensions to a
        #     freshly built unrotated reference cell to detect 90°/270° rotation
        #     when both stored counters are unavailable (e.g. after file load).
        cell_subgroups = getattr(group, "_cell_subgroups", [])
        if cell_subgroups and cell_subgroups[0].get("cell_rotation_steps", 0):
            rotation_steps = cell_subgroups[0]["cell_rotation_steps"]
        elif getattr(group, "_cell_rotation_steps", 0):
            rotation_steps = group._cell_rotation_steps
        else:
            # Fallback: detect rotation from live geometry vs unrotated reference.
            rotation_steps = 0
            try:
                ref_params = dict(cdef.defaults)
                if hasattr(group, "_cell_params"):
                    ref_params.update(group._cell_params)
                ref_result = place_cell(cell_id, Point(0, 0), params=ref_params)
                ref_comps  = ref_result.components
                if ref_comps:
                    ref_w = max(c.bbox.x_max for c in ref_comps) - min(c.bbox.x_min for c in ref_comps)
                    ref_h = max(c.bbox.y_max for c in ref_comps) - min(c.bbox.y_min for c in ref_comps)
                    live_comps = [self._design.get(cid) for cid in group.member_ids
                                  if self._design.get(cid)]
                    if live_comps and ref_w != ref_h:
                        live_w = max(c.bbox.x_max for c in live_comps) - min(c.bbox.x_min for c in live_comps)
                        # If live width ≈ ref height the cell has been rotated 90° or 270°.
                        # We can't distinguish 90° from 270° from dimensions alone, so use
                        # _cell_rotation_steps if it's 1 or 3, else default to 1 (90° CCW).
                        if abs(live_w - ref_h) < abs(live_w - ref_w):
                            stored = getattr(group, "_cell_rotation_steps", 0)
                            rotation_steps = stored if stored in (1, 3) else 1
            except Exception:
                pass
        bb_live = group.bbox_from(self._design.components)
        old_bbox_cx = (bb_live.x_min + bb_live.x_max) // 2
        old_bbox_cy = (bb_live.y_min + bb_live.y_max) // 2

        # Always build the new cell at origin (0,0) — ReplaceCellCmd will
        # rotate + translate it into the correct position.
        try:
            new_result = place_cell(cell_id, Point(0, 0), params=params)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Cell Parameter", str(exc))
            return

        # Preserve _cell_origin so MoveGroup keeps working after the replace.
        origin = getattr(group, "_cell_origin", None)

        old_comp_ids   = list(group.member_ids)
        # ✅ Deep-copy here so ReplaceCellCmd.undo() restores clean snapshots,
        # and live objects aren't re-observed by the scene after removal.
        old_comps      = [_copy.deepcopy(c) for c in
                        (self._design.get(cid) for cid in old_comp_ids) if c]
        old_group_name = group.name

        # Snapshot connections on the old anchor BEFORE removal destroys them.
        # The anchor is always the first component in the group that has ports.
        old_anchor = next(
            (self._design.get(cid) for cid in old_comp_ids
             if self._design.get(cid) and self._design.get(cid).ports),
            None,
        )
        conn_snap = self._snapshot_anchor_connections(old_anchor, group=group)

        cmd = ReplaceCellCmd(
            self._design, self._scene, new_result, cdef,
            param_key, cell_id, params,
            group_id, old_group_name, old_comp_ids, old_comps,
            cell_origin=origin,
            rotation_steps=rotation_steps,
            old_bbox_centre=(old_bbox_cx, old_bbox_cy),
        )
        self._scene.cmd_stack.execute(cmd)

        # Re-select the newly created GroupItem so the properties panel stays
        # populated and the user doesn't lose their selection after each edit.
        new_group = cmd._new_cmd._group
        if new_group is not None:
            new_gi = self._scene._group_items.get(new_group.id)
            if new_gi is not None:
                self._scene.clearSelection()
                new_gi.setSelected(True)
                self._scene.group_selected.emit(new_group.id)

        # Nudge connected neighbours to match the new port positions and
        # re-establish the connections on the new anchor component.
        new_anchor = next(
            (self._design.get(cid)
             for cid in (new_group.member_ids if new_group else [])
             if self._design.get(cid) and self._design.get(cid).ports),
            None,
        )
        new_group_member_ids = set(new_group.member_ids) if new_group else set()
        new_members = [
            self._design.get(cid)
            for cid in (new_group.member_ids if new_group else [])
            if self._design.get(cid)
        ]
        move_cmds = self._restore_connections_and_nudge(
            conn_snap, new_anchor,
            excluded_ids=new_group_member_ids,
            new_members=new_members,
        )
        if move_cmds:
            batch = BatchCommand(
                move_cmds,
                f"Nudge {len(move_cmds)} neighbour(s) after cell param edit",
            )
            self._scene.cmd_stack.push(batch)

        self._flash_status(f"Updated {cdef.name}: {param_key} = {new_value}")

    def _snapshot_anchor_connections(self, anchor, group=None) -> list:
        """
        Before a cell is replaced (which destroys all connections to old IDs),
        snapshot every external connection on *anchor* and all other members of
        *group* (if supplied) as plain dicts:

            {
              "src_comp_id":    str,          # which cell member owns this port
              "port_name":      str,          # port name on that member
              "old_abs_x":      int,          # absolute scene X of the port before rebuild
              "old_abs_y":      int,          # absolute scene Y of the port before rebuild
              "nbr_comp_id":    str,          # the other (external) component's ID
              "nbr_port_id":    str,          # the other component's port ID
            }

        When *group* is supplied all members are walked so connections on
        non-anchor members are captured too.  Without it only *anchor* is
        walked (backward-compatible).

        Returns an empty list if anchor is None or has no connections.
        The snapshots survive the replace operation because they are plain dicts
        keyed by neighbour IDs and port names — not live object references.
        """
        if anchor is None:
            return []

        # Collect every component to inspect (anchor + any other group members)
        member_ids: set[str] = {anchor.id}
        if group is not None:
            member_ids.update(group.member_ids)

        snaps = []
        for src_id in member_ids:
            src = self._design.get(src_id)
            if src is None:
                continue
            port_by_id = {p.id: p for p in src.ports}
            for cn in self._design.connections_for(src_id):
                our_port_id = cn.port_a if cn.comp_a == src_id else cn.port_b
                nbr_comp_id = cn.comp_b if cn.comp_a == src_id else cn.comp_a
                nbr_port_id = cn.port_b if cn.comp_a == src_id else cn.port_a

                # Only snapshot external connections (skip intra-group wiring)
                if nbr_comp_id in member_ids:
                    continue

                port = port_by_id.get(our_port_id)
                if port is None:
                    continue
                abs_p = port.abs_pos(src.origin)
                snaps.append({
                    "src_comp_id": src_id,
                    "port_name":   port.name,
                    "old_abs_x":   abs_p.x,
                    "old_abs_y":   abs_p.y,
                    "nbr_comp_id": nbr_comp_id,
                    "nbr_port_id": nbr_port_id,
                })
        return snaps

    def _restore_connections_and_nudge(
        self,
        conn_snap: list,
        new_anchor,
        excluded_ids: set,
        new_members: list | None = None,
    ) -> list:
        """
        After a cell replace:
          1. For each snapshotted connection, find the matching port on the
             corresponding new member by port name.  Port lookup searches
             *new_anchor* first, then all other *new_members* so connections
             on any member of the rebuilt cell are re-established (not just
             the anchor).  Compute how far that port moved (new_abs − old_abs).
          2. Re-establish the Connection record between the new member's port
             and the neighbour's port (so the indicator dots reappear).
          3. BFS-propagate the displacement to all transitively connected
             neighbours that are not inside *excluded_ids* (the rebuilt cell's
             own members).

        Bug fixes vs. old version
        -------------------------
        * Single shared ``visited`` set across ALL snapshots so no neighbour
          is moved twice when two snapshotted ports displaced it by the same
          delta (previously each snap got its own ``visited``, causing
          double-moves equal to 2×dx/dy).
        * Port-name lookup now checks every new member, not only new_anchor,
          so connections that lived on non-anchor cell members survive rebuild.
        * Connections on non-moving ports (dx=dy=0) are re-established even
          though no nudge is needed — previously they were skipped entirely,
          leaving the indicator dot dark.

        Returns a list of MoveComponent commands that were already executed
        (for the caller to record on the undo stack).
        """
        if not conn_snap or new_anchor is None:
            return []

        from collections import deque

        # Build a port-name → (component, port) lookup across ALL new members
        # so we can match connections that lived on non-anchor members.
        all_new_members: list = [new_anchor]
        if new_members:
            for m in new_members:
                if m is not None and m.id != new_anchor.id:
                    all_new_members.append(m)

        def _find_port_by_name(name: str):
            """Return (component, port) for the first new member owning *name*."""
            for member in all_new_members:
                for p in member.ports:
                    if p.name == name:
                        return member, p
            return None, None

        move_cmds = []
        # visited tracks every component ID that has already been moved (or
        # deliberately skipped) so no component is moved more than once.
        # Seeded with the rebuilt cell's own members so we never move them.
        visited: set[str] = set(excluded_ids)

        from collections import deque

        # BFS queue entries: (comp_id, dx, dy)
        #
        # CRITICAL — dx/dy are recomputed at each hop from live port geometry,
        # NOT inherited blindly from the parent entry.
        #
        # Naive inheritance causes the following bug:
        #   - Port A on the rebuilt cell moved by (dx, dy).
        #   - Neighbour N is directly wired to port A → correctly nudged by (dx, dy).
        #   - N also has port B wired to unrelated cell U.
        #   - BFS enqueues U with the same (dx, dy) even though port B never moved.
        #   - U gets dragged along, appearing to "join" the rebuilt cell's group.
        #
        # Instead: when we enqueue a downstream neighbour, compute dx/dy from the
        # displacement of the SPECIFIC port on the just-moved component that connects
        # to that downstream neighbour.  If that port didn't move (displacement = 0),
        # we do NOT enqueue the downstream neighbour for a physical move — though we
        # still re-establish the connection record so indicator dots stay accurate.
        #
        # "old_abs" for intermediate hops is the pre-nudge position of that port,
        # which is its current position BEFORE the move is applied (we read it just
        # before calling move_by).

        # Pass 1 — handle the rebuilt cell's direct connections.
        # These are the only entries where we know old_abs from the snapshot.
        queue: deque[tuple[str, int, int]] = deque()

        for snap in conn_snap:
            src_member, new_port = _find_port_by_name(snap["port_name"])
            if new_port is None:
                continue

            new_abs = new_port.abs_pos(src_member.origin)
            dx = new_abs.x - snap["old_abs_x"]
            dy = new_abs.y - snap["old_abs_y"]

            # Always re-establish the connection (even if port didn't move).
            nbr_comp_id = snap["nbr_comp_id"]
            nbr_port_id = snap["nbr_port_id"]
            nbr = self._design.get(nbr_comp_id)
            if nbr is not None:
                self._design.connect(
                    src_member.id, new_port.id,
                    nbr_comp_id,   nbr_port_id,
                )

            if dx == 0 and dy == 0:
                continue  # port didn't move — connection restored, no nudge

            queue.append((nbr_comp_id, dx, dy))

        # Pass 2 — BFS, propagating only through ports that actually moved.
        while queue:
            cid, dx, dy = queue.popleft()
            if cid in visited:
                continue
            visited.add(cid)

            comp = self._design.get(cid)
            if comp is None:
                continue

            grp = self._design.group_of(cid)
            if grp is not None:
                member_ids_set = set(grp.member_ids)
                if member_ids_set & excluded_ids:
                    continue  # group contains rebuilt cell — skip

                # Snapshot all member port positions BEFORE the move so we can
                # compute per-port displacements for downstream propagation.
                pre_abs: dict[tuple[str, str], tuple[int, int]] = {}
                for mid in grp.member_ids:
                    m = self._design.get(mid)
                    if m is None:
                        continue
                    for p in m.ports:
                        abs_p = p.abs_pos(m.origin)
                        pre_abs[(mid, p.id)] = (abs_p.x, abs_p.y)

                for mid in grp.member_ids:
                    m = self._design.get(mid)
                    if m is None:
                        continue
                    old_orig = m.origin
                    new_orig = Point(old_orig.x + dx, old_orig.y + dy)
                    mc = MoveComponent(mid, old_orig, new_orig)
                    mc.execute(self._design)
                    move_cmds.append(mc)
                    m_item = self._scene.item_for(mid)
                    if m_item:
                        m_item.sync_from_model()

                if hasattr(grp, "_cell_origin") and grp._cell_origin is not None:
                    o = grp._cell_origin
                    grp._cell_origin = Point(o.x + dx, o.y + dy)
                visited.update(member_ids_set)

                # Propagate downstream: for each connection on each group member,
                # only enqueue a neighbour if its connecting port is NOT already
                # co-located with our port after the move (i.e., there is still
                # a physical gap to close).  This naturally stops BFS propagation
                # in loops once the chain has been nudged enough.
                for mid in grp.member_ids:
                    m = self._design.get(mid)
                    if m is None:
                        continue
                    port_map = {p.id: p for p in m.ports}
                    for cn in self._design.connections_for(mid):
                        nxt = cn.comp_b if cn.comp_a == mid else cn.comp_a
                        if nxt in visited:
                            continue
                        our_port_id = cn.port_a if cn.comp_a == mid else cn.port_b
                        their_port_id = cn.port_b if cn.comp_a == mid else cn.port_a
                        port = port_map.get(our_port_id)
                        if port is None:
                            continue
                        our_abs = port.abs_pos(m.origin)
                        # Check if the neighbour's port is already at our port's position
                        nxt_comp = self._design.get(nxt)
                        if nxt_comp is not None:
                            nxt_port = next((p for p in nxt_comp.ports if p.id == their_port_id), None)
                            if nxt_port is not None:
                                their_abs = nxt_port.abs_pos(nxt_comp.origin)
                                ndx = our_abs.x - their_abs.x
                                ndy = our_abs.y - their_abs.y
                                if ndx != 0 or ndy != 0:
                                    queue.append((nxt, ndx, ndy))

            else:
                # Snapshot port positions BEFORE the move.
                pre_abs_comp: dict[str, tuple[int, int]] = {}
                for p in comp.ports:
                    abs_p = p.abs_pos(comp.origin)
                    pre_abs_comp[p.id] = (abs_p.x, abs_p.y)

                old_orig = comp.origin
                new_orig = Point(old_orig.x + dx, old_orig.y + dy)
                mc = MoveComponent(cid, old_orig, new_orig)
                mc.execute(self._design)
                move_cmds.append(mc)
                comp_item = self._scene.item_for(cid)
                if comp_item:
                    comp_item.sync_from_model()

                # Propagate downstream: only enqueue a neighbour if its
                # connecting port is not already co-located with ours after the
                # move.  This stops BFS naturally in loops.
                port_map = {p.id: p for p in comp.ports}
                for cn in self._design.connections_for(cid):
                    nxt = cn.comp_b if cn.comp_a == cid else cn.comp_a
                    if nxt in visited:
                        continue
                    our_port_id  = cn.port_a if cn.comp_a == cid else cn.port_b
                    their_port_id = cn.port_b if cn.comp_a == cid else cn.port_a
                    port = port_map.get(our_port_id)
                    if port is None:
                        continue
                    our_abs = port.abs_pos(comp.origin)
                    nxt_comp = self._design.get(nxt)
                    if nxt_comp is not None:
                        nxt_port = next((p for p in nxt_comp.ports if p.id == their_port_id), None)
                        if nxt_port is not None:
                            their_abs = nxt_port.abs_pos(nxt_comp.origin)
                            ndx = our_abs.x - their_abs.x
                            ndy = our_abs.y - their_abs.y
                            if ndx != 0 or ndy != 0:
                                queue.append((nxt, ndx, ndy))

        # Sync all new member canvas items so port indicators refresh
        for member in all_new_members:
            m_item = self._scene.item_for(member.id)
            if m_item:
                m_item.sync_from_model()

        return move_cmds

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

        # ── Snapshot absolute port positions BEFORE the edit ─────────────────
        # For every connection on the edited component, record the current
        # absolute scene position of OUR port so we can compute how far it
        # moved after the resize.
        #
        # pre_ports[our_port_id] = (old_abs_x, old_abs_y)
        port_map_before = {p.id: p for p in comp.ports}
        pre_ports: dict[str, tuple[int, int]] = {
            pid: (p.abs_pos(comp.origin).x, p.abs_pos(comp.origin).y)
            for pid, p in port_map_before.items()
        }

        # ── Execute the geometry edit ─────────────────────────────────────────
        edit_cmd = EditComponent(comp, **{field: value_dbu})
        edit_cmd.execute(self._design)

        # Sync the edited component's canvas item — this calls rebuild_ports()
        # which recomputes port offsets to match the new bbox.
        item = self._scene.item_for(comp_id)
        if item:
            item.sync_from_model()

        # ── BFS wave propagation: push every transitively connected neighbour ──
        #
        # Wave entries: (src_comp_id, dx, dy)
        #   src_comp_id — the component whose ports we just moved / will move
        #   dx, dy      — the displacement already applied to src_comp
        #
        # visited tracks component IDs that have already been assigned a
        # displacement so we never move anything twice and never loop back to
        # the edited component.
        #
        # For each wave entry we inspect every connection on src_comp.  If the
        # port on src_comp's side moved by (dx, dy), the neighbour must be
        # translated by the same (dx, dy) to keep the ports touching.
        #
        # When the neighbour belongs to a group, ALL group members are shifted
        # together — otherwise the group becomes internally inconsistent and the
        # group bounding box jumps on the next repaint.

        move_cmds: list = []          # Command objects for the undo batch
        visited: set[str] = {comp_id}  # don't move the edited comp

        # Seed: collect (neighbour_comp_id, dx, dy) from the edited comp's ports
        from collections import deque
        queue: deque[tuple[str, int, int]] = deque()

        port_map_after = {p.id: p for p in comp.ports}
        for cn in self._design.connections_for(comp_id):
            our_port_id = cn.port_a if cn.comp_a == comp_id else cn.port_b
            nbr_comp_id = cn.comp_b if cn.comp_a == comp_id else cn.comp_a

            port_after = port_map_after.get(our_port_id)
            if port_after is None:
                continue
            old_x, old_y = pre_ports.get(our_port_id, (0, 0))
            new_abs = port_after.abs_pos(comp.origin)
            dx = new_abs.x - old_x
            dy = new_abs.y - old_y

            if nbr_comp_id not in visited:
                # Always enqueue so the BFS can refresh the indicator even if
                # dx==dy==0 (port didn't move but indicator dot needs updating).
                queue.append((nbr_comp_id, dx, dy))

        while queue:
            nbr_comp_id, dx, dy = queue.popleft()
            if nbr_comp_id in visited:
                continue
            visited.add(nbr_comp_id)

            nbr = self._design.get(nbr_comp_id)
            if nbr is None:
                continue

            # Determine whether this component is part of a group.
            # If so, move ALL group members together so the group stays intact.
            group = self._design.group_of(nbr_comp_id)

            if group is not None:
                # Collect all live group members (the group may contain stale IDs)
                members = [
                    self._design.get(cid)
                    for cid in group.member_ids
                    if self._design.get(cid) is not None
                ]
                # Don't move a group that contains the edited component itself.
                member_ids_set = {m.id for m in members}
                if comp_id in member_ids_set:
                    continue

                # Apply the move to each member (skip zero-delta — no move needed,
                # but still sync the canvas item so connection indicators refresh).
                per_member_cmds = []
                for m in members:
                    m_item = self._scene.item_for(m.id)
                    if dx != 0 or dy != 0:
                        old_orig = m.origin
                        new_orig = Point(old_orig.x + dx, old_orig.y + dy)
                        mc = MoveComponent(m.id, old_orig, new_orig)
                        mc.execute(self._design)
                        per_member_cmds.append(mc)
                    if m_item:
                        m_item.sync_from_model()

                # Keep _cell_origin in sync (MoveGroup does this; replicate it)
                if hasattr(group, "_cell_origin") and group._cell_origin is not None:
                    o = group._cell_origin
                    group._cell_origin = Point(o.x + dx, o.y + dy)

                move_cmds.extend(per_member_cmds)

                # Mark ALL group members visited so they aren't moved again.
                visited.update(member_ids_set)

                # Propagate further: for every connection on every group member,
                # check whether the connected component (outside this group)
                # needs to be pushed by the same (dx, dy).
                for m in members:
                    for cn in self._design.connections_for(m.id):
                        next_comp_id = cn.comp_b if cn.comp_a == m.id else cn.comp_a
                        if next_comp_id not in visited:
                            queue.append((next_comp_id, dx, dy))

            else:
                # Standalone component — move only it (skip zero-delta, but always
                # sync so connection indicator dots refresh correctly).
                nbr_item = self._scene.item_for(nbr_comp_id)
                if dx != 0 or dy != 0:
                    old_orig = nbr.origin
                    new_orig = Point(old_orig.x + dx, old_orig.y + dy)
                    mc = MoveComponent(nbr_comp_id, old_orig, new_orig)
                    mc.execute(self._design)
                    move_cmds.append(mc)
                if nbr_item:
                    nbr_item.sync_from_model()

                # Propagate further: push components connected to this neighbour
                # by the same delta (rigid chain — the whole chain shifts together).
                for cn in self._design.connections_for(nbr_comp_id):
                    next_comp_id = cn.comp_b if cn.comp_a == nbr_comp_id else cn.comp_a
                    if next_comp_id not in visited:
                        queue.append((next_comp_id, dx, dy))

        # ── Push a single undoable batch onto the command stack ───────────────
        # All moves were applied directly above to avoid double-execution.
        # cmd_stack.push() records commands for undo without re-running them.
        all_cmds = [edit_cmd] + move_cmds
        if len(all_cmds) == 1:
            self._scene.cmd_stack.push(edit_cmd)
        else:
            n_shapes = len({
                mc._comp_id if isinstance(mc, MoveComponent) else "?"
                for mc in move_cmds
            })
            batch = BatchCommand(
                all_cmds,
                f"Resize {field} + nudge {n_shapes} connected shape(s)",
            )
            self._scene.cmd_stack.push(batch)

        self._flash_status(
            f"{field.replace('_', ' ').title()} → {value_dbu / 1000:.3f} µm"
            + (f"  · nudged {len(move_cmds)} connected shape(s)" if move_cmds else "")
        )

    @pyqtSlot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        label = f"ZOOM  {zoom * 1000:.2f} px/µm"
        self._sb_zoom.setText(label)
        self._tb_zoom_label.setText(f"{zoom * 1000:.2f} px/µm")

    @pyqtSlot()
    def _refresh_props_for_selection(self) -> None:
        selected = self._scene.selectedItems()
        if len(selected) == 1 and hasattr(selected[0], "component"):
            self._props.show_component(selected[0].component, self._design,
                                       overlay=self._scene._undercut)

    @pyqtSlot(str)
    def _on_group_selected(self, group_id: str) -> None:
        self._selected_group_id = group_id
        group = self._design.get_group(group_id)
        if group:
            self._props.show_group(group, self._design,
                                   overlay=self._scene._undercut)

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
        # If we were in ruler mode, un-check the ruler button too.
        self._act_ruler.setChecked(False)
        self._tb_ruler.setChecked(False)

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
        # Collect IDs of components that already belong to a selected group so
        # we can exclude them below.  Group members can still appear in
        # selectedItems() if they were selected while in group-edit mode and
        # exit_group_edit() did not clear their selection — including them would
        # cause those components to be treated as extra loose items and moved
        # into the wrong group (or lost from their original group).
        _selected_group_member_ids: set[str] = {
            cid
            for item in selected_items
            if isinstance(item, GroupItem)
            for cid in item.group.member_ids
        }
        selected_comps = [
            item.component
            for item in selected_items
            if hasattr(item, "component")
            and item.component.id not in _selected_group_member_ids
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
        ExportDialog(self._design, self, overlay=self._scene._undercut).exec()

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
            "M - Ruler / Measure  (click-drag; M or ESC to clear)<br>"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lbl.setContentsMargins(12, 12, 12, 12)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(lbl)
        layout.addWidget(buttons)
        dlg.exec()