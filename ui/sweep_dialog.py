"""
ui/sweep_dialog.py — Parameter sweep + M×N array generation.

Generates an M (columns) × N (rows) array of clones of the selected
component, with one parameter swept linearly across all copies in
row-major order (left→right, top→bottom).

    value[col, row] = start + (row * M + col) * step

Spacing is uniform: the same gap is applied in both X and Y,
measured from bounding-box edge to bounding-box edge.
"""

from __future__ import annotations

import copy
import uuid

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QSpinBox, QDialogButtonBox, QFrame,
    QWidget, QGridLayout, QMessageBox,
)
from PyQt6.QtCore import Qt

from core.model import (
    GDSComponent, ComponentKind, DesignScene,
    Point, dbu_to_um, um_to_dbu,
)
from core.commands import AddComponent, BatchCommand


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


# ── Parameter descriptors ─────────────────────────────────────────────────────

class _Param:
    def __init__(self, key, label, unit, lo, hi,
                 default_step, is_int=False):
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


# ── Dialog ────────────────────────────────────────────────────────────────────

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
        self._on_param_changed()   # seed spin values for initial param

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        # ── Header ────────────────────────────────────────────────────────────
        title = QLabel("Array / Parameter Sweep")
        title.setStyleSheet(
            f"color: {_TEXT}; font-size: 15px; font-weight: bold;"
        )
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

        # ── Array dimensions ──────────────────────────────────────────────────
        section = QLabel("Array")
        section.setStyleSheet(
            f"color: {_ACCENT}; font-size: 11px; "
            f"letter-spacing: 1px; font-weight: bold;"
        )
        grid.addWidget(section, 0, 0, 1, 3)

        self._cols_spin = _ispin(3, 1, 256)   # M  — columns (X)
        self._rows_spin = _ispin(2, 1, 256)   # N  — rows    (Y)
        row(1, "Columns  (M)", self._cols_spin)
        row(2, "Rows  (N)",    self._rows_spin)

        # Uniform gap
        self._gap_spin = _dspin(5.0, 0, 10_000, 1.0)
        row(3, "Gap  (X and Y)", self._gap_spin)

        # ── Sweep parameter ───────────────────────────────────────────────────
        sweep_section = QLabel("Sweep")
        sweep_section.setStyleSheet(section.styleSheet())
        grid.addWidget(sweep_section, 4, 0, 1, 3)

        self._param_combo = QComboBox()
        self._param_combo.setFixedWidth(130)
        self._param_combo.setStyleSheet(f"""
            QComboBox {{
                background: #0f172a; border: 1px solid {_BORDER};
                border-radius: 4px; color: {_TEXT};
                padding: 4px 8px; font-size: 12px;
            }}
            QComboBox QAbstractItemView {{ background: #0f172a; color: {_TEXT}; }}
        """)
        for key in _KIND_PARAMS.get(self._comp.kind, []):
            self._param_combo.addItem(_ALL_PARAMS[key].label, key)
        row(5, "Parameter", self._param_combo)

        # Start value — double and int variants, only one visible at a time
        self._start_dspin = _dspin(0, -10_000, 10_000)
        self._start_ispin = _ispin(0, 0, 63)
        row(6, "Start value", self._start_dspin)
        grid.addWidget(self._start_ispin, 6, 1)

        # Step size
        self._step_dspin = _dspin(1.0, -10_000, 10_000)
        self._step_ispin = _ispin(1, -63, 63)
        row(7, "Step size", self._step_dspin)
        grid.addWidget(self._step_ispin, 7, 1)

        root.addLayout(grid)
        root.addWidget(_sep())

        # ── Live preview ──────────────────────────────────────────────────────
        self._preview_lbl = _label("", muted=True)
        self._preview_lbl.setWordWrap(True)
        self._preview_lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; "
            f"background: #0f172a; border: 1px solid {_BORDER}; "
            f"border-radius: 4px; padding: 8px;"
        )
        root.addWidget(self._preview_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate Array")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(_btn_style())
        root.addWidget(btns)

        # ── Signal wiring ─────────────────────────────────────────────────────
        self._param_combo.currentIndexChanged.connect(self._on_param_changed)
        for w in (self._cols_spin, self._rows_spin, self._gap_spin,
                  self._start_dspin, self._step_dspin,
                  self._start_ispin, self._step_ispin):
            w.valueChanged.connect(self._update_preview)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _current_param(self) -> _Param:
        return _ALL_PARAMS[self._param_combo.currentData()]

    def _on_param_changed(self) -> None:
        p      = self._current_param()
        is_int = p.is_int

        self._start_dspin.setVisible(not is_int)
        self._start_ispin.setVisible(is_int)
        self._step_dspin.setVisible(not is_int)
        self._step_ispin.setVisible(is_int)

        # Seed start from the component's current value
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
        return float(self._start_ispin.value() if p.is_int
                     else self._start_dspin.value())

    def _get_step(self) -> float:
        p = self._current_param()
        return float(self._step_ispin.value() if p.is_int
                     else self._step_dspin.value())

    def _update_preview(self) -> None:
        p      = self._current_param()
        start  = self._get_start()
        step   = self._get_step()
        cols   = self._cols_spin.value()
        rows   = self._rows_spin.value()
        total  = cols * rows
        unit   = f" {p.unit}" if p.unit else ""

        # Show first few values in row-major order
        sample_count = min(total, 6)
        vals = [
            f"{start + i * step:.3g}{unit}"
            for i in range(sample_count)
        ]
        ellipsis = "  …" if total > 6 else ""

        self._preview_lbl.setText(
            f"  {cols} col{'s' if cols != 1 else ''}  ×  "
            f"{rows} row{'s' if rows != 1 else ''}  =  {total} copies\n"
            f"  {p.label}:  {',  '.join(vals)}{ellipsis}\n"
            f"  Gap: {self._gap_spin.value():.3g} µm  (edge-to-edge, uniform X and Y)"
        )

    # ── Accept ────────────────────────────────────────────────────────────────

    def _on_accept(self) -> None:
        p     = self._current_param()
        start = self._get_start()
        step  = self._get_step()
        cols  = self._cols_spin.value()    # M
        rows  = self._rows_spin.value()    # N
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
            # Accumulate Y offset row by row, based on the tallest cell in that row
            # For a height sweep: each row has a different height, use that row's value
            row_linear_base = row_i * cols

            for col_i in range(cols):
                if row_i == 0 and col_i == 0:
                    continue

                linear_idx = row_i * cols + col_i
                val_dbu    = p.to_dbu(start + linear_idx * step)

                # Actual dimensions of THIS clone after sweep applied
                clone_w = val_dbu if p.key == "width"  else orig_w
                clone_h = val_dbu if p.key == "height" else orig_h

                # X offset: sum of widths of all clones to the left in this row
                dx = 0
                for prev_col in range(col_i):
                    prev_linear = row_i * cols + prev_col
                    prev_val    = p.to_dbu(start + prev_linear * step)
                    prev_w      = prev_val if p.key == "width" else orig_w
                    dx += prev_w + gap

                # Y offset: sum of tallest clone in each row above
                dy = 0
                for prev_row in range(row_i):
                    # Tallest clone in prev_row is the one with the largest height value
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