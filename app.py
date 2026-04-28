"""
app.py — Main application window.

Layout:
  ┌──────────────────────────────────────────────────────┐
  │  Toolbar (Select | Wire | Pan | Zoom fit | Export)   │
  ├───────────────┬──────────────────┬────────────────────┤
  │  Component    │                  │  Properties        │
  │  palette      │   GDS Canvas     │  panel             │
  │               │                  │                    │
  │  Layer        │                  │  Connections       │
  │  toggles      │                  │                    │
  └───────────────┴──────────────────┴────────────────────┘
  │  Status bar                                           │
  └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations
import os
from datetime import datetime

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QToolBar, QLabel, QStatusBar, QScrollArea, QPushButton,
    QGroupBox, QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox,
    QCheckBox, QFileDialog, QMessageBox, QFrame, QSizePolicy,
    QSpinBox, QApplication,
)
from PyQt6.QtCore import Qt, QMimeData, QPointF, pyqtSignal, QSize
from PyQt6.QtGui import (
    QDrag, QAction, QColor, QPalette, QFont, QIcon,
    QPixmap, QPainter, QBrush,
)

from config import Config
from component_model import (
    ComponentInstance, COMPONENT_TYPES, LAYER_COLORS, LAYER_NAMES,
    export_to_gds, MergedInstance, merge_instances,
    UNDERCUT_RING_LAYER, UNDERCUT_RING_THICKNESS,
    save_workspace, load_workspace,
    export_gds_script,
)
from canvas import GDSScene, GDSView, um_to_px, px_to_um, snap, SNAP_UM


# ── Palette card ──────────────────────────────────────────────────────────────

class PaletteCard(QFrame):
    """Draggable component card in the left palette."""

    def __init__(self, type_id: str, parent=None):
        super().__init__(parent)
        ctype = COMPONENT_TYPES[type_id]
        self.type_id = type_id

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(ctype.description)
        self.setFixedHeight(54)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(8)

        # Colour swatch
        swatch = QLabel()
        swatch.setFixedSize(14, 14)
        color = list(LAYER_COLORS.values())[list(COMPONENT_TYPES.keys()).index(type_id) % len(LAYER_COLORS)]
        swatch.setStyleSheet(
            f"background:{color}; border-radius:3px;"
        )
        lay.addWidget(swatch)

        info = QVBoxLayout()
        info.setSpacing(1)
        name_lbl = QLabel(ctype.name)
        name_lbl.setStyleSheet("font-weight:500; font-size:12px;")
        desc_lbl = QLabel(ctype.description)
        desc_lbl.setStyleSheet("color:#888; font-size:10px;")
        desc_lbl.setWordWrap(True)
        info.addWidget(name_lbl)
        info.addWidget(desc_lbl)
        lay.addLayout(info)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setText(self.type_id)
            drag.setMimeData(mime)

            # Build a tiny pixmap for the drag ghost
            pm = QPixmap(80, 30)
            pm.fill(QColor(0, 0, 0, 0))
            painter = QPainter(pm)
            painter.setPen(QColor("#7F77DD"))
            painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
                             COMPONENT_TYPES[self.type_id].name)
            painter.end()
            drag.setPixmap(pm)
            drag.exec(Qt.DropAction.CopyAction)


# ── Left panel ────────────────────────────────────────────────────────────────

class LeftPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(195)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # Components section
        comp_label = QLabel("Components")
        comp_label.setStyleSheet(
            "font-size:10px; font-weight:500; color:#888; letter-spacing:0.05em;"
        )
        lay.addWidget(comp_label)

        for type_id in COMPONENT_TYPES:
            lay.addWidget(PaletteCard(type_id))

        lay.addSpacing(10)

        # Layer toggles
        layer_label = QLabel("Layers")
        layer_label.setStyleSheet(
            "font-size:10px; font-weight:500; color:#888; letter-spacing:0.05em;"
        )
        lay.addWidget(layer_label)

        self.layer_checks: dict[int, QCheckBox] = {}
        for layer, name in LAYER_NAMES.items():
            color = LAYER_COLORS.get(layer, "#888888")
            row   = QWidget()
            rl    = QHBoxLayout(row)
            rl.setContentsMargins(2, 0, 2, 0)
            rl.setSpacing(6)

            swatch = QLabel()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background:{color}; border-radius:2px;")
            rl.addWidget(swatch)

            cb = QCheckBox(f"{name}  L{layer}")
            cb.setChecked(True)
            cb.setStyleSheet("font-size:11px;")
            self.layer_checks[layer] = cb
            rl.addWidget(cb)
            lay.addWidget(row)

        lay.addStretch()


# ── Properties panel ──────────────────────────────────────────────────────────

class PropertiesPanel(QWidget):
    param_changed = pyqtSignal(int, str, object)   # inst_id, key, value

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(195)
        self._inst: ComponentInstance | None = None
        self._editors: dict[str, QWidget] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # Title
        self._title = QLabel("No selection")
        self._title.setStyleSheet("font-size:12px; font-weight:500;")
        lay.addWidget(self._title)

        # Position
        pos_box = QGroupBox("Position (µm)")
        pos_box.setStyleSheet("QGroupBox{font-size:11px;}")
        pf = QFormLayout(pos_box)
        pf.setSpacing(4)
        self._x_spin = QDoubleSpinBox()
        self._y_spin = QDoubleSpinBox()
        for sp in (self._x_spin, self._y_spin):
            sp.setRange(-10000, 10000)
            sp.setSingleStep(0.05)
            sp.setDecimals(3)
            sp.setStyleSheet("font-size:11px;")
        self._x_spin.valueChanged.connect(self._on_x_changed)
        self._y_spin.valueChanged.connect(self._on_y_changed)
        pf.addRow("x", self._x_spin)
        pf.addRow("y", self._y_spin)
        lay.addWidget(pos_box)

        # Parameters
        self._params_box = QGroupBox("Parameters")
        self._params_box.setStyleSheet("QGroupBox{font-size:11px;}")
        self._params_layout = QFormLayout(self._params_box)
        self._params_layout.setSpacing(4)
        lay.addWidget(self._params_box)

        # Connections
        self._conn_box = QGroupBox("Connections")
        self._conn_box.setStyleSheet("QGroupBox{font-size:11px;}")
        self._conn_layout = QVBoxLayout(self._conn_box)
        lay.addWidget(self._conn_box)

        lay.addStretch()

    def load(self, inst: ComponentInstance | None):
        self._inst = inst
        self._clear_params()
        self._clear_conns()

        if inst is None:
            self._title.setText("No selection")
            self._x_spin.setValue(0)
            self._y_spin.setValue(0)
            return

        self._title.setText(inst.label)
        self._x_spin.blockSignals(True)
        self._y_spin.blockSignals(True)
        self._x_spin.setValue(inst.x)
        self._y_spin.setValue(inst.y)
        self._x_spin.blockSignals(False)
        self._y_spin.blockSignals(False)

        # Parameter editors — merged groups show read-only source info
        from component_model import MergedInstance
        if isinstance(inst, MergedInstance):
            src_lbl = QLabel(inst.params.get("source_labels", ""))
            src_lbl.setStyleSheet("font-size:10px; color:#888; font-style:italic;")
            src_lbl.setWordWrap(True)
            self._params_layout.addRow("sources", src_lbl)
            poly_lbl = QLabel(str(len(inst._poly_data)))
            poly_lbl.setStyleSheet("font-size:10px; color:#888;")
            self._params_layout.addRow("polygons", poly_lbl)
        else:
            # Internal params that should not appear in the properties panel
            _HIDDEN_PARAMS = {"ring_polys", "_offset_x", "_offset_y",
                              "source_inst_id", "_source_instances"}
            for key, val in inst.params.items():
                if key in _HIDDEN_PARAMS:
                    continue
                self._add_param_editor(inst, key, val)

        # Connection display
        if inst.connections:
            for port_name, (other_id, other_port) in inst.connections.items():
                lbl = QLabel(f"{port_name} → #{other_id}.{other_port}")
                lbl.setStyleSheet("font-size:10px; color:#5DCAA5;")
                self._conn_layout.addWidget(lbl)
        else:
            lbl = QLabel("No connections")
            lbl.setStyleSheet("font-size:10px; color:#555;")
            self._conn_layout.addWidget(lbl)

    def _clear_params(self):
        while self._params_layout.rowCount():
            self._params_layout.removeRow(0)
        self._editors.clear()

    def _clear_conns(self):
        while self._conn_layout.count():
            item = self._conn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_param_editor(self, inst: ComponentInstance, key: str, val):
        if isinstance(val, bool):
            w = QCheckBox()
            w.setChecked(val)
            w.stateChanged.connect(
                lambda state, k=key: self._emit(k, bool(state))
            )
        elif isinstance(val, float):
            w = QDoubleSpinBox()
            w.setRange(-10000, 10000)
            w.setDecimals(3)
            w.setSingleStep(0.1)
            w.setValue(val)
            # Use editingFinished so rebuilds only fire when the user commits
            # a value (Enter or focus-loss), not on every intermediate keystroke.
            w.editingFinished.connect(lambda ww=w, k=key: self._emit(k, ww.value()))
        elif isinstance(val, int):
            w = QSpinBox()
            w.setRange(0, 99)
            w.setValue(val)
            w.valueChanged.connect(lambda v, k=key: self._emit(k, v))
        elif isinstance(val, str) and key in ("cap_style", "undercut_style",
                                               "direction", "narrow_end",
                                               "entry_dir", "turn_dir"):
            w = QComboBox()
            options = {
                "cap_style":      ["top", "side"],
                "undercut_style": ["right", "top"],
                "direction":      ["+x", "-x", "+y", "-y"],
                "entry_dir":      ["+x", "-x", "+y", "-y"],
                "turn_dir":       ["l", "r"],
                "narrow_end":     ["start", "end"],
            }.get(key, [val])
            w.addItems(options)
            w.setCurrentText(val)
            w.currentTextChanged.connect(lambda v, k=key: self._emit(k, v))
        else:
            w = QLineEdit(str(val))
            w.editingFinished.connect(
                lambda k=key, ww=w: self._emit(k, ww.text())
            )

        w.setStyleSheet("font-size:11px;")
        self._params_layout.addRow(key, w)
        self._editors[key] = w

    def _emit(self, key: str, val):
        if self._inst:
            self._inst.params[key] = val
            self.param_changed.emit(self._inst.inst_id, key, val)

    def _on_x_changed(self, v: float):
        if self._inst:
            self._inst.x = v
            self.param_changed.emit(self._inst.inst_id, "_x", v)

    def _on_y_changed(self, v: float):
        if self._inst:
            self._inst.y = v
            self.param_changed.emit(self._inst.inst_id, "_y", v)


# ── Drop-enabled canvas wrapper ───────────────────────────────────────────────

class DropCanvas(GDSView):
    component_dropped = pyqtSignal(str, float, float)  # type_id, x_um, y_um

    def __init__(self, scene: GDSScene):
        super().__init__(scene)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        type_id = event.mimeData().text()
        sp = self.mapToScene(event.position().toPoint())
        um_x = snap(px_to_um(sp.x()),  self.scene().snap_um)
        um_y = snap(-px_to_um(sp.y()), self.scene().snap_um)
        self.component_dropped.emit(type_id, um_x, um_y)
        event.acceptProposedAction()


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    # Default autosave location — same directory as this script
    _AUTOSAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "workspace.json")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GDS Layout Editor")
        self.resize(1280, 780)

        self.cfg = Config()
        self._instances: dict[int, ComponentInstance] = {}
        self._selected_id: int | None = None
        self._clipboard: ComponentInstance | None = None   # copy/paste buffer

        self._build_ui()
        self._connect_signals()
        self._apply_dark_style()

        # Auto-load last workspace if it exists
        if os.path.isfile(self._AUTOSAVE_PATH):
            self._load_workspace_from(self._AUTOSAVE_PATH, silent=True)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Body
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # Left panel
        self._left = LeftPanel()
        body.addWidget(self._left)

        # Separator
        sep_l = QFrame()
        sep_l.setFrameShape(QFrame.Shape.VLine)
        sep_l.setStyleSheet("color:#2a2a2a;")
        body.addWidget(sep_l)

        # Canvas — must be created before toolbar (toolbar connects to view)
        self.scene = GDSScene(self.cfg)
        self.view  = DropCanvas(self.scene)
        body.addWidget(self.view, stretch=1)

        # Toolbar (built after self.view exists)
        self._build_toolbar()

        # Separator
        sep_r = QFrame()
        sep_r.setFrameShape(QFrame.Shape.VLine)
        sep_r.setStyleSheet("color:#2a2a2a;")
        body.addWidget(sep_r)

        # Properties panel
        self._props = PropertiesPanel()
        body.addWidget(self._props)

        root.addLayout(body, stretch=1)

        # Status bar
        self._status = QStatusBar()
        self._status.setStyleSheet("font-size:11px; color:#888;")
        self._coord_lbl = QLabel("x: —  y: —")
        self._coord_lbl.setStyleSheet(
            "font-family: monospace; font-size:11px; color:#555; padding-right:12px;"
        )
        self._status.addPermanentWidget(self._coord_lbl)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready — drag a component onto the canvas")

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setStyleSheet(
            "QToolBar{border:none; padding:2px 6px; spacing:4px;}"
            "QToolButton{padding:4px 10px; border-radius:4px; font-size:12px;}"
            "QToolButton:checked{background:#1c3a5a; color:#4fc3f7;}"
            "QToolButton:hover{background:#1e1e1e;}"
        )
        self.addToolBar(tb)

        self._act_select = QAction("Select", self, checkable=True, checked=True)
        self._act_wire   = QAction("Wire", self, checkable=True)
        self._act_pan    = QAction("Pan", self, checkable=True)
        self._act_erase  = QAction("Erase Ring  [X]", self, checkable=True)
        self._act_select.triggered.connect(lambda: self._set_tool("select"))
        self._act_wire.triggered.connect(lambda:   self._set_tool("wire"))
        self._act_pan.triggered.connect(lambda:    self._set_tool("pan"))
        self._act_erase.triggered.connect(lambda:  self._set_tool("erase"))

        tb.addAction(self._act_select)
        tb.addAction(self._act_wire)
        tb.addAction(self._act_pan)
        tb.addAction(self._act_erase)
        tb.addSeparator()

        act_fit   = QAction("Zoom fit", self)
        act_reset = QAction("Zoom 1:1", self)
        act_fit.triggered.connect(self.view.zoom_fit)
        act_reset.triggered.connect(self.view.zoom_reset)
        tb.addAction(act_fit)
        tb.addAction(act_reset)
        tb.addSeparator()

        act_delete = QAction("Delete sel.", self)
        act_delete.triggered.connect(self._delete_selected)
        act_delete.setShortcut("Delete")
        self.addAction(act_delete)
        tb.addAction(act_delete)

        act_copy = QAction(self)
        act_copy.setShortcut("Ctrl+C")
        act_copy.triggered.connect(self._copy_selected)
        self.addAction(act_copy)

        act_paste = QAction(self)
        act_paste.setShortcut("Ctrl+V")
        act_paste.triggered.connect(self._paste)
        self.addAction(act_paste)

        act_merge_shortcut = QAction(self)
        act_merge_shortcut.setShortcut("Ctrl+M")
        act_merge_shortcut.triggered.connect(self._merge_selected)
        self.addAction(act_merge_shortcut)

        act_undercut_shortcut = QAction(self)
        act_undercut_shortcut.setShortcut("U")
        act_undercut_shortcut.triggered.connect(self._add_undercut_ring)
        self.addAction(act_undercut_shortcut)

        act_erase_shortcut = QAction(self)
        act_erase_shortcut.setShortcut("X")
        act_erase_shortcut.triggered.connect(lambda: self._set_tool("erase"))
        self.addAction(act_erase_shortcut)

        act_copy  = QAction("Copy  [Ctrl+C]", self)
        act_paste = QAction("Paste  [Ctrl+V]", self)
        act_copy.triggered.connect(self._copy_selected)
        act_paste.triggered.connect(self._paste)
        tb.addAction(act_copy)
        tb.addAction(act_paste)
        tb.addSeparator()

        act_merge = QAction("Merge  [M]", self)
        act_merge.triggered.connect(self._merge_selected)
        tb.addAction(act_merge)

        act_unmerge = QAction("Unmerge", self)
        act_unmerge.triggered.connect(self._unmerge_selected)
        act_unmerge.setShortcut("Ctrl+Shift+M")
        tb.addAction(act_unmerge)
        self.addAction(act_unmerge)
        tb.addSeparator()

        act_undercut = QAction("Undercut Ring  [U]", self)
        act_undercut.triggered.connect(self._add_undercut_ring)
        tb.addAction(act_undercut)
        tb.addSeparator()

        act_rot_cw  = QAction("Rotate CW  [R]", self)
        act_rot_ccw = QAction("Rotate CCW  [E]", self)
        act_rot_cw.triggered.connect(self._rotate_selected_cw)
        act_rot_ccw.triggered.connect(self._rotate_selected_ccw)
        tb.addAction(act_rot_cw)
        tb.addAction(act_rot_ccw)
        tb.addSeparator()

        act_export = QAction("Export GDS…", self)
        act_export.triggered.connect(self._export_gds)
        tb.addAction(act_export)

        act_export_plot = QAction("Export GDS script…", self)
        act_export_plot.triggered.connect(self._export_plot_script)
        tb.addAction(act_export_plot)

        tb.addSeparator()

        act_save_ws = QAction("Save workspace  [Ctrl+S]", self)
        act_save_ws.triggered.connect(self._save_workspace)
        tb.addAction(act_save_ws)

        act_load_ws = QAction("Load workspace…", self)
        act_load_ws.triggered.connect(self._load_workspace_dialog)
        tb.addAction(act_load_ws)

        act_save_shortcut = QAction(self)
        act_save_shortcut.setShortcut("Ctrl+S")
        act_save_shortcut.triggered.connect(self._save_workspace)
        self.addAction(act_save_shortcut)

        tb.addSeparator()
        snap_label = QLabel("  Snap (µm) ")
        snap_label.setStyleSheet("font-size:11px; color:#888;")
        tb.addWidget(snap_label)
        self._snap_spin = QDoubleSpinBox()
        self._snap_spin.setRange(0.001, 10.0)
        self._snap_spin.setDecimals(3)
        self._snap_spin.setSingleStep(0.05)
        self._snap_spin.setValue(SNAP_UM)
        self._snap_spin.setFixedWidth(72)
        self._snap_spin.setStyleSheet(
            "font-size:11px; background:#1c1c1c; border:0.5px solid #2e2e2e;"
            " border-radius:3px; color:#ccc; padding:2px 4px;"
        )
        self._snap_spin.setToolTip("Snap grid resolution in µm")
        tb.addWidget(self._snap_spin)

    def _connect_signals(self):
        self.view.component_dropped.connect(self._on_drop)
        self.view.coord_changed.connect(self._on_coord)
        self.scene.component_moved.connect(self._on_component_moved)
        self.scene.selection_changed_signal.connect(self._on_selection_changed)
        self.scene.wire_connected.connect(self._on_wire_connected)
        self.scene.status_message.connect(self._status.showMessage)
        self.scene.merge_requested.connect(self._on_merge_requested)
        self.scene.undercut_confirmed.connect(self._on_undercut_confirmed)
        self.scene.erase_applied.connect(self._on_erase_applied)
        self._props.param_changed.connect(self._on_param_changed)

        for layer, cb in self._left.layer_checks.items():
            cb.toggled.connect(
                lambda checked, l=layer: self.scene.set_layer_visible(l, checked)
            )

        self._snap_spin.valueChanged.connect(self._on_snap_changed)

    def _on_snap_changed(self, value: float):
        self.scene.snap_um = value
        self._status.showMessage(f"Snap grid set to {value:.3f} µm")

    # ── Tool switching ────────────────────────────────────────────────────────

    def _set_tool(self, tool: str):
        self._act_select.setChecked(tool == "select")
        self._act_wire.setChecked(tool == "wire")
        self._act_pan.setChecked(tool == "pan")
        self._act_erase.setChecked(tool == "erase")
        self.scene.set_wire_mode(tool == "wire")
        self.view.set_pan_mode(tool == "pan")
        self.view.set_erase_mode(tool == "erase")
        for item in self.scene.all_instances():
            pass  # items remain movable only in select mode
        if tool == "select":
            self._status.showMessage("Select tool — click to select, drag to move")
        elif tool == "wire":
            self._status.showMessage("Wire tool — click a port, then click destination port")
        elif tool == "pan":
            self._status.showMessage("Pan tool — drag to pan, scroll to zoom")
        elif tool == "erase":
            self._status.showMessage(
                "Erase Ring tool — drag a rectangle to delete undercut ring geometry inside it"
            )

    # ── Drop ─────────────────────────────────────────────────────────────────

    def _on_drop(self, type_id: str, x: float, y: float):
        inst = ComponentInstance(type_id, x, y)
        self._instances[inst.inst_id] = inst
        self.scene.add_component(inst)
        self._status.showMessage(f"Placed {inst.label} at ({x:.2f}, {y:.2f}) µm")

    # ── Coord display ─────────────────────────────────────────────────────────

    def _on_coord(self, x: float, y: float):
        self._coord_lbl.setText(f"x: {x:.2f}µm  y: {y:.2f}µm")

    # ── Selection ────────────────────────────────────────────────────────────

    def _on_selection_changed(self, inst_id: int):
        self._selected_id = inst_id
        inst = self._instances.get(inst_id)
        self._props.load(inst)

    def _on_component_moved(self, inst_id: int):
        inst = self._instances.get(inst_id)
        if inst and inst_id == self._selected_id:
            self._props._x_spin.blockSignals(True)
            self._props._y_spin.blockSignals(True)
            self._props._x_spin.setValue(inst.x)
            self._props._y_spin.setValue(inst.y)
            self._props._x_spin.blockSignals(False)
            self._props._y_spin.blockSignals(False)

        # Move any undercut rings that are linked to this component.
        # The ring stores the offset between its own anchor and the source anchor
        # at creation time; we just keep that delta fixed.
        if inst is not None:
            self._sync_linked_rings(inst_id, inst.x, inst.y)

    def _sync_linked_rings(self, source_id: int, new_x: float, new_y: float):
        """Reposition all undercut rings whose source_inst_id == source_id."""
        from canvas import um_to_px, ComponentItem
        for ring_id, ring in self._instances.items():
            if ring.type_id != "undercut_ring":
                continue
            if not ring.params.get("linked", True):
                continue
            if ring.params.get("source_inst_id", -1) != source_id:
                continue
            # Retrieve the stored offset (set once at ring-creation time)
            dx = ring.params.get("_offset_x", 0.0)
            dy = ring.params.get("_offset_y", 0.0)
            ring.x = new_x + dx
            ring.y = new_y + dy
            item = self.scene._component_items.get(ring_id)
            if item:
                item.setPos(um_to_px(ring.x), -um_to_px(ring.y))
                item._rebuild()

    def _on_wire_connected(self, id1: int, p1: str, id2: int, p2: str):
        self._status.showMessage(
            f"Connected #{id1}.{p1} → #{id2}.{p2}"
        )
        # Refresh properties if one of these is selected
        if self._selected_id in (id1, id2):
            self._props.load(self._instances.get(self._selected_id))

    # ── Params ────────────────────────────────────────────────────────────────

    def _on_param_changed(self, inst_id: int, key: str, val):
        from canvas import um_to_px
        inst = self._instances.get(inst_id)
        if inst is None:
            return
        if key == "_x":
            inst.x = val
        elif key == "_y":
            inst.y = val
        # When an undercut ring is re-linked, immediately snap it back to source
        elif key == "linked" and val is True and inst.type_id == "undercut_ring":
            src_id = inst.params.get("source_inst_id", -1)
            src = self._instances.get(src_id)
            if src is not None:
                dx = inst.params.get("_offset_x", 0.0)
                dy = inst.params.get("_offset_y", 0.0)
                inst.x = src.x + dx
                inst.y = src.y + dy
                item = self.scene._component_items.get(inst_id)
                if item:
                    item.setPos(um_to_px(inst.x), -um_to_px(inst.y))
                    item._rebuild()
                # Update position spinboxes in properties panel
                self._props._x_spin.blockSignals(True)
                self._props._y_spin.blockSignals(True)
                self._props._x_spin.setValue(inst.x)
                self._props._y_spin.setValue(inst.y)
                self._props._x_spin.blockSignals(False)
                self._props._y_spin.blockSignals(False)
        # Sync scene item position & rebuild polygons
        item = self.scene._component_items.get(inst_id)
        if item:
            item.setPos(um_to_px(inst.x), -um_to_px(inst.y))
            item._rebuild()

    # ── Delete ────────────────────────────────────────────────────────────────

    def _delete_selected(self):
        from canvas import ComponentItem
        to_delete = []
        for item in self.scene.selectedItems():
            if isinstance(item, ComponentItem):
                to_delete.append(item.inst.inst_id)

        for iid in to_delete:
            # Also collect any undercut rings linked to this component
            linked_rings = [
                rid for rid, ring in self._instances.items()
                if ring.type_id == "undercut_ring"
                and ring.params.get("linked", True)
                and ring.params.get("source_inst_id", -1) == iid
            ]
            self._instances.pop(iid, None)
            self.scene.remove_component(iid)
            if self._selected_id == iid:
                self._selected_id = None
                self._props.load(None)

            for rid in linked_rings:
                self._instances.pop(rid, None)
                self.scene.remove_component(rid)
                if self._selected_id == rid:
                    self._selected_id = None
                    self._props.load(None)

    # ── Copy / Paste ──────────────────────────────────────────────────────────

    def _copy_selected(self):
        """Copy the currently selected component into the internal clipboard."""
        if self._selected_id is None:
            self._status.showMessage("Nothing selected to copy")
            return
        inst = self._instances.get(self._selected_id)
        if inst is None:
            return
        self._clipboard = inst          # store reference; clone is made on paste
        self._status.showMessage(f"Copied {inst.label}")

    def _paste(self):
        """Paste a clone of the clipboard component, offset by +2 µm in x and -2 µm in y."""
        if self._clipboard is None:
            self._status.showMessage("Clipboard is empty — copy a component first")
            return
        new_inst = self._clipboard.clone(offset_x=2.0, offset_y=-2.0)
        self._instances[new_inst.inst_id] = new_inst
        self.scene.add_component(new_inst)
        # Select the newly pasted component
        self.scene.select_component(new_inst.inst_id)
        self._clipboard = new_inst      # subsequent pastes cascade by +2 µm each time
        self._status.showMessage(
            f"Pasted {new_inst.label} at ({new_inst.x:.2f}, {new_inst.y:.2f}) µm"
        )

    # ── Merge ─────────────────────────────────────────────────────────────────

    def _merge_selected(self):
        """Trigger a merge of all currently selected components."""
        from canvas import ComponentItem
        ids = [
            item.inst.inst_id
            for item in self.scene.selectedItems()
            if isinstance(item, ComponentItem)
        ]
        if len(ids) >= 2:
            self._on_merge_requested(ids)
        else:
            self._status.showMessage(
                "Select 2 or more components to merge  [M / Ctrl+M]"
            )

    def _on_merge_requested(self, inst_ids: list):
        """
        Collect the instances by id, call merge_instances(), replace the
        originals on the canvas with the single merged result, and select it.
        """
        instances = [self._instances[iid] for iid in inst_ids
                     if iid in self._instances]
        if len(instances) < 2:
            self._status.showMessage("Need at least 2 components to merge")
            return

        try:
            merged = merge_instances(instances, self.cfg)
        except Exception as e:
            self._status.showMessage(f"Merge failed: {e}")
            return

        # Remove originals
        for iid in inst_ids:
            self._instances.pop(iid, None)
            self.scene.remove_component(iid)

        # Add merged result
        self._instances[merged.inst_id] = merged
        self.scene.add_component(merged)
        self.scene.select_component(merged.inst_id)
        self._selected_id = merged.inst_id
        self._props.load(merged)

        self._status.showMessage(
            f"Merged {len(instances)} components → {merged.label}"
        )

    def _unmerge_selected(self):
        """
        Split the selected MergedInstance back into its original components.
        Only works if the group was created after the unmerge feature was added.
        """
        from component_model import MergedInstance, unmerge_instance

        if self._selected_id is None:
            self._status.showMessage("Select a merged group to unmerge")
            return

        inst = self._instances.get(self._selected_id)
        if not isinstance(inst, MergedInstance):
            self._status.showMessage("Selected component is not a merged group")
            return

        try:
            restored = unmerge_instance(inst)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot unmerge", str(e))
            return

        # Remove the merged group
        self._instances.pop(self._selected_id, None)
        self.scene.remove_component(self._selected_id)
        self._selected_id = None
        self._props.load(None)

        # Add each restored instance back onto the canvas
        for r in restored:
            self._instances[r.inst_id] = r
            self.scene.add_component(r)

        self._status.showMessage(
            f"Unmerged → {len(restored)} component{'s' if len(restored) != 1 else ''} restored"
        )

    def _add_undercut_ring(self):
        """
        Begin the interactive undercut-ring editor for the selected component.
        Must have exactly one component selected.
        """
        if self._selected_id is None:
            self._status.showMessage(
                "Select a component first, then click Undercut Ring  [U]"
            )
            return
        inst = self._instances.get(self._selected_id)
        if inst is None:
            return
        ok = self.scene.begin_undercut_edit(self._selected_id)
        if ok:
            self._status.showMessage(
                f"Editing undercut ring for {inst.label} — "
                "click segments to toggle, then Confirm or Cancel"
            )

    def _on_undercut_confirmed(self, source_id: int, sides: dict):
        """
        Called when the user confirms the ring.  Creates an undercut_ring
        ComponentInstance whose bbox matches the source component's bounding box.
        """
        from component_model import render_instance

        source = self._instances.get(source_id)
        if source is None:
            return

        # Compute µm bounding box from the source's rendered polygons.
        # Exclude layer 11 (narrow-end slivers) from bbox calculation.
        polys = render_instance(source, self.cfg)
        if not polys:
            self._status.showMessage("Cannot compute bounding box — no geometry")
            return

        from component_model import compute_undercut_ring_polys, UNDERCUT_RING_LAYER, UNDERCUT_RING_THICKNESS

        all_xs = [x for layer, pts in polys if layer != 11 for x, _ in pts]
        all_ys = [y for layer, pts in polys if layer != 11 for _, y in pts]
        if not all_xs:
            all_xs = [x for _, pts in polys for x, _ in pts]
            all_ys = [y for _, pts in polys for _, y in pts]
        cx = (min(all_xs) + max(all_xs)) / 2
        cy = (min(all_ys) + max(all_ys)) / 2

        # Compute the geometry-hugging offset ring as baked local-offset polys
        ring_polys = compute_undercut_ring_polys(
            polys, sides, UNDERCUT_RING_THICKNESS, UNDERCUT_RING_LAYER, cx, cy
        )

        ring = ComponentInstance("undercut_ring", x=cx, y=cy)
        ring.params.update({
            "ring_polys":     ring_polys,   # pre-baked LOCAL-offset shell polygons
            "side_top":       sides.get("top",    True),
            "side_bottom":    sides.get("bottom", True),
            "side_left":      sides.get("left",   True),
            "side_right":     sides.get("right",  True),
            "linked":         True,         # sticky — follows its source by default
            "source_inst_id": source_id,    # id of the parent component
            # Offset from source anchor → ring anchor (fixed at creation time)
            "_offset_x":      cx - source.x,
            "_offset_y":      cy - source.y,
        })

        self._instances[ring.inst_id] = ring
        self.scene.add_component(ring)
        self.scene.select_component(ring.inst_id)
        self._selected_id = ring.inst_id
        self._props.load(ring)

        active_sides = [s for s, v in sides.items() if v]
        self._status.showMessage(
            f"Undercut ring added — sides: {', '.join(active_sides) or 'none'}"
        )

    def _on_erase_applied(self, inst_id: int):
        """Refresh properties panel if the erased ring is currently selected."""
        if self._selected_id == inst_id:
            self._props.load(self._instances.get(inst_id))

    # ── Rotate ────────────────────────────────────────────────────────────────

    def _rotate_selected_cw(self):
        self._rotate_selected(cw=True)

    def _rotate_selected_ccw(self):
        self._rotate_selected(cw=False)

    def _rotate_selected(self, cw: bool):
        from canvas import ComponentItem
        rotated = []
        for item in self.scene.selectedItems():
            if isinstance(item, ComponentItem):
                if cw:
                    item.inst.rotate_cw()
                else:
                    item.inst.rotate_ccw()
                item._rebuild()
                rotated.append(item.inst)

        if rotated:
            direction = "CW" if cw else "CCW"
            deg = rotated[-1].rotation
            self._status.showMessage(f"Rotated {direction} → {deg}°")
            if self._selected_id in {i.inst_id for i in rotated}:
                self._props.load(self._instances.get(self._selected_id))

    # ── Workspace save / load ─────────────────────────────────────────────────

    def _save_workspace(self):
        """Save to the autosave path (next to the script), no dialog needed."""
        instances = list(self._instances.values())
        try:
            save_workspace(instances, self._AUTOSAVE_PATH)
            self._status.showMessage(
                f"Workspace saved → {os.path.basename(self._AUTOSAVE_PATH)}"
                f"  ({len(instances)} component{'s' if len(instances) != 1 else ''})"
            )
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def _load_workspace_dialog(self):
        """Let the user pick any .json workspace file to load."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Workspace", "", "Workspace files (*.json);;All files (*)"
        )
        if path:
            self._load_workspace_from(path, silent=False)

    def _load_workspace_from(self, path: str, silent: bool = False):
        """
        Clear the canvas and restore all instances from *path*.

        Parameters
        ----------
        silent : if True, show the result only in the status bar (no dialog);
                 used for the auto-load on startup.
        """
        try:
            loaded = load_workspace(path)
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Load failed", str(e))
            else:
                self._status.showMessage(f"Auto-load failed: {e}")
            return

        # Clear existing canvas
        for iid in list(self._instances.keys()):
            self.scene.remove_component(iid)
        self._instances.clear()
        self._selected_id = None
        self._props.load(None)

        # Restore instances
        for inst in loaded:
            self._instances[inst.inst_id] = inst
            self.scene.add_component(inst)

        msg = (
            f"Loaded {len(loaded)} component{'s' if len(loaded) != 1 else ''}"
            f" from {os.path.basename(path)}"
        )
        self._status.showMessage(msg)
        if not silent:
            QMessageBox.information(self, "Workspace loaded", msg)

    def _export_plot_script(self):
        instances = list(self._instances.values())
        if not instances:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default   = f"layout_{timestamp}.py"
        path, _   = QFileDialog.getSaveFileName(
            self, "Export GDS script", default, "Python scripts (*.py)"
        )
        if not path:
            return

        try:
            export_gds_script(instances, self.cfg, path)
            import os as _os
            gds_name = _os.path.splitext(_os.path.basename(path))[0] + ".gds"
            self._status.showMessage(f"GDS script exported → {_os.path.basename(path)}")
            QMessageBox.information(
                self, "Export",
                f"Saved:\n{path}\n\n"
                f"Run it to produce:\n  {gds_name}\n\n"
                f"Command:\n  python {_os.path.basename(path)}\n"
                f"  python {_os.path.basename(path)} /custom/output.gds"
            )
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    # ── Export GDS ────────────────────────────────────────────────────────────

    def _export_gds(self):
        instances = list(self._instances.values())
        if not instances:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default   = f"layout_{timestamp}.gds"
        path, _   = QFileDialog.getSaveFileName(
            self, "Export GDS", default, "GDS files (*.gds)"
        )
        if not path:
            return

        try:
            export_to_gds(instances, self.cfg, path)
            self._status.showMessage(f"Exported → {os.path.basename(path)}")
            QMessageBox.information(self, "Export", f"Saved:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    # ── Dark style ────────────────────────────────────────────────────────────

    def _apply_dark_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #141414;
                color: #cccccc;
            }
            QGroupBox {
                border: 0.5px solid #2e2e2e;
                border-radius: 4px;
                margin-top: 6px;
                padding-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 6px;
                color: #777;
            }
            QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {
                background: #1c1c1c;
                border: 0.5px solid #2e2e2e;
                border-radius: 3px;
                padding: 2px 5px;
                color: #ccc;
                font-size: 11px;
            }
            QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus, QComboBox:focus {
                border-color: #4fc3f7;
            }
            QCheckBox {
                color: #aaa;
            }
            QCheckBox::indicator {
                width: 12px;
                height: 12px;
                border: 0.5px solid #444;
                border-radius: 2px;
                background: #1c1c1c;
            }
            QCheckBox::indicator:checked {
                background: #4fc3f7;
                border-color: #4fc3f7;
            }
            QFrame[frameShape="5"] {
                color: #222;
                max-width: 1px;
            }
            QScrollBar:vertical {
                background: #111;
                width: 6px;
            }
            QScrollBar::handle:vertical {
                background: #333;
                border-radius: 3px;
            }
            QPushButton, QToolButton {
                background: #1c1c1c;
                border: 0.5px solid #2e2e2e;
                border-radius: 4px;
                color: #ccc;
                padding: 4px 10px;
                font-size: 12px;
            }
            QPushButton:hover, QToolButton:hover {
                background: #252525;
                border-color: #3a3a3a;
            }
            QStatusBar {
                background: #0d0d0d;
                border-top: 0.5px solid #222;
                color: #555;
                font-size: 11px;
            }
            QLabel {
                color: #ccc;
            }
        """)

    def closeEvent(self, event):
        """Autosave workspace when the window is closed."""
        if self._instances:
            try:
                save_workspace(list(self._instances.values()), self._AUTOSAVE_PATH)
            except Exception:
                pass   # never block closing due to a save error
        event.accept()