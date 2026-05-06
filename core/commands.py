"""
core/commands.py — Command pattern for undo/redo.

Every user action that mutates the model is wrapped in a Command.
CommandStack owns the undo/redo stacks and calls on_change after each mutation.

Phase 1 commands:  AddComponent, RemoveComponent, MoveComponent
Phase 2 additions: EditComponent (change layer, width, height, path_width)
                   SetPolygonPoints (replace vertex list after vertex edit)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional

from core.model import DesignScene, GDSComponent, Point, Connection, ComponentGroup
from core.cell_library import CellResult, PortSide

# Fields that EditComponent is allowed to mutate.
# A typo in a key name silently creates a new attribute on the dataclass,
# which is a silent data-corruption bug — validate against this set instead.
_EDITABLE_FIELDS = frozenset({"layer", "width", "height", "path_width", "origin", "points"})


# ── Base ──────────────────────────────────────────────────────────────────────

class Command(ABC):
    @abstractmethod
    def execute(self, design: DesignScene) -> None: ...

    @abstractmethod
    def undo(self, design: DesignScene) -> None: ...

    @property
    @abstractmethod
    def description(self) -> str: ...


# ── Concrete commands ─────────────────────────────────────────────────────────

class AddComponent(Command):
    def __init__(self, comp: GDSComponent) -> None:
        self._comp = comp

    def execute(self, design: DesignScene) -> None:
        if not self._comp.ports and not getattr(self._comp, "_no_auto_ports", False):
            self._comp.build_default_ports()
        design.add(self._comp)

    def undo(self, design: DesignScene) -> None:
        design.remove(self._comp.id)

    @property
    def description(self) -> str:
        return f"Add {self._comp.kind.name.lower()} on layer {self._comp.layer}"


class RemoveComponent(Command):
    def __init__(self, comp: GDSComponent) -> None:
        self._comp = comp

    def execute(self, design: DesignScene) -> None:
        design.remove(self._comp.id)

    def undo(self, design: DesignScene) -> None:
        design.add(self._comp)

    @property
    def description(self) -> str:
        return f"Delete {self._comp.kind.name.lower()}"


class MoveComponent(Command):
    def __init__(self, comp_id: str, old_origin: Point, new_origin: Point) -> None:
        self._comp_id   = comp_id
        self._old_origin = old_origin
        self._new_origin = new_origin

    def execute(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp:
            dx = self._new_origin.x - comp.origin.x
            dy = self._new_origin.y - comp.origin.y
            comp.move_by(dx, dy)

    def undo(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp:
            dx = self._old_origin.x - comp.origin.x
            dy = self._old_origin.y - comp.origin.y
            comp.move_by(dx, dy)

    @property
    def description(self) -> str:
        return "Move component"


class EditComponent(Command):
    """
    Phase 2: mutate a component's properties (layer, dimensions).
    Stores a snapshot of all mutable fields for clean undo.
    """

    def __init__(self, comp: GDSComponent, **new_values) -> None:
        unknown = set(new_values) - _EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"EditComponent: unknown field(s) {unknown}. "
                             f"Allowed: {_EDITABLE_FIELDS}")
        self._comp_id = comp.id
        self._new     = new_values
        # Snapshot current state for undo
        self._old = {k: getattr(comp, k) for k in new_values}

    def execute(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp:
            for k, v in self._new.items():
                setattr(comp, k, v)
            design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp:
            for k, v in self._old.items():
                setattr(comp, k, v)
            design.is_dirty = True

    @property
    def description(self) -> str:
        keys = ", ".join(self._new.keys())
        return f"Edit {keys}"


class SetPolygonPoints(Command):
    """Phase 2: replace the vertex list of a polygon/path after interactive editing."""

    def __init__(self, comp: GDSComponent, new_points: List[Point]) -> None:
        self._comp_id   = comp.id
        self._new_points = list(new_points)
        self._old_points = list(comp.points) if comp.points else []
        self._old_origin = Point(comp.origin.x, comp.origin.y)

    def execute(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp and self._new_points:
            comp.points = list(self._new_points)
            comp.origin = self._new_points[0]
            design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if comp:
            comp.points = list(self._old_points)
            comp.origin = self._old_origin
            design.is_dirty = True

    @property
    def description(self) -> str:
        return "Edit polygon vertices"


class ConnectPorts(Command):
    """Create a connection between two (component, port) pairs."""

    def __init__(self, comp_a_id: str, port_a_id: str,
                 comp_b_id: str, port_b_id: str) -> None:
        self._comp_a = comp_a_id
        self._port_a = port_a_id
        self._comp_b = comp_b_id
        self._port_b = port_b_id
        self._conn_id: Optional[str] = None

    def execute(self, design: DesignScene) -> None:
        conn = design.connect(self._comp_a, self._port_a,
                              self._comp_b, self._port_b)
        self._conn_id = conn.id
        design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        if self._conn_id:
            design.disconnect(self._conn_id)

    @property
    def description(self) -> str:
        return "Connect ports"


class DisconnectPorts(Command):
    """Remove an existing connection."""

    def __init__(self, connection: Connection) -> None:
        self._snap = Connection(
            comp_a=connection.comp_a, port_a=connection.port_a,
            comp_b=connection.comp_b, port_b=connection.port_b,
            id=connection.id,
        )

    def execute(self, design: DesignScene) -> None:
        design.disconnect(self._snap.id)

    def undo(self, design: DesignScene) -> None:
        design.connect(self._snap.comp_a, self._snap.port_a,
                       self._snap.comp_b, self._snap.port_b)

    @property
    def description(self) -> str:
        return "Disconnect ports"

class BatchCommand(Command):
    """Execute multiple commands as a single undo/redo unit."""

    def __init__(self, commands: List[Command], label: str) -> None:
        self._commands = commands
        self._label    = label

    def execute(self, design: DesignScene) -> None:
        for cmd in self._commands:
            cmd.execute(design)

    def undo(self, design: DesignScene) -> None:
        for cmd in reversed(self._commands):
            cmd.undo(design)

    @property
    def description(self) -> str:
        return self._label
    
class GroupComponents(Command):
    """
    Collect existing components into a named ComponentGroup.
    Components stay in the scene — only a group record is added.
    """

    def __init__(self, comp_ids: List[str], name: str) -> None:
        from core.model import ComponentGroup
        self._group = ComponentGroup(name=name, member_ids=list(comp_ids))

    def execute(self, design: DesignScene) -> None:
        design.add_group(self._group)

    def undo(self, design: DesignScene) -> None:
        design.remove_group(self._group.id)

    @property
    def description(self) -> str:
        return f"Group '{self._group.name}'"


class UngroupComponents(Command):
    """Dissolve a group back to independent components."""

    def __init__(self, group: "ComponentGroup") -> None:
        self._group = ComponentGroup(
            name=group.name,
            member_ids=list(group.member_ids),
            id=group.id,
        )

    def execute(self, design: DesignScene) -> None:
        design.remove_group(self._group.id)

    def undo(self, design: DesignScene) -> None:
        design.add_group(self._group)

    @property
    def description(self) -> str:
        return f"Ungroup '{self._group.name}'"


class MergeGroups(Command):
    """
    Flatten N groups (and optionally loose components) into one new group.

    No nesting: every source group is dissolved and all their member_ids
    — plus any extra loose component IDs — are collected into a single
    flat ComponentGroup.  The operation is fully reversible: undo re-creates
    each source group with its original name/id and removes the merged group.

    Parameters
    ----------
    source_groups : list[ComponentGroup]
        Existing groups to dissolve.  Snapshots are taken at construction time.
    extra_comp_ids : list[str]
        IDs of individually-selected components that are not already in any
        source group (can be empty).
    name : str
        Display name for the new merged group.
    """

    def __init__(
        self,
        source_groups: List["ComponentGroup"],
        extra_comp_ids: List[str],
        name: str,
    ) -> None:
        # Snapshot source groups so undo can recreate them exactly, preserving
        # all dynamic attrs (cell_id, _cell_params) so undo restores parametric
        # cell metadata and the sweep dialog can recover it after undo.
        self._source_snapshots: List[ComponentGroup] = []
        for g in source_groups:
            snap = ComponentGroup(name=g.name, member_ids=list(g.member_ids), id=g.id)
            for attr in ("cell_id", "_cell_params"):
                if hasattr(g, attr):
                    setattr(snap, attr, getattr(g, attr))
            self._source_snapshots.append(snap)

        # Flat union of all member IDs (preserves order, deduplicates)
        seen: set = set()
        flat_ids: List[str] = []
        for g in source_groups:
            for cid in g.member_ids:
                if cid not in seen:
                    seen.add(cid)
                    flat_ids.append(cid)
        for cid in extra_comp_ids:
            if cid not in seen:
                seen.add(cid)
                flat_ids.append(cid)

        self._merged = ComponentGroup(name=name, member_ids=flat_ids)

        # Store cell sub-group descriptors so GroupSweepDialog can expose
        # cell-level parameters (square_x, lead_width, …) rather than raw
        # component fields (width/height/layer) when the merged group contains
        # parametric cells.
        #
        # _cell_subgroups is a list of dicts, one per source group (cell OR
        # plain regular group) plus one extra "loose" entry for any
        # extra_comp_ids that weren't in any source group.  Each entry has:
        #   "name"        — display name (e.g. "ByiskJJ (2.0×2.0µm)")
        #   "cell_id"     — catalogue key, or None for plain/regular groups
        #   "cell_params" — dict of µm-space parameter values (empty for plain)
        #   "member_ids"  — list of component IDs that belong to this sub-group
        #
        # Non-cell entries (cell_id=None) are treated as passthrough in the
        # sweep loop — they are deep-copied and translated but never rebuilt
        # via place_cell().  This ensures regular items in a mixed merge group
        # are not silently dropped during a sweep.
        subgroups: list = []
        for snap in self._source_snapshots:
            subgroups.append({
                "name":        snap.name,
                "cell_id":     getattr(snap, "cell_id", None),
                "cell_params": dict(getattr(snap, "_cell_params", {})),
                "member_ids":  list(snap.member_ids),
            })
        # Loose components that were not part of any source group
        if extra_comp_ids:
            already_covered = {cid for snap in self._source_snapshots
                               for cid in snap.member_ids}
            loose = [cid for cid in extra_comp_ids if cid not in already_covered]
            if loose:
                subgroups.append({
                    "name":        "(loose)",
                    "cell_id":     None,
                    "cell_params": {},
                    "member_ids":  loose,
                })
        self._merged._cell_subgroups = subgroups

    def execute(self, design: DesignScene) -> None:
        # Dissolve every source group
        for snap in self._source_snapshots:
            design.remove_group(snap.id)
        # Add the flat merged group (carries _cell_subgroups set in __init__)
        design.add_group(self._merged)

    def undo(self, design: DesignScene) -> None:
        # Remove the merged group
        design.remove_group(self._merged.id)
        # Recreate each source group with its original id and membership
        for snap in self._source_snapshots:
            design.add_group(snap)

    @property
    def description(self) -> str:
        n = len(self._source_snapshots)
        return f"Merge {n} group{'s' if n != 1 else ''} → '{self._merged.name}'"


class MoveGroup(Command):
    """Translate all members of a group by (dx, dy)."""

    def __init__(self, group_id: str, dx: int, dy: int) -> None:
        self._group_id = group_id
        self._dx = dx
        self._dy = dy

    def execute(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group:
            for cid in group.member_ids:
                comp = design.get(cid)
                if comp:
                    comp.move_by(self._dx, self._dy)
            design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group:
            for cid in group.member_ids:
                comp = design.get(cid)
                if comp:
                    comp.move_by(-self._dx, -self._dy)
            design.is_dirty = True

    @property
    def description(self) -> str:
        return "Move group"


# ── Rotation helpers ──────────────────────────────────────────────────────────

def _rotate_point(px: int, py: int,
                  cx: int, cy: int,
                  steps: int) -> tuple[int, int]:
    """
    Rotate point (px, py) around centre (cx, cy) by `steps` × 90° CCW.
    steps=1 → 90° CCW, steps=-1 → 90° CW (equivalent to 270° CCW).
    Pure integer arithmetic — no floating point, no rounding drift.
    """
    steps = steps % 4
    dx, dy = px - cx, py - cy
    for _ in range(steps):
        dx, dy = -dy, dx          # 90° CCW: (dx, dy) → (−dy, dx)
    return cx + dx, cy + dy


def _rotate_port_side(side: "PortSide", steps: int) -> "PortSide":
    """
    Rotate a PortSide by `steps` × 90° CCW.

    Rotation order (CCW): NORTH → WEST → SOUTH → EAST → NORTH
    One step CCW:  N→W, W→S, S→E, E→N
    """
    from core.model import PortSide
    ccw_cycle = [PortSide.NORTH, PortSide.WEST, PortSide.SOUTH, PortSide.EAST]
    idx = ccw_cycle.index(side)
    return ccw_cycle[(idx + steps) % 4]


def _rotate_component_in_place(comp: "GDSComponent",
                                cx: int, cy: int,
                                steps: int) -> None:
    """
    Bake a rotation into comp's coordinates around world centre (cx, cy).

    Rectangles are converted to polygons (4 vertices) on the first rotation
    because a rotated axis-aligned rectangle is no longer axis-aligned.
    Polygons and paths simply rotate every vertex.
    steps: +1 = 90° CCW, -1 = 90° CW.

    Port fix: port offsets are vectors relative to comp.origin in local space.
    When the component rotates, those offset vectors must rotate by the same
    steps around (0, 0) in local space.  Port sides (NORTH/SOUTH/EAST/WEST)
    rotate by the same steps.  Port IDs are preserved — no Connection breakage.
    """
    from core.model import ComponentKind, Point

    if comp.kind == ComponentKind.RECTANGLE:
        # Explode to 4 explicit corners, then rotate to a polygon
        x0, y0 = comp.origin.x, comp.origin.y
        x1, y1 = x0 + comp.width, y0 + comp.height
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        rotated = [_rotate_point(px, py, cx, cy, steps) for px, py in corners]
        # Re-use the component as a POLYGON — keeps the same id / ports intact
        comp.kind   = ComponentKind.POLYGON
        comp.points = [Point(rx, ry) for rx, ry in rotated]
        comp.origin = comp.points[0]
        comp.width  = 0
        comp.height = 0
    else:
        # POLYGON or PATH — rotate every vertex
        pts = comp.points or []
        comp.points = [
            Point(*_rotate_point(p.x, p.y, cx, cy, steps)) for p in pts
        ]
        if comp.points:
            comp.origin = comp.points[0]

    # ── Rotate port offsets and sides ─────────────────────────────────────────
    # A port's absolute world position before rotation is:
    #   abs_old = old_origin + offset
    # After rotating the whole component around (cx, cy) by `steps`:
    #   abs_new = rotate(abs_old, cx, cy, steps)
    # The new local offset must be:
    #   new_offset = abs_new - new_origin
    #
    # At this point comp.origin is already new_origin (set by the geometry
    # block above).  Recover old_origin by applying the inverse rotation:
    #   old_origin = rotate(new_origin, cx, cy, -steps)
    new_ox, new_oy = comp.origin.x, comp.origin.y
    old_ox, old_oy = _rotate_point(new_ox, new_oy, cx, cy, (4 - steps) % 4)
    for port in comp.ports:
        abs_old_x = old_ox + port.offset.x
        abs_old_y = old_oy + port.offset.y
        abs_new_x, abs_new_y = _rotate_point(abs_old_x, abs_old_y, cx, cy, steps)
        port.offset = Point(abs_new_x - new_ox, abs_new_y - new_oy)
        port.side   = _rotate_port_side(port.side, steps)


class RotateComponent(Command):
    """
    Rotate one component by `steps` × 90° around its own bounding-box centre.

    steps = +1  →  90° CCW
    steps = -1  →  90° CW   (stored as 3 so undo is symmetric)

    Rotation is baked into the DBU coordinates — no Qt transform is used.
    Rectangles are promoted to polygons on first rotation (axis-aligned
    rect is no longer representable as a rect after a 90° turn unless it
    is perfectly square, and even then we keep it as a polygon for simplicity
    so the model stays consistent).

    Undo: rotate by (4 - steps) which is the complementary turn back to 0°.
    """

    def __init__(self, comp: "GDSComponent", steps: int = 1) -> None:
        from core.model import ComponentKind
        self._comp_id = comp.id
        self._steps   = steps % 4      # normalise to 0-3
        self._undo_steps = (4 - self._steps) % 4
        # Snapshot the FULL component state for a clean undo
        # (kind may change rect→polygon, so we must snapshot it)
        self._snap_kind   = comp.kind
        self._snap_origin = Point(comp.origin.x, comp.origin.y)
        self._snap_width  = comp.width
        self._snap_height = comp.height
        self._snap_points = list(comp.points) if comp.points else None
        # Snapshot port offsets+sides — _rotate_component_in_place mutates them
        # in-place, so undo must restore them rather than rotate back.
        self._snap_ports  = [
            (p.id, Point(p.offset.x, p.offset.y), p.side)
            for p in comp.ports
        ]

    def _centre_of(self, comp: "GDSComponent") -> tuple[int, int]:
        bb = comp.bbox
        return (bb.x_min + bb.x_max) // 2, (bb.y_min + bb.y_max) // 2

    def execute(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if not comp or self._steps == 0:
            return
        cx, cy = self._centre_of(comp)
        _rotate_component_in_place(comp, cx, cy, self._steps)
        design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        comp = design.get(self._comp_id)
        if not comp:
            return
        # Restore the exact snapshot — simpler and safer than rotating back
        comp.kind   = self._snap_kind
        comp.origin = Point(self._snap_origin.x, self._snap_origin.y)
        comp.width  = self._snap_width
        comp.height = self._snap_height
        comp.points = list(self._snap_points) if self._snap_points is not None else None
        # Restore port offsets and sides from the pre-rotation snapshot
        port_map = {p.id: p for p in comp.ports}
        for port_id, offset, side in self._snap_ports:
            if port_id in port_map:
                port_map[port_id].offset = Point(offset.x, offset.y)
                port_map[port_id].side   = side
        design.is_dirty = True

    @property
    def description(self) -> str:
        deg = self._steps * 90
        return f"Rotate component {deg}° CCW"


class RotateGroup(Command):
    """
    Rotate all members of a group by `steps` × 90° CCW around the group's
    bounding-box centre.

    All members rotate around the SAME centre (the group bbox centre), so
    the group retains its overall shape — members don't spin individually.

    Undo: restore every member's exact coordinate snapshot taken at construction.
    """

    def __init__(self, group: "ComponentGroup",
                 components: List["GDSComponent"],
                 steps: int = 1) -> None:
        self._group_id = group.id
        self._steps    = steps % 4
        # Compute the group bbox centre now (before any rotation)
        if components:
            x_min = min(c.bbox.x_min for c in components)
            y_min = min(c.bbox.y_min for c in components)
            x_max = max(c.bbox.x_max for c in components)
            y_max = max(c.bbox.y_max for c in components)
            self._cx = (x_min + x_max) // 2
            self._cy = (y_min + y_max) // 2
        else:
            self._cx = self._cy = 0

        # Deep snapshot every member for clean undo (kind may change)
        self._snaps: List[dict] = []
        for comp in components:
            self._snaps.append({
                "id":     comp.id,
                "kind":   comp.kind,
                "origin": Point(comp.origin.x, comp.origin.y),
                "width":  comp.width,
                "height": comp.height,
                "points": list(comp.points) if comp.points else None,
                # Snapshot port offsets+sides — rotated in-place, must restore on undo
                "ports":  [(p.id, Point(p.offset.x, p.offset.y), p.side)
                           for p in comp.ports],
            })

    def execute(self, design: DesignScene) -> None:
        if self._steps == 0:
            return
        group = design.get_group(self._group_id)
        if not group:
            return
        for cid in group.member_ids:
            comp = design.get(cid)
            if comp:
                _rotate_component_in_place(comp, self._cx, self._cy, self._steps)
        design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        for snap in self._snaps:
            comp = design.get(snap["id"])
            if comp:
                comp.kind   = snap["kind"]
                comp.origin = Point(snap["origin"].x, snap["origin"].y)
                comp.width  = snap["width"]
                comp.height = snap["height"]
                comp.points = list(snap["points"]) if snap["points"] is not None else None
                # Restore port offsets and sides from pre-rotation snapshot
                port_map = {p.id: p for p in comp.ports}
                for port_id, offset, side in snap["ports"]:
                    if port_id in port_map:
                        port_map[port_id].offset = Point(offset.x, offset.y)
                        port_map[port_id].side   = side
        design.is_dirty = True

    @property
    def description(self) -> str:
        deg = self._steps * 90
        return f"Rotate group {deg}° CCW"


# ── Cell library commands ─────────────────────────────────────────────────────

class PlaceCellCommand(Command):
    """
    Atomically add all components from *result* and create a ComponentGroup
    wrapping them under *result.group_name*.

    execute : adds all components → builds group
    undo    : removes group → removes all components (reverse order)

    The group has two extra attributes set after execute():
      group.cell_id    — cell_id string from the catalogue (for properties panel)
      group._cell_params — copy of the params used to build this cell
    These are plain Python attrs added dynamically; they survive in memory but
    are not persisted (the group_name already encodes the key dimensions).
    """

    def __init__(self, result: CellResult, cell_id: str = "",
                 cell_params: dict | None = None) -> None:
        self._result      = result
        self._cell_id     = cell_id
        self._cell_params = dict(cell_params) if cell_params else {}
        self._comp_ids    = [c.id for c in result.components]
        self._group: Optional[ComponentGroup] = None

    def execute(self, design: DesignScene) -> None:
        for comp in self._result.components:
            design.add(comp)
        self._group = ComponentGroup(
            name=self._result.group_name,
            member_ids=list(self._comp_ids),
        )
        # Tag the group so the Properties panel can identify and edit this cell
        self._group.cell_id     = self._cell_id
        self._group._cell_params = dict(self._cell_params)
        design.add_group(self._group)

    def undo(self, design: DesignScene) -> None:
        if self._group is not None:
            design.remove_group(self._group.id)
        for comp_id in reversed(self._comp_ids):
            design.remove(comp_id)

    @property
    def description(self) -> str:
        return f"Place {self._result.group_name}"

# ── Paste command ─────────────────────────────────────────────────────────────

class PasteComponents(Command):
    """
    Add a set of pasted components (and optionally a group) to the scene
    as a single, fully undoable action.

    Mirrors PlaceCellCommand in structure:
      execute : add all components → add group (if any)
      undo    : remove group → remove all components (reverse order)

    Port auto-generation is skipped for components that already have ports
    (they were deep-copied from real shapes that had ports built previously).
    Components with no ports and _no_auto_ports=False get fresh default ports.
    """

    def __init__(self, components: list["GDSComponent"],
                 group: Optional["ComponentGroup"] = None) -> None:
        self._components = components
        self._group      = group

    def execute(self, design: DesignScene) -> None:
        for comp in self._components:
            if not comp.ports and not getattr(comp, "_no_auto_ports", False):
                comp.build_default_ports()
            design.add(comp)
        if self._group is not None:
            design.add_group(self._group)

    def undo(self, design: DesignScene) -> None:
        if self._group is not None:
            design.remove_group(self._group.id)
        for comp in reversed(self._components):
            design.remove(comp.id)

    @property
    def description(self) -> str:
        n = len(self._components)
        return f"Paste {n} component{'s' if n != 1 else ''}"


# ── Command Stack ─────────────────────────────────────────────────────────────

class CommandStack:
    """
    Owns the undo/redo stacks.

    Observers register via connect_change(callable) — called after every
    mutation (execute, undo, redo, clear).  Multiple listeners are supported.
    Using a list rather than a single Callable lets Qt slots connect naturally
    without wrapping everything in a lambda.
    """

    def __init__(self, design: DesignScene) -> None:
        self._design      = design
        self._listeners:  List[Callable[[], None]] = []
        self._undo_stack: List[Command] = []
        self._redo_stack: List[Command] = []

    def connect_change(self, listener: Callable[[], None]) -> None:
        """Register a zero-argument callable called after every stack mutation."""
        self._listeners.append(listener)

    def _notify(self) -> None:
        for fn in self._listeners:
            fn()

    def execute(self, cmd: Command) -> None:
        cmd.execute(self._design)
        self._undo_stack.append(cmd)
        self._redo_stack.clear()
        self._notify()

    def undo(self) -> Optional[str]:
        if not self._undo_stack:
            return None
        cmd = self._undo_stack.pop()
        cmd.undo(self._design)
        self._redo_stack.append(cmd)
        self._notify()
        return cmd.description

    def redo(self) -> Optional[str]:
        if not self._redo_stack:
            return None
        cmd = self._redo_stack.pop()
        cmd.execute(self._design)
        self._undo_stack.append(cmd)
        self._notify()
        return cmd.description

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._notify()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @property
    def undo_description(self) -> str:
        return self._undo_stack[-1].description if self._undo_stack else ""

    @property
    def redo_description(self) -> str:
        return self._redo_stack[-1].description if self._redo_stack else ""
    
class RemoveGroup:
    """
    Minimal undo-able command to remove a group record from the design.
    Used by _delete_selected so group deletion is undoable as part of
    a BatchCommand alongside RemoveComponent.
    """
    def __init__(self, group) -> None:
        # Reconstruct explicitly — shallow copy would share the member_ids list
        self._group = ComponentGroup(
            name=group.name,
            member_ids=list(group.member_ids),
            id=group.id,
        )
        # Preserve any dynamic attrs (cell_id, _cell_params) so undo restores
        # parametric cell metadata correctly
        for attr in ("cell_id", "_cell_params"):
            if hasattr(group, attr):
                setattr(self._group, attr, getattr(group, attr))

    def execute(self, design) -> None:
        design.remove_group(self._group.id)

    def undo(self, design) -> None:
        design.add_group(self._group)

    @property
    def description(self) -> str:
        return f"Remove group '{self._group.name}'"
    
class ReplaceCellCmd:
    """Atomic remove-old + place-new, fully undo-able."""
    def __init__(self, design_ref, scene_ref, new_result, cdef, param_key, cell_id, params,
                 old_group_id, old_group_name, old_comp_ids, old_comps):
        self._design = design_ref
        self._scene  = scene_ref
        self.cdef = cdef
        self.param_key = param_key
        self.params = params
        self._old_group_id = old_group_id
        self._old_group_name = old_group_name
        self._old_comp_ids = old_comp_ids
        self._old_comps = old_comps
        self._new_cmd = PlaceCellCommand(new_result, cell_id=cell_id, cell_params=params)

    @property
    def description(self):
        return f"Edit {self.cdef.name} parameter '{self.param_key}'"

    def execute(self, design):
        # Remove old group + members
        design.remove_group(self._old_group_id)
        for cid in self._old_comp_ids:
            design.remove(cid)
        # Place rebuilt cell
        self._new_cmd.execute(design)
        # Tag new group with param overrides
        if self._new_cmd._group is not None:
            self._new_cmd._group._cell_params = self.params

    def undo(self, design):
        # Undo new placement
        self._new_cmd.undo(design)
        # Restore old components
        for comp in self._old_comps:
            design.add(comp)
        old_g = ComponentGroup(
            name=self._old_group_name,
            member_ids=self._old_comp_ids,
            id=self._old_group_id,
        )
        design.add_group(old_g)