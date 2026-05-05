"""
ui/sweep_dialog.py — Parameter sweep + M×N array generation.

Two dialogs:

SweepDialog (original, unchanged)
    Generates an M×N array of clones of a single selected component,
    with one parameter swept linearly across all copies.

GroupSweepDialog (new)
    Generates an M×N array of clones of an entire ComponentGroup,
    with one parameter of one designated member component swept linearly.
    Each array position produces:
      - Deep-copies of all member components (fresh IDs)
      - A new ComponentGroup record wrapping those copies
      - The swept parameter applied only to the copy of the target member
    Everything is committed as a single undo-able BatchCommand.
"""

from __future__ import annotations

import copy
import uuid
from typing import List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QSpinBox, QDialogButtonBox, QFrame,
    QWidget, QGridLayout, QMessageBox,
)
from PyQt6.QtCore import Qt

from core.model import (
    GDSComponent, ComponentKind, ComponentGroup, DesignScene,
    Point, dbu_to_um, um_to_dbu,
)
from core.commands import AddComponent, BatchCommand, GroupComponents
from core.cell_library import place_cell
from core.model import BBox

# ── Styling helpers ───────────────────────────────────────────────────────────

_BG     = "#1e293b"
_BORDER = "#334155"
_TEXT   = "#e2e8f0"
_MUTED  = "#94a3b8"
_ACCENT = "#38bdf8"


def _label(text: str, muted: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {'#94a3b8' if muted else _TEXT}; "
        f"font-size: 12px; background: transparent;"
    )
    return lbl


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"color: {_BORDER};")
    return f


def _dspin(value: float, lo: float, hi: float,
           step: float = 0.5, suffix: str = " µm") -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi); sb.setValue(value)
    sb.setSingleStep(step); sb.setDecimals(3); sb.setSuffix(suffix)
    sb.setFixedWidth(130)
    sb.setStyleSheet(_spin_style("QDoubleSpinBox"))
    return sb


def _ispin(value: int, lo: int, hi: int) -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(lo, hi); sb.setValue(value)
    sb.setFixedWidth(130)
    sb.setStyleSheet(_spin_style("QSpinBox"))
    return sb


def _spin_style(cls: str) -> str:
    return f"""
        {cls} {{
            background: #0f172a; border: 1px solid {_BORDER};
            border-radius: 4px; color: {_TEXT};
            padding: 4px 6px; font-size: 12px;
        }}
        {cls}::up-button, {cls}::down-button {{ width: 16px; }}
    """


def _btn_style() -> str:
    return f"""
        QPushButton {{
            background: #0f172a; border: 1px solid {_BORDER};
            border-radius: 4px; color: {_TEXT};
            padding: 6px 18px; font-size: 12px;
        }}
        QPushButton:hover {{ border-color: {_ACCENT}; color: {_ACCENT}; }}
    """


def _combo_style() -> str:
    return f"""
        QComboBox {{
            background: #0f172a; border: 1px solid {_BORDER};
            border-radius: 4px; color: {_TEXT};
            padding: 4px 8px; font-size: 12px;
        }}
        QComboBox QAbstractItemView {{ background: #0f172a; color: {_TEXT}; }}
    """


# ── Parameter descriptors ─────────────────────────────────────────────────────

class _Param:
    def __init__(self, key, label, unit, lo, hi, default_step, is_int=False):
        self.key          = key
        self.label        = label
        self.unit         = unit
        self.lo           = lo
        self.hi           = hi
        self.default_step = default_step
        self.is_int       = is_int

    def to_dbu(self, v: float) -> int:
        return int(round(v)) if self.is_int else um_to_dbu(v)

    def from_comp(self, comp: GDSComponent) -> float:
        raw = getattr(comp, self.key, None) or 0
        return float(raw) if self.is_int else dbu_to_um(raw)


_ALL_PARAMS = {
    "width":      _Param("width",      "Width",      "µm", 0.001, 10_000, 1.0),
    "height":     _Param("height",     "Height",     "µm", 0.001, 10_000, 1.0),
    "path_width": _Param("path_width", "Path width", "µm", 0.001,  1_000, 0.5),
    "layer":      _Param("layer",      "Layer",      "",   0,      63,    1,   True),
}

_KIND_PARAMS = {
    ComponentKind.RECTANGLE: ["width", "height", "layer"],
    ComponentKind.POLYGON:   ["layer"],
    ComponentKind.PATH:      ["path_width", "layer"],
}


# ── SweepDialog (original — single component) ─────────────────────────────────

class SweepDialog(QDialog):

    def __init__(self, comp: GDSComponent, design: DesignScene,
                 cmd_stack, parent=None) -> None:
        super().__init__(parent)
        self._comp      = comp
        self._design    = design
        self._cmd_stack = cmd_stack

        self.setWindowTitle("Array / Parameter Sweep")
        self.setMinimumWidth(420)
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")
        self._build_ui()
        self._on_param_changed()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Array / Parameter Sweep")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: bold;")
        root.addWidget(title)
        sub = _label(
            f"{self._comp.kind.name.capitalize()}  ·  id {self._comp.id}  ·  "
            f"layer {self._comp.layer}",
            muted=True,
        )
        root.addWidget(sub)
        root.addWidget(_sep())

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 110)

        def row(r: int, lbl: str, *widgets):
            grid.addWidget(_label(lbl, muted=True), r, 0)
            for c, w in enumerate(widgets, start=1):
                grid.addWidget(w, r, c)

        section = QLabel("Array")
        section.setStyleSheet(
            f"color: {_ACCENT}; font-size: 11px; letter-spacing: 1px; font-weight: bold;"
        )
        grid.addWidget(section, 0, 0, 1, 3)

        self._cols_spin = _ispin(3, 1, 256)
        self._rows_spin = _ispin(2, 1, 256)
        row(1, "Columns  (M)", self._cols_spin)
        row(2, "Rows  (N)",    self._rows_spin)

        self._gap_spin = _dspin(5.0, 0, 10_000, 1.0)
        row(3, "Gap  (X and Y)", self._gap_spin)

        sweep_section = QLabel("Sweep")
        sweep_section.setStyleSheet(section.styleSheet())
        grid.addWidget(sweep_section, 4, 0, 1, 3)

        self._param_combo = QComboBox()
        self._param_combo.setFixedWidth(130)
        self._param_combo.setStyleSheet(_combo_style())
        for key in _KIND_PARAMS.get(self._comp.kind, []):
            self._param_combo.addItem(_ALL_PARAMS[key].label, key)
        row(5, "Parameter", self._param_combo)

        self._start_dspin = _dspin(0, -10_000, 10_000)
        self._start_ispin = _ispin(0, 0, 63)
        row(6, "Start value", self._start_dspin)
        grid.addWidget(self._start_ispin, 6, 1)

        self._step_dspin = _dspin(1.0, -10_000, 10_000)
        self._step_ispin = _ispin(1, -63, 63)
        row(7, "Step size", self._step_dspin)
        grid.addWidget(self._step_ispin, 7, 1)

        root.addLayout(grid)
        root.addWidget(_sep())

        self._preview_lbl = _label("", muted=True)
        self._preview_lbl.setWordWrap(True)
        self._preview_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; "
            f"background: #0f172a; border: 1px solid {_BORDER}; "
            f"border-radius: 4px; padding: 8px;"
        )
        root.addWidget(self._preview_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate Array")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(_btn_style())
        root.addWidget(btns)

        self._param_combo.currentIndexChanged.connect(self._on_param_changed)
        for w in (self._cols_spin, self._rows_spin, self._gap_spin,
                  self._start_dspin, self._step_dspin,
                  self._start_ispin, self._step_ispin):
            w.valueChanged.connect(self._update_preview)

    def _current_param(self) -> _Param:
        return _ALL_PARAMS[self._param_combo.currentData()]

    def _on_param_changed(self) -> None:
        p      = self._current_param()
        is_int = p.is_int

        self._start_dspin.setVisible(not is_int)
        self._start_ispin.setVisible(is_int)
        self._step_dspin.setVisible(not is_int)
        self._step_ispin.setVisible(is_int)

        current = p.from_comp(self._comp)
        if is_int:
            self._start_ispin.setValue(int(current))
            self._step_ispin.setValue(int(p.default_step))
        else:
            self._start_dspin.setValue(current)
            self._step_dspin.setValue(p.default_step)

        self._update_preview()

    def _get_start(self) -> float:
        p = self._current_param()
        return float(self._start_ispin.value() if p.is_int else self._start_dspin.value())

    def _get_step(self) -> float:
        p = self._current_param()
        return float(self._step_ispin.value() if p.is_int else self._step_dspin.value())

    def _update_preview(self) -> None:
        p      = self._current_param()
        start  = self._get_start()
        step   = self._get_step()
        cols   = self._cols_spin.value()
        rows   = self._rows_spin.value()
        total  = cols * rows
        unit   = f" {p.unit}" if p.unit else ""

        sample_count = min(total, 6)
        vals = [f"{start + i * step:.3g}{unit}" for i in range(sample_count)]
        ellipsis = "  …" if total > 6 else ""

        self._preview_lbl.setText(
            f"  {cols} col{'s' if cols != 1 else ''}  ×  "
            f"{rows} row{'s' if rows != 1 else ''}  =  {total} copies\n"
            f"  {p.label}:  {',  '.join(vals)}{ellipsis}\n"
            f"  Gap: {self._gap_spin.value():.3g} µm  (edge-to-edge, uniform X and Y)"
        )

    def _on_accept(self) -> None:
        p     = self._current_param()
        start = self._get_start()
        step  = self._get_step()
        cols  = self._cols_spin.value()
        rows  = self._rows_spin.value()
        gap   = um_to_dbu(self._gap_spin.value())

        total = cols * rows
        if step == 0 and total > 1:
            reply = QMessageBox.question(
                self, "Zero Step",
                "Step size is zero — all copies will be identical. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        bb     = self._comp.bbox
        orig_w = bb.x_max - bb.x_min
        orig_h = bb.y_max - bb.y_min

        cmds = []
        for row_i in range(rows):
            for col_i in range(cols):
                if row_i == 0 and col_i == 0:
                    continue

                linear_idx = row_i * cols + col_i
                val_dbu    = p.to_dbu(start + linear_idx * step)

                clone_w = val_dbu if p.key == "width"  else orig_w
                clone_h = val_dbu if p.key == "height" else orig_h

                # X offset: use column-only index so every row shares the same
                # X grid.  The swept value at [row_i, col_i] uses the full
                # linear index; spacing must NOT — otherwise each row's columns
                # shift independently and the array is no longer rectangular.
                dx = 0
                for prev_col in range(col_i):
                    prev_val = p.to_dbu(start + prev_col * step)
                    prev_w   = prev_val if p.key == "width" else orig_w
                    dx += prev_w + gap

                dy = 0
                for prev_row in range(row_i):
                    row_heights = []
                    for c in range(cols):
                        li  = prev_row * cols + c
                        val = p.to_dbu(start + li * step)
                        row_heights.append(val if p.key == "height" else orig_h)
                    dy += max(row_heights) + gap

                clone       = copy.deepcopy(self._comp)
                clone.id    = uuid.uuid4().hex[:8]
                clone.ports = []

                clone.origin = Point(
                    self._comp.origin.x + dx,
                    self._comp.origin.y + dy,
                )
                if clone.points:
                    clone.points = [
                        Point(pt.x + dx, pt.y + dy)
                        for pt in clone.points
                    ]

                setattr(clone, p.key, val_dbu)
                cmds.append(AddComponent(clone))

        self._cmd_stack.execute(
            BatchCommand(cmds, f"Array {cols}×{rows}  sweep {p.label}")
        )
        self.accept()
    def _compute_cell_bbox(cell_id: str, params: dict) -> "BBox":
        result = place_cell(cell_id, Point(0, 0), params=params)
        xs = [c.bbox.x_min for c in result.components] + [c.bbox.x_max for c in result.components]
        ys = [c.bbox.y_min for c in result.components] + [c.bbox.y_max for c in result.components]
        return BBox(min(xs), min(ys), max(xs), max(ys))


# ── GroupSweepDialog — sweep a parameter within a duplicated group ─────────────

class GroupSweepDialog(QDialog):
    """
    M×N array sweep over an entire ComponentGroup.

    Two modes, selected automatically based on group metadata:

    CELL MODE  (group._cell_subgroups is non-empty)
    ────────────────────────────────────────────────
    The group was produced by merging two or more parametric cell groups
    (e.g. two square_node or manhattan_jj instances).  MergeGroups stores the
    original cell descriptors in group._cell_subgroups so we can reconstruct
    each cell at any parameter value via place_cell().

    Target picker lists the original *cells* by name (not raw rectangles).
    Parameter picker lists that cell's µm-space catalogue params
    (square_x, square_y, cap_width, lead_width, …).

    For each array slot the target cell is rebuilt via place_cell() with the
    swept value; all other cells are deep-copied and translated unchanged.
    The resulting flat component list is grouped under a new merged group that
    also carries _cell_subgroups so the result is itself sweep-able.

    RAW MODE  (no _cell_subgroups)
    ────────────────────────────────
    Original behaviour: target picker lists individual GDSComponent members,
    parameter picker offers width / height / path_width / layer.
    The swept field is applied with setattr on the clone.
    """

    def __init__(self, group: ComponentGroup, design: DesignScene,
                 cmd_stack, parent=None) -> None:
        super().__init__(parent)
        self._group     = group
        self._design    = design
        self._cmd_stack = cmd_stack

        # Resolve live member components (guard against stale IDs)
        self._members: List[GDSComponent] = [
            design.get(cid) for cid in group.member_ids
            if design.get(cid) is not None
        ]

        # Detect cell mode: the group was merged from parametric cells.
        # _cell_subgroups now includes non-cell (plain) entries too, so check
        # that at least one entry has a real cell_id before entering cell mode.
        self._cell_subgroups: list = getattr(group, "_cell_subgroups", [])
        self._is_cell_mode: bool   = any(sg.get("cell_id") for sg in self._cell_subgroups)

        self.setWindowTitle("Group Array / Parameter Sweep")
        self.setMinimumWidth(480)
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")
        self._build_ui()
        self._on_target_changed()   # seed parameter combo + preview

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        from core.cell_library import CELL_BY_ID

        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        # Header
        mode_tag = "Cell" if self._is_cell_mode else "Component"
        title = QLabel("Group Array / Parameter Sweep")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: bold;")
        root.addWidget(title)

        sub = _label(
            f"Group  '{self._group.name}'  ·  {len(self._members)} members  ·  id {self._group.id}"
            + (f"  ·  {len(self._cell_subgroups)} cells" if self._is_cell_mode else ""),
            muted=True,
        )
        root.addWidget(sub)
        root.addWidget(_sep())

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 130)

        def row(r: int, lbl: str, *widgets):
            grid.addWidget(_label(lbl, muted=True), r, 0)
            for c, w in enumerate(widgets, start=1):
                grid.addWidget(w, r, c)

        # ── Array section ─────────────────────────────────────────────────────
        arr_hdr = QLabel("Array")
        arr_hdr.setStyleSheet(
            f"color: {_ACCENT}; font-size: 11px; letter-spacing: 1px; font-weight: bold;"
        )
        grid.addWidget(arr_hdr, 0, 0, 1, 3)

        self._cols_spin = _ispin(3, 1, 256)
        self._rows_spin = _ispin(2, 1, 256)
        self._gap_spin  = _dspin(5.0, 0, 10_000, 1.0)
        row(1, "Columns  (M)", self._cols_spin)
        row(2, "Rows  (N)",    self._rows_spin)
        row(3, "Gap  (X and Y)", self._gap_spin)

        # ── Sweep section ─────────────────────────────────────────────────────
        sw_hdr = QLabel("Sweep")
        sw_hdr.setStyleSheet(arr_hdr.styleSheet())
        grid.addWidget(sw_hdr, 4, 0, 1, 3)

        # Target picker — cells in cell mode, raw members otherwise
        self._target_combo = QComboBox()
        self._target_combo.setFixedWidth(240)
        self._target_combo.setStyleSheet(_combo_style())

        if self._is_cell_mode:
            # One entry per cell sub-group descriptor (skip plain/passthrough entries)
            for i, sg in enumerate(self._cell_subgroups):
                if not sg.get("cell_id"):
                    continue   # passthrough non-cell entry — not a sweep target
                cdef = CELL_BY_ID.get(sg["cell_id"])
                label = sg["name"] if sg["name"] else (cdef.name if cdef else sg["cell_id"])
                self._target_combo.addItem(label, i)   # data = subgroup index
            row(5, "Target cell", self._target_combo)
        else:
            for comp in self._members:
                label = f"{comp.kind.name.capitalize()}  ·  {comp.id}  (layer {comp.layer})"
                self._target_combo.addItem(label, comp.id)
            row(5, "Target member", self._target_combo)

        # Parameter picker (repopulated when target changes)
        self._param_combo = QComboBox()
        self._param_combo.setFixedWidth(240)
        self._param_combo.setStyleSheet(_combo_style())
        row(6, "Parameter", self._param_combo)

        # Start / step spinboxes — both double and int variants present,
        # visibility toggled by _on_param_changed.
        self._start_dspin = _dspin(0, -10_000, 10_000)
        self._start_ispin = _ispin(0, 0, 63)
        row(7, "Start value", self._start_dspin)
        grid.addWidget(self._start_ispin, 7, 1)

        self._step_dspin = _dspin(1.0, -10_000, 10_000)
        self._step_ispin = _ispin(1, -63, 63)
        row(8, "Step size", self._step_dspin)
        grid.addWidget(self._step_ispin, 8, 1)

        root.addLayout(grid)
        root.addWidget(_sep())

        # Preview
        self._preview_lbl = _label("", muted=True)
        self._preview_lbl.setWordWrap(True)
        self._preview_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; "
            f"background: #0f172a; border: 1px solid {_BORDER}; "
            f"border-radius: 4px; padding: 8px;"
        )
        root.addWidget(self._preview_lbl)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate Group Array")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(_btn_style())
        root.addWidget(btns)

        # Wiring
        self._target_combo.currentIndexChanged.connect(self._on_target_changed)
        self._param_combo.currentIndexChanged.connect(self._on_param_changed)
        for w in (self._cols_spin, self._rows_spin, self._gap_spin,
                  self._start_dspin, self._step_dspin,
                  self._start_ispin, self._step_ispin):
            w.valueChanged.connect(self._update_preview)

    # ── Helpers — cell mode ───────────────────────────────────────────────────

    def _current_subgroup(self) -> dict | None:
        """Return the selected cell sub-group descriptor (cell mode only)."""
        if not self._is_cell_mode:
            return None
        idx = self._target_combo.currentData()
        if idx is None or idx >= len(self._cell_subgroups):
            return None
        return self._cell_subgroups[idx]

    # ── Helpers — raw mode ────────────────────────────────────────────────────

    def _target_comp(self) -> GDSComponent | None:
        """Return the selected raw GDSComponent (raw mode only)."""
        if self._is_cell_mode:
            return None
        cid = self._target_combo.currentData()
        return self._design.get(cid)

    def _current_param_raw(self) -> _Param:
        """Return _Param for raw mode (key is an _ALL_PARAMS key)."""
        return _ALL_PARAMS[self._param_combo.currentData()]

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_target_changed(self) -> None:
        """Repopulate the parameter combo when the selected target changes."""
        self._param_combo.blockSignals(True)
        self._param_combo.clear()

        if self._is_cell_mode:
            from core.cell_library import CELL_BY_ID
            sg = self._current_subgroup()
            if sg:
                cdef = CELL_BY_ID.get(sg["cell_id"])
                if cdef:
                    # Offer all float params from the cell catalogue entry
                    for key, default in cdef.defaults.items():
                        if isinstance(default, float):
                            self._param_combo.addItem(key, key)
        else:
            comp = self._target_comp()
            if comp is not None:
                for key in _KIND_PARAMS.get(comp.kind, []):
                    self._param_combo.addItem(_ALL_PARAMS[key].label, key)

        self._param_combo.blockSignals(False)
        self._on_param_changed()

    def _on_param_changed(self) -> None:
        if self._param_combo.count() == 0:
            return

        if self._is_cell_mode:
            # All cell params are float µm values — always use double spinboxes
            self._start_dspin.setVisible(True)
            self._start_ispin.setVisible(False)
            self._step_dspin.setVisible(True)
            self._step_ispin.setVisible(False)

            key = self._param_combo.currentData()
            sg  = self._current_subgroup()
            if sg and key:
                current = sg["cell_params"].get(key, 1.0)
                self._start_dspin.blockSignals(True)
                self._step_dspin.blockSignals(True)
                self._start_dspin.setValue(current)
                self._step_dspin.setValue(round(current * 0.1, 3) or 0.1)
                self._start_dspin.blockSignals(False)
                self._step_dspin.blockSignals(False)
        else:
            p      = self._current_param_raw()
            is_int = p.is_int
            self._start_dspin.setVisible(not is_int)
            self._start_ispin.setVisible(is_int)
            self._step_dspin.setVisible(not is_int)
            self._step_ispin.setVisible(is_int)

            comp = self._target_comp()
            if comp:
                current = p.from_comp(comp)
                if is_int:
                    self._start_ispin.setValue(int(current))
                    self._step_ispin.setValue(int(p.default_step))
                else:
                    self._start_dspin.setValue(current)
                    self._step_dspin.setValue(p.default_step)

        self._update_preview()

    def _get_start(self) -> float:
        if self._is_cell_mode:
            return self._start_dspin.value()
        p = self._current_param_raw()
        return float(self._start_ispin.value() if p.is_int else self._start_dspin.value())

    def _get_step(self) -> float:
        if self._is_cell_mode:
            return self._step_dspin.value()
        p = self._current_param_raw()
        return float(self._step_ispin.value() if p.is_int else self._step_dspin.value())

    def _update_preview(self) -> None:
        if self._param_combo.count() == 0:
            return
        start  = self._get_start()
        step   = self._get_step()
        cols   = self._cols_spin.value()
        rows   = self._rows_spin.value()
        total  = cols * rows

        if self._is_cell_mode:
            key   = self._param_combo.currentData() or "?"
            sg    = self._current_subgroup()
            tname = sg["name"] if sg else "?"
            unit  = " µm"
        else:
            p     = self._current_param_raw()
            key   = p.label
            comp  = self._target_comp()
            tname = f"{comp.kind.name.capitalize()} {comp.id}" if comp else "?"
            unit  = f" {p.unit}" if p.unit else ""

        sample_count = min(total, 6)
        vals = [f"{start + i * step:.3g}{unit}" for i in range(sample_count)]
        ellipsis = "  …" if total > 6 else ""

        self._preview_lbl.setText(
            f"  {cols} col{'s' if cols != 1 else ''}  ×  "
            f"{rows} row{'s' if rows != 1 else ''}  =  {total} group copies\n"
            f"  Sweep  {key}  of  {tname}:  {',  '.join(vals)}{ellipsis}\n"
            f"  Gap: {self._gap_spin.value():.3g} µm  between group bboxes (X and Y)"
        )

    # ── Accept ────────────────────────────────────────────────────────────────

    def _on_accept(self) -> None:
        if self._param_combo.count() == 0:
            QMessageBox.warning(self, "Sweep", "No sweepable parameter available.")
            return

        if self._is_cell_mode:
            self._accept_cell_mode()
        else:
            self._accept_raw_mode()

    # ── Cell mode generation ──────────────────────────────────────────────────

    def _accept_cell_mode(self) -> None:
        """
        Generate an M×N array by re-placing the target cell at each swept
        parameter value and deep-copying all other cells unchanged.

        For each grid slot we:
          1. Compute the (dx, dy) translation from the group's anchor.
          2. Rebuild the target cell via place_cell() at the new origin with
             the swept param value — this gives geometrically correct shapes.
          3. Deep-copy every other cell's components and translate them by
             the same (dx, dy).
          4. Assemble all components into a flat merged group (with
             _cell_subgroups preserved) via AddComponent + GroupComponents.
        """
        from core.cell_library import CELL_BY_ID, place_cell
        from core.model import BBox

        key        = self._param_combo.currentData()
        start      = self._get_start()
        step       = self._get_step()
        cols       = self._cols_spin.value()
        rows       = self._rows_spin.value()
        gap        = um_to_dbu(self._gap_spin.value())
        sg_idx     = self._target_combo.currentData()
        target_sg  = self._cell_subgroups[sg_idx]

        total = cols * rows
        if step == 0 and total > 1:
            reply = QMessageBox.question(
                self, "Zero Step",
                "Step size is zero — all group copies will be identical. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # ── Reference bbox: build the target cell at start value at origin (0,0)
        # so we know how big each column will be when the swept param changes.
        def _cell_bbox(cell_id: str, params: dict) -> "BBox":
            result = place_cell(cell_id, Point(0, 0), params=params)
            xs = [c.bbox.x_min for c in result.components] + \
                 [c.bbox.x_max for c in result.components]
            ys = [c.bbox.y_min for c in result.components] + \
                 [c.bbox.y_max for c in result.components]
            return BBox(min(xs), min(ys), max(xs), max(ys))

        # Full group bbox for non-target cells' contribution
        grp_bb = self._group.bbox_from(self._design.components)
        grp_w  = grp_bb.x_max - grp_bb.x_min
        grp_h  = grp_bb.y_max - grp_bb.y_min
        grp_ox = grp_bb.x_min
        grp_oy = grp_bb.y_min

        # Target cell bbox at each value — precomputed column-0 widths
        ref_params_start = dict(target_sg["cell_params"])
        ref_params_start[key] = start
        try:
            ref_tgt_bb = _cell_bbox(target_sg["cell_id"], ref_params_start)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Sweep", f"Cannot build reference cell: {exc}")
            return
        # How much the target sub-bbox changes relative to original
        orig_params = dict(target_sg["cell_params"])
        try:
            orig_tgt_bb = _cell_bbox(target_sg["cell_id"], orig_params)
        except Exception:
            orig_tgt_bb = ref_tgt_bb
        orig_tgt_w = orig_tgt_bb.x_max - orig_tgt_bb.x_min
        orig_tgt_h = orig_tgt_bb.y_max - orig_tgt_bb.y_min

        # IDs of components belonging to the target sub-group (for exclusion
        # when deep-copying non-target members)
        target_member_ids: set = set(target_sg["member_ids"])

        all_cmds = []

        for row_i in range(rows):
            for col_i in range(cols):
                if row_i == 0 and col_i == 0:
                    continue   # original group stays in place

                linear_idx = row_i * cols + col_i
                val        = start + linear_idx * step
                params_for_slot = dict(target_sg["cell_params"])
                params_for_slot[key] = val

                # ── Compute swept cell bbox to adjust group spacing ────────────
                try:
                    slot_tgt_bb = _cell_bbox(target_sg["cell_id"], params_for_slot)
                except Exception:
                    slot_tgt_bb = ref_tgt_bb
                slot_tgt_w = slot_tgt_bb.x_max - slot_tgt_bb.x_min
                slot_tgt_h = slot_tgt_bb.y_max - slot_tgt_bb.y_min

                slot_grp_w = grp_w + (slot_tgt_w - orig_tgt_w)
                slot_grp_h = grp_h + (slot_tgt_h - orig_tgt_h)

                # ── X offset: column-only index for consistent X grid ──────────
                dx = 0
                for prev_col in range(col_i):
                    prev_val = start + prev_col * step
                    prev_params = dict(target_sg["cell_params"])
                    prev_params[key] = prev_val
                    try:
                        prev_tgt_bb = _cell_bbox(target_sg["cell_id"], prev_params)
                        prev_grp_w = grp_w + (
                            (prev_tgt_bb.x_max - prev_tgt_bb.x_min) - orig_tgt_w
                        )
                    except Exception:
                        prev_grp_w = grp_w
                    dx += prev_grp_w + gap

                # ── Y offset: tallest group copy in each preceding row ─────────
                dy = 0
                for prev_row in range(row_i):
                    row_hs = []
                    for c in range(cols):
                        li = prev_row * cols + c
                        pv = start + li * step
                        pp = dict(target_sg["cell_params"])
                        pp[key] = pv
                        try:
                            ptb = _cell_bbox(target_sg["cell_id"], pp)
                            row_hs.append(grp_h + ((ptb.y_max - ptb.y_min) - orig_tgt_h))
                        except Exception:
                            row_hs.append(grp_h)
                    dy += max(row_hs) + gap

                # ── Build components for this slot ────────────────────────────
                slot_comp_ids: list[str] = []
                new_cell_subgroups: list[dict] = []

                for sg_i, sg in enumerate(self._cell_subgroups):
                    if sg_i == sg_idx:
                        # Rebuild the target cell at the swept value
                        origin = Point(grp_ox + dx, grp_oy + dy)
                        try:
                            result = place_cell(sg["cell_id"], origin,
                                                params=params_for_slot)
                        except (KeyError, ValueError) as exc:
                            QMessageBox.warning(
                                self, "Sweep",
                                f"Slot [{row_i},{col_i}]: {exc}"
                            )
                            return
                        for comp in result.components:
                            all_cmds.append(AddComponent(comp))
                            slot_comp_ids.append(comp.id)
                        new_cell_subgroups.append({
                            "name":       result.group_name,
                            "cell_id":    sg["cell_id"],
                            "cell_params": dict(params_for_slot),
                            "member_ids": [c.id for c in result.components],
                        })
                    else:
                        # Deep-copy the other cell's members, translated
                        new_member_ids: list[str] = []
                        for cid in sg["member_ids"]:
                            orig_comp = self._design.get(cid)
                            if orig_comp is None:
                                continue
                            clone = copy.deepcopy(orig_comp)
                            clone.id = uuid.uuid4().hex[:8]
                            clone.ports = []
                            clone.origin = Point(orig_comp.origin.x + dx,
                                                 orig_comp.origin.y + dy)
                            if clone.points:
                                clone.points = [
                                    Point(pt.x + dx, pt.y + dy)
                                    for pt in clone.points
                                ]
                            all_cmds.append(AddComponent(clone))
                            slot_comp_ids.append(clone.id)
                            new_member_ids.append(clone.id)
                        new_cell_subgroups.append({
                            "name":       sg["name"],
                            "cell_id":    sg["cell_id"],
                            "cell_params": dict(sg["cell_params"]),
                            "member_ids": new_member_ids,
                        })

                # ── Create merged group for this slot, preserving sub-group info
                new_group_name = f"{self._group.name}_{linear_idx}"
                gc = GroupComponents(slot_comp_ids, new_group_name)
                # Attach cell subgroup metadata so the copy is itself sweep-able
                gc._group._cell_subgroups = new_cell_subgroups
                all_cmds.append(gc)

        if not all_cmds:
            self.accept()
            return

        target_label = f"{target_sg['name']}.{key}"
        self._cmd_stack.execute(
            BatchCommand(
                all_cmds,
                f"Group array {cols}×{rows}  sweep {target_label} "
                f"[{start:.3g}…{start + (total - 1) * step:.3g}] µm",
            )
        )
        self.accept()

    # ── Raw mode generation (original behaviour, unchanged) ───────────────────

    def _accept_raw_mode(self) -> None:
        p          = self._current_param_raw()
        target_cid = self._target_combo.currentData()
        start      = self._get_start()
        step       = self._get_step()
        cols       = self._cols_spin.value()
        rows       = self._rows_spin.value()
        gap        = um_to_dbu(self._gap_spin.value())

        total = cols * rows
        if step == 0 and total > 1:
            reply = QMessageBox.question(
                self, "Zero Step",
                "Step size is zero — all group copies will be identical. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        grp_bb  = self._group.bbox_from(self._design.components)
        grp_w   = grp_bb.x_max - grp_bb.x_min
        grp_h   = grp_bb.y_max - grp_bb.y_min

        target_comp     = self._design.get(target_cid)
        orig_target_val = getattr(target_comp, p.key, 0) or 0

        all_cmds = []

        for row_i in range(rows):
            for col_i in range(cols):
                if row_i == 0 and col_i == 0:
                    continue

                linear_idx = row_i * cols + col_i
                val_dbu    = p.to_dbu(start + linear_idx * step)

                dx = 0
                for prev_col in range(col_i):
                    prev_val = p.to_dbu(start + prev_col * step)
                    prev_grp_w = grp_w + (prev_val - orig_target_val) if p.key == "width" else grp_w
                    dx += prev_grp_w + gap

                dy = 0
                for prev_row in range(row_i):
                    row_hs = []
                    for c in range(cols):
                        li  = prev_row * cols + c
                        val = p.to_dbu(start + li * step)
                        row_hs.append(
                            grp_h + (val - orig_target_val) if p.key == "height" else grp_h
                        )
                    dy += max(row_hs) + gap

                id_map: dict[str, str] = {}
                for member in self._members:
                    clone       = copy.deepcopy(member)
                    clone.id    = uuid.uuid4().hex[:8]
                    clone.ports = []
                    clone.origin = Point(member.origin.x + dx, member.origin.y + dy)
                    if clone.points:
                        clone.points = [
                            Point(pt.x + dx, pt.y + dy) for pt in clone.points
                        ]
                    if member.id == target_cid:
                        setattr(clone, p.key, val_dbu)
                    id_map[member.id] = clone.id
                    all_cmds.append(AddComponent(clone))

                new_group_name = f"{self._group.name}_{linear_idx}"
                new_member_ids = [id_map[mid] for mid in self._group.member_ids
                                  if mid in id_map]
                all_cmds.append(GroupComponents(new_member_ids, new_group_name))

        if not all_cmds:
            self.accept()
            return

        self._cmd_stack.execute(
            BatchCommand(
                all_cmds,
                f"Group array {cols}×{rows}  sweep {p.label} on {target_cid}",
            )
        )
        self.accept()

class CellSweepDialog(QDialog):
    """
    2-D array / parameter sweep for a parametric cell group.

    Generates an M (cols) × N (rows) grid of re-placed cell copies with one
    cell-level parameter swept linearly across all copies (linear index:
    row_i * cols + col_i).  The grid spacing accounts for the actual bbox of
    each copy so columns stay flush even when the swept parameter changes the
    cell width or height.

    Column X-spacing uses the column-only index (prev_col) for reference
    widths so every row shares the same X grid — identical fix to SweepDialog.

    Layout
    ------
    ┌──────────────────────────────────────┐
    │  Cell: ManhattanJJ (…)               │
    │  ── Array ──────────────────────     │
    │  Columns (M):  [spin]                │
    │  Rows    (N):  [spin]                │
    │  Gap (X & Y):  [spin]  µm            │
    │  ── Sweep ──────────────────────     │
    │  Parameter:    [combo ▼]             │
    │  Start value:  [spin]  µm            │
    │  Step size:    [spin]  µm            │
    │  [ Cancel ]          [ Generate ]   │
    └──────────────────────────────────────┘
    """

    _BG     = "#1e293b"
    _BORDER = "#334155"
    _TEXT   = "#e2e8f0"
    _MUTED  = "#94a3b8"
    _ACCENT = "#38bdf8"

    def __init__(self, group, design, scene, parent=None) -> None:
        super().__init__(parent)
        self._group  = group
        self._design = design
        self._scene  = scene

        from core.cell_library import CELL_BY_ID
        self._cdef = CELL_BY_ID[group.cell_id]

        # Current params = catalogue defaults overridden by stored values
        self._current_params = dict(self._cdef.defaults)
        stored = getattr(self._group, "_cell_params", {})
        self._current_params.update(stored)

        self.setWindowTitle(f"Cell Array Sweep — {self._cdef.name}")
        self.setMinimumWidth(420)
        self.setStyleSheet(f"background: {self._BG}; color: {self._TEXT};")
        self._build_ui()
        self._on_param_changed()

    # ── helpers ────────────────────────────────────────────────────────────────

    def _label(self, text, muted=False):
        from PyQt6.QtWidgets import QLabel
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {'#94a3b8' if muted else self._TEXT}; "
            f"font-size: 12px; background: transparent;"
        )
        return lbl

    def _dspin(self, value, lo, hi, step=0.5, suffix=" µm"):
        from PyQt6.QtWidgets import QDoubleSpinBox
        sb = QDoubleSpinBox()
        sb.setRange(lo, hi); sb.setValue(value)
        sb.setSingleStep(step); sb.setDecimals(3); sb.setSuffix(suffix)
        sb.setFixedWidth(130)
        sb.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: #0f172a; border: 1px solid {self._BORDER};
                border-radius: 4px; color: {self._TEXT};
                padding: 4px 6px; font-size: 12px;
            }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; }}
        """)
        return sb

    def _ispin(self, value, lo, hi):
        from PyQt6.QtWidgets import QSpinBox
        sb = QSpinBox()
        sb.setRange(lo, hi); sb.setValue(value)
        sb.setFixedWidth(130)
        sb.setStyleSheet(f"""
            QSpinBox {{
                background: #0f172a; border: 1px solid {self._BORDER};
                border-radius: 4px; color: {self._TEXT};
                padding: 4px 6px; font-size: 12px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
        """)
        return sb

    def _section(self, text):
        from PyQt6.QtWidgets import QLabel
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {self._ACCENT}; font-size: 11px; "
            f"letter-spacing: 1px; font-weight: bold;"
        )
        return lbl

    def _sep(self):
        from PyQt6.QtWidgets import QFrame
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color: {self._BORDER};")
        return f

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        from PyQt6.QtWidgets import (
            QComboBox, QDialogButtonBox, QGridLayout,
        )

        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        # Header
        title = self._label(f"Cell Array / Parameter Sweep")
        title.setStyleSheet(
            f"color: {self._TEXT}; font-size: 15px; font-weight: bold;"
        )
        root.addWidget(title)
        root.addWidget(self._label(
            f"{self._cdef.name}  ·  {self._group.name}  ·  id {self._group.id}",
            muted=True,
        ))
        root.addWidget(self._sep())

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 120)

        def row(r, lbl, *widgets):
            grid.addWidget(self._label(lbl, muted=True), r, 0)
            for c, w in enumerate(widgets, start=1):
                grid.addWidget(w, r, c)

        # ── Array section ──────────────────────────────────────────────────────
        grid.addWidget(self._section("Array"), 0, 0, 1, 3)

        self._cols_spin = self._ispin(3, 1, 256)
        self._rows_spin = self._ispin(2, 1, 256)
        self._gap_spin  = self._dspin(5.0, 0, 10_000, 1.0)
        row(1, "Columns  (M)", self._cols_spin)
        row(2, "Rows  (N)",    self._rows_spin)
        row(3, "Gap  (X and Y)", self._gap_spin)

        # ── Sweep section ──────────────────────────────────────────────────────
        grid.addWidget(self._section("Sweep"), 4, 0, 1, 3)

        self._param_combo = QComboBox()
        self._param_combo.setFixedWidth(130)
        self._param_combo.setStyleSheet(f"""
            QComboBox {{
                background: #0f172a; border: 1px solid {self._BORDER};
                border-radius: 4px; color: {self._TEXT};
                padding: 4px 8px; font-size: 12px;
            }}
            QComboBox QAbstractItemView {{ background: #0f172a; color: {self._TEXT}; }}
        """)
        float_params = [k for k, v in self._cdef.defaults.items()
                        if isinstance(v, float)]
        for key in float_params:
            self._param_combo.addItem(key, key)
        row(5, "Parameter", self._param_combo)

        self._start_sb = self._dspin(0.0, 0.001, 1000.0, 0.1)
        self._step_sb  = self._dspin(0.1, -1000.0, 1000.0, 0.1)
        row(6, "Start value", self._start_sb)
        row(7, "Step size",   self._step_sb)

        root.addLayout(grid)
        root.addWidget(self._sep())

        # Preview label
        self._preview_lbl = self._label("", muted=True)
        self._preview_lbl.setWordWrap(True)
        self._preview_lbl.setStyleSheet(
            f"color: {self._MUTED}; font-size: 11px; "
            f"background: #0f172a; border: 1px solid {self._BORDER}; "
            f"border-radius: 4px; padding: 8px;"
        )
        root.addWidget(self._preview_lbl)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate Array")
        btns.accepted.connect(self._do_sweep)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: #0f172a; border: 1px solid {self._BORDER};
                border-radius: 4px; color: {self._TEXT};
                padding: 6px 18px; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {self._ACCENT}; color: {self._ACCENT}; }}
        """)
        root.addWidget(btns)

        # Wire signals
        self._param_combo.currentIndexChanged.connect(self._on_param_changed)
        for w in (self._cols_spin, self._rows_spin, self._gap_spin,
                  self._start_sb, self._step_sb):
            w.valueChanged.connect(self._update_preview)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_param_changed(self) -> None:
        """Pre-fill start/step from the current value of the chosen parameter."""
        key = self._param_combo.currentData()
        if not key:
            return
        cur = self._current_params.get(key, self._cdef.defaults.get(key, 1.0))
        self._start_sb.blockSignals(True)
        self._step_sb.blockSignals(True)
        self._start_sb.setValue(cur)
        self._step_sb.setValue(round(cur * 0.1, 3) or 0.1)
        self._start_sb.blockSignals(False)
        self._step_sb.blockSignals(False)
        self._update_preview()

    def _update_preview(self) -> None:
        key    = self._param_combo.currentData() or "?"
        start  = self._start_sb.value()
        step   = self._step_sb.value()
        cols   = self._cols_spin.value()
        rows   = self._rows_spin.value()
        total  = cols * rows

        sample = min(total, 6)
        vals   = [f"{start + i * step:.3g} µm" for i in range(sample)]
        ellipsis = "  …" if total > 6 else ""

        self._preview_lbl.setText(
            f"  {cols} col{'s' if cols != 1 else ''}  ×  "
            f"{rows} row{'s' if rows != 1 else ''}  =  {total} copies"
            f"  {key}:  {',  '.join(vals)}{ellipsis}"
            f"  Gap: {self._gap_spin.value():.3g} µm  (edge-to-edge, X and Y)"
        )

    # ── Generate ───────────────────────────────────────────────────────────────

    def _do_sweep(self) -> None:
        from core.cell_library import place_cell
        from core.commands import PlaceCellCommand, BatchCommand
        from core.model import Point

        key   = self._param_combo.currentData()
        start = self._start_sb.value()
        step  = self._step_sb.value()
        cols  = self._cols_spin.value()
        rows  = self._rows_spin.value()
        gap   = int(round(self._gap_spin.value() * 1000))   # µm → DBU

        total = cols * rows
        if step == 0 and total > 1:
            reply = QMessageBox.question(
                self, "Zero Step",
                "Step size is zero — all copies will be identical. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Anchor: top-left corner of the original group's bbox
        bb     = self._group.bbox_from(self._design.components)
        anchor = Point(bb.x_min, bb.y_min)

        # Build ONE reference result at the start value to get a stable bbox
        # for column-spacing.  This is what col-0 / row-0 looks like.
        ref_params = dict(self._current_params)
        ref_params[key] = start
        try:
            ref_result = place_cell(self._group.cell_id, Point(0, 0), params=ref_params)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Sweep", f"Cannot build reference cell: {exc}")
            return
        # CellResult has no .bbox — derive it from the union of component bboxes
        def _cell_bbox(result):
            from core.model import BBox
            xs = [c.bbox.x_min for c in result.components] +                  [c.bbox.x_max for c in result.components]
            ys = [c.bbox.y_min for c in result.components] +                  [c.bbox.y_max for c in result.components]
            return BBox(min(xs), min(ys), max(xs), max(ys))

        ref_bb = _cell_bbox(ref_result)

        cmds = []
        for row_i in range(rows):
            for col_i in range(cols):
                linear_idx = row_i * cols + col_i
                val        = start + linear_idx * step

                params = dict(self._current_params)
                params[key] = val

                # ── X offset: column-only index for consistent vertical alignment
                # Build a temp result at (0,0) to get the actual bbox of each
                # column's reference copy (col 0 at each step value).
                # Using the reference bbox for all columns keeps the grid
                # rectangular when the parameter doesn't affect width.
                dx = 0
                for prev_col in range(col_i):
                    prev_val = start + prev_col * step
                    prev_params = dict(self._current_params)
                    prev_params[key] = prev_val
                    try:
                        prev_result = place_cell(
                            self._group.cell_id, Point(0, 0), params=prev_params
                        )
                        prev_bb2 = _cell_bbox(prev_result)
                        prev_w = prev_bb2.x_max - prev_bb2.x_min
                    except Exception:
                        prev_w = ref_bb.x_max - ref_bb.x_min
                    dx += prev_w + gap

                # ── Y offset: tallest copy in each preceding row
                dy = 0
                for prev_row in range(row_i):
                    row_hs = []
                    for c in range(cols):
                        li = prev_row * cols + c
                        pv = start + li * step
                        pp = dict(self._current_params)
                        pp[key] = pv
                        try:
                            pr = place_cell(
                                self._group.cell_id, Point(0, 0), params=pp
                            )
                            pr_bb = _cell_bbox(pr)
                            row_hs.append(pr_bb.y_max - pr_bb.y_min)
                        except Exception:
                            row_hs.append(ref_bb.y_max - ref_bb.y_min)
                    dy += max(row_hs) + gap

                origin = Point(anchor.x + dx, anchor.y + dy)

                try:
                    result = place_cell(self._group.cell_id, origin, params=params)
                except (KeyError, ValueError) as exc:
                    QMessageBox.warning(self, "Sweep", f"Step [{row_i},{col_i}]: {exc}")
                    return

                cmds.append(PlaceCellCommand(
                    result, cell_id=self._group.cell_id, cell_params=params
                ))

        if cmds:
            self._scene.cmd_stack.execute(
                BatchCommand(
                    cmds,
                    f"Cell array {cols}×{rows}  sweep {self._cdef.name}.{key} "
                    f"[{start:.3g}…{start + (total-1)*step:.3g}] µm",
                )
            )
        self.accept()