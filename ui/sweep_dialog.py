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

                dx = 0
                for prev_col in range(col_i):
                    prev_linear = row_i * cols + prev_col
                    prev_val    = p.to_dbu(start + prev_linear * step)
                    prev_w      = prev_val if p.key == "width" else orig_w
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


# ── GroupSweepDialog — sweep a parameter within a duplicated group ─────────────

class GroupSweepDialog(QDialog):
    """
    M×N array sweep over an entire ComponentGroup.

    The user picks:
      1. Which member component to vary (the "target member").
      2. Which parameter of that member to sweep.
      3. The start value, step, array dimensions, and spacing (same as SweepDialog).

    For each array slot (except [0,0] which is the original):
      - Every member of the group is deep-copied with a fresh ID.
      - The whole group of copies is translated to its grid position.
      - The swept parameter is applied only to the copy of the target member.
      - A new ComponentGroup record is created wrapping all the copies.
      - All AddComponent commands + one GroupComponents command are batched
        into a single undo-able unit.

    Layout offset is based on the group's bounding box, updated per-row when
    the swept parameter affects height, so rows never overlap.
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

        self.setWindowTitle("Group Array / Parameter Sweep")
        self.setMinimumWidth(460)
        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")
        self._build_ui()
        self._on_target_changed()   # seed parameter combo + preview

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        # Header
        title = QLabel("Group Array / Parameter Sweep")
        title.setStyleSheet(f"color: {_TEXT}; font-size: 15px; font-weight: bold;")
        root.addWidget(title)

        sub = _label(
            f"Group  '{self._group.name}'  ·  {len(self._members)} members  ·  id {self._group.id}",
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

        # Target member picker
        self._target_combo = QComboBox()
        self._target_combo.setFixedWidth(200)
        self._target_combo.setStyleSheet(_combo_style())
        for comp in self._members:
            label = f"{comp.kind.name.capitalize()}  ·  {comp.id}  (layer {comp.layer})"
            self._target_combo.addItem(label, comp.id)
        row(5, "Target member", self._target_combo)

        # Parameter picker (repopulated when target changes)
        self._param_combo = QComboBox()
        self._param_combo.setFixedWidth(200)
        self._param_combo.setStyleSheet(_combo_style())
        row(6, "Parameter", self._param_combo)

        # Start / step — double and int variants
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _target_comp(self) -> GDSComponent:
        cid = self._target_combo.currentData()
        return self._design.get(cid)

    def _current_param(self) -> _Param:
        return _ALL_PARAMS[self._param_combo.currentData()]

    def _on_target_changed(self) -> None:
        """Repopulate the parameter combo when the target member changes."""
        comp = self._target_comp()
        if comp is None:
            return

        self._param_combo.blockSignals(True)
        self._param_combo.clear()
        for key in _KIND_PARAMS.get(comp.kind, []):
            self._param_combo.addItem(_ALL_PARAMS[key].label, key)
        self._param_combo.blockSignals(False)

        self._on_param_changed()

    def _on_param_changed(self) -> None:
        if self._param_combo.count() == 0:
            return
        p      = self._current_param()
        is_int = p.is_int
        comp   = self._target_comp()

        self._start_dspin.setVisible(not is_int)
        self._start_ispin.setVisible(is_int)
        self._step_dspin.setVisible(not is_int)
        self._step_ispin.setVisible(is_int)

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
        p = self._current_param()
        return float(self._start_ispin.value() if p.is_int else self._start_dspin.value())

    def _get_step(self) -> float:
        p = self._current_param()
        return float(self._step_ispin.value() if p.is_int else self._step_dspin.value())

    def _update_preview(self) -> None:
        if self._param_combo.count() == 0:
            return
        p      = self._current_param()
        start  = self._get_start()
        step   = self._get_step()
        cols   = self._cols_spin.value()
        rows   = self._rows_spin.value()
        total  = cols * rows
        unit   = f" {p.unit}" if p.unit else ""
        comp   = self._target_comp()
        cname  = f"{comp.kind.name.capitalize()} {comp.id}" if comp else "?"

        sample_count = min(total, 6)
        vals = [f"{start + i * step:.3g}{unit}" for i in range(sample_count)]
        ellipsis = "  …" if total > 6 else ""

        self._preview_lbl.setText(
            f"  {cols} col{'s' if cols != 1 else ''}  ×  "
            f"{rows} row{'s' if rows != 1 else ''}  =  {total} group copies\n"
            f"  Sweep  {p.label}  of  {cname}:  {',  '.join(vals)}{ellipsis}\n"
            f"  Gap: {self._gap_spin.value():.3g} µm  between group bboxes (X and Y)"
        )

    # ── Accept — generate the array ───────────────────────────────────────────

    def _on_accept(self) -> None:
        if self._param_combo.count() == 0:
            QMessageBox.warning(self, "Sweep", "No sweepable parameter available.")
            return

        p          = self._current_param()
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

        # Group bounding box in DBU — used for grid spacing
        grp_bb  = self._group.bbox_from(self._design.components)
        grp_w   = grp_bb.x_max - grp_bb.x_min   # full group width
        grp_h   = grp_bb.y_max - grp_bb.y_min   # full group height
        grp_ox  = grp_bb.x_min                   # group origin X
        grp_oy  = grp_bb.y_min                   # group origin Y

        all_cmds = []

        for row_i in range(rows):
            for col_i in range(cols):
                if row_i == 0 and col_i == 0:
                    continue   # original group stays in place

                linear_idx = row_i * cols + col_i
                val_dbu    = p.to_dbu(start + linear_idx * step)

                # ── Work out the effective width/height of this copy ──────────
                # If we're sweeping width/height of the target member, the group
                # bbox in that axis grows by (new_val - original_val).
                target_comp = self._design.get(target_cid)
                orig_target_val = getattr(target_comp, p.key, 0) or 0

                if p.key == "width":
                    copy_grp_w = grp_w + (val_dbu - orig_target_val)
                    copy_grp_h = grp_h
                elif p.key == "height":
                    copy_grp_w = grp_w
                    copy_grp_h = grp_h + (val_dbu - orig_target_val)
                else:
                    copy_grp_w = grp_w
                    copy_grp_h = grp_h

                # ── X offset: sum of widths of all copies to the left ─────────
                dx = 0
                for prev_col in range(col_i):
                    prev_linear = row_i * cols + prev_col
                    prev_val    = p.to_dbu(start + prev_linear * step)
                    if p.key == "width":
                        prev_grp_w = grp_w + (prev_val - orig_target_val)
                    else:
                        prev_grp_w = grp_w
                    dx += prev_grp_w + gap

                # ── Y offset: sum of tallest copy in each row above ───────────
                dy = 0
                for prev_row in range(row_i):
                    row_hs = []
                    for c in range(cols):
                        li  = prev_row * cols + c
                        val = p.to_dbu(start + li * step)
                        if p.key == "height":
                            row_hs.append(grp_h + (val - orig_target_val))
                        else:
                            row_hs.append(grp_h)
                    dy += max(row_hs) + gap

                # ── Deep-copy every member, translate, apply sweep ────────────
                id_map: dict[str, str] = {}   # old_id → new_id
                cloned_comps: List[GDSComponent] = []

                for member in self._members:
                    clone       = copy.deepcopy(member)
                    clone.id    = uuid.uuid4().hex[:8]
                    clone.ports = []

                    # Translate to grid position
                    clone.origin = Point(member.origin.x + dx, member.origin.y + dy)
                    if clone.points:
                        clone.points = [
                            Point(pt.x + dx, pt.y + dy) for pt in clone.points
                        ]

                    # Apply the swept value only to the designated target member
                    if member.id == target_cid:
                        setattr(clone, p.key, val_dbu)

                    id_map[member.id] = clone.id
                    cloned_comps.append(clone)
                    all_cmds.append(AddComponent(clone))

                # ── Register a new group for this copy ───────────────────────
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