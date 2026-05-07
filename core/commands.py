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

    _cell_subgroups is always populated so the Properties panel can treat
    item+item, cell+item, and cell+cell groups uniformly via one code path.
    Each entry has cell_id=None and empty cell_params for plain items.
    """

    def __init__(self, comp_ids: List[str], name: str) -> None:
        from core.model import ComponentGroup
        self._group = ComponentGroup(name=name, member_ids=list(comp_ids))
        # Each bare component gets its own sub-group entry so the panel can
        # render per-member controls without special-casing this command.
        self._group._cell_subgroups = [
            {
                "name":                f"comp:{cid}",
                "cell_id":             None,
                "cell_params":         {},
                "cell_rotation_steps": 0,
                "member_ids":          [cid],
            }
            for cid in comp_ids
        ]

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
            for attr in ("cell_id", "_cell_params", "_cell_origin",
                         "_cell_rotation_steps", "_cell_subgroups"):
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
                "cell_rotation_steps": getattr(snap, "_cell_rotation_steps", 0),
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
        # Recreate each source group with its original id, membership, AND
        # _cell_subgroups so that parametric cell metadata is fully restored.
        for snap in self._source_snapshots:
            g = ComponentGroup(name=snap.name, member_ids=list(snap.member_ids), id=snap.id)
            for attr in ("cell_id", "_cell_params", "_cell_origin",
                         "_cell_rotation_steps", "_cell_subgroups"):
                if hasattr(snap, attr):
                    setattr(g, attr, getattr(snap, attr))
            design.add_group(g)

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
            # Keep _cell_origin in sync so param edits re-place at the
            # current position, not the original drop position.
            if hasattr(group, "_cell_origin") and group._cell_origin is not None:
                from core.model import Point
                o = group._cell_origin
                group._cell_origin = Point(o.x + self._dx, o.y + self._dy)
            design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group:
            for cid in group.member_ids:
                comp = design.get(cid)
                if comp:
                    comp.move_by(-self._dx, -self._dy)
            if hasattr(group, "_cell_origin") and group._cell_origin is not None:
                from core.model import Point
                o = group._cell_origin
                group._cell_origin = Point(o.x - self._dx, o.y - self._dy)
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
        # Rotate _cell_origin around the same centre so param edits re-place
        # at the correct rotated position.
        if hasattr(group, "_cell_origin") and group._cell_origin is not None:
            nx, ny = _rotate_point(group._cell_origin.x, group._cell_origin.y,
                                   self._cx, self._cy, self._steps)
            group._cell_origin = Point(nx, ny)
        # Track cumulative rotation so ReplaceCellCmd can re-apply it.
        group._cell_rotation_steps = (getattr(group, "_cell_rotation_steps", 0) + self._steps) % 4
        design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group and hasattr(group, "_cell_origin") and group._cell_origin is not None:
            nx, ny = _rotate_point(group._cell_origin.x, group._cell_origin.y,
                                   self._cx, self._cy, -self._steps)
            group._cell_origin = Point(nx, ny)
        if group:
            group._cell_rotation_steps = (getattr(group, "_cell_rotation_steps", 0) - self._steps) % 4
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
                 cell_params: dict | None = None,
                 cell_origin=None) -> None:
        self._result      = result
        self._cell_id     = cell_id
        self._cell_params = dict(cell_params) if cell_params else {}
        # Store the exact origin passed to place_cell so ReplaceCellCmd can
        # re-place at the identical anchor point.  Recomputing from bbox
        # shifts the cell when the builder origin != bbox min-corner.
        self._cell_origin = cell_origin
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
        self._group.cell_id      = self._cell_id
        self._group._cell_params = dict(self._cell_params)
        if self._cell_origin is not None:
            self._group._cell_origin = self._cell_origin
        # _cell_subgroups: single-cell groups get one entry matching the whole
        # group, so the panel can always use the unified _cell_subgroups path.
        self._group._cell_subgroups = [
            {
                "name":                self._result.group_name,
                "cell_id":             self._cell_id,
                "cell_params":         dict(self._cell_params),
                "cell_rotation_steps": 0,
                "member_ids":          list(self._comp_ids),
            }
        ]
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
            # Deep-copy carries dynamic attrs (cell_id, _cell_params,
            # _cell_subgroups) automatically from the Clipboard snapshot;
            # no extra work needed here.
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
        for attr in ("cell_id", "_cell_params", "_cell_origin", "_cell_rotation_steps"):
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
                 old_group_id, old_group_name, old_comp_ids, old_comps, cell_origin=None,
                 rotation_steps=0):
        self._design = design_ref
        self._scene  = scene_ref
        self.cdef = cdef
        self.param_key = param_key
        self.params = params
        self._old_group_id = old_group_id
        self._old_group_name = old_group_name
        self._old_comp_ids = old_comp_ids
        self._old_comps = old_comps
        self._cell_origin = cell_origin
        self._rotation_steps = rotation_steps % 4
        self._new_cmd = PlaceCellCommand(new_result, cell_id=cell_id, cell_params=params,
                                         cell_origin=cell_origin)

    @property
    def description(self):
        return f"Edit {self.cdef.name} parameter '{self.param_key}'"

    def execute(self, design):
        # Snapshot the old anchor position BEFORE removing anything.
        # We use this to pin the new cell to exactly the same spot.
        old_anchor = next(
            (design.get(cid) for cid in self._old_comp_ids if design.get(cid)), None
        )
        old_anchor_origin = Point(old_anchor.origin.x, old_anchor.origin.y)             if old_anchor else None

        # Remove old group + members
        design.remove_group(self._old_group_id)
        for cid in self._old_comp_ids:
            design.remove(cid)

        # Place rebuilt cell at _cell_origin (axis-aligned)
        self._new_cmd.execute(design)
        new_group = self._new_cmd._group
        if new_group is not None:
            new_group._cell_params = self.params
            new_group._cell_rotation_steps = self._rotation_steps

            if self._rotation_steps:
                members = [design.get(cid) for cid in new_group.member_ids
                           if design.get(cid)]
                if members:
                    # Step 1: rotate around origin (0,0) so we can measure
                    # where the anchor component ends up after rotation.
                    new_anchor_before = members[0]
                    pre_ax = new_anchor_before.origin.x
                    pre_ay = new_anchor_before.origin.y

                    # Rotate everything around the cell origin point
                    ox, oy = self._cell_origin.x, self._cell_origin.y
                    for comp in members:
                        _rotate_component_in_place(comp, ox, oy, self._rotation_steps)

                    # Step 2: the anchor has moved to a new position after rotation.
                    # Translate all members so the anchor sits exactly where the
                    # old anchor was — this keeps the cell perfectly in place.
                    if old_anchor_origin is not None:
                        post_ax = members[0].origin.x
                        post_ay = members[0].origin.y
                        dx = old_anchor_origin.x - post_ax
                        dy = old_anchor_origin.y - post_ay
                        if dx or dy:
                            for comp in members:
                                comp.move_by(dx, dy)
                            # Keep _cell_origin consistent with the translation
                            new_group._cell_origin = Point(
                                new_group._cell_origin.x + dx,
                                new_group._cell_origin.y + dy,
                            ) if hasattr(new_group, "_cell_origin") and                                new_group._cell_origin is not None                             else new_group._cell_origin

    def undo(self, design):
        # Undo new placement
        self._new_cmd.undo(design)
        # Restore old components (already have baked-in rotation)
        for comp in self._old_comps:
            design.add(comp)
        old_g = ComponentGroup(
            name=self._old_group_name,
            member_ids=self._old_comp_ids,
            id=self._old_group_id,
        )
        # Restore dynamic attrs
        if self._cell_origin is not None:
            old_g._cell_origin = self._cell_origin
        old_g._cell_rotation_steps = self._rotation_steps
        design.add_group(old_g)

class ReplaceSubgroupCellCmd:
    """
    Replace the components of ONE cell sub-group inside a merged group,
    leaving all other sub-groups' components untouched.

    This is the undo-able counterpart to ReplaceSubgroupCellCmd used when a
    parameter spinbox fires inside a merged/cell+item/cell+cell group.

    execute:
      1. Remove the old sub-group's components from the design.
      2. Add the new components from new_cell_result.
      3. Patch the merged group's member_ids (swap old → new comp IDs).
      4. Patch _cell_subgroups[sg_index] with updated_sg_entry (new IDs + params).

    undo:
      Reverse exactly: remove new components, restore old ones, restore
      member_ids and _cell_subgroups entry to the pre-edit state.
    """

    def __init__(
        self,
        group_id: str,
        sg_index: int,
        old_sg_comp_ids: List[str],
        old_sg_comps: List["GDSComponent"],   # deep-copied snapshots
        new_cell_result: "CellResult",
        updated_sg_entry: dict,               # member_ids=[] — filled in execute
        old_sg_entry: dict,
        old_all_member_ids: List[str],
        cdef_name: str,
        param_key: str,
        new_value,
    ) -> None:
        self._group_id           = group_id
        self._sg_index           = sg_index
        self._old_sg_comp_ids    = list(old_sg_comp_ids)
        self._old_sg_comps       = old_sg_comps
        self._new_result         = new_cell_result
        self._updated_sg_entry   = updated_sg_entry   # mutated in execute
        self._old_sg_entry       = old_sg_entry
        self._old_all_member_ids = list(old_all_member_ids)
        self._cdef_name          = cdef_name
        self._param_key          = param_key
        self._new_value          = new_value
        # New component IDs are determined at execute time
        self._new_comp_ids: List[str] = []

    def execute(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group is None:
            return

        # 1. Remove old sub-group components
        for cid in self._old_sg_comp_ids:
            design.remove(cid)

        # 2. Add new components
        self._new_comp_ids = []
        for comp in self._new_result.components:
            design.add(comp)
            self._new_comp_ids.append(comp.id)

        # 3. Patch the merged group's member_ids: replace old IDs with new IDs
        #    in-place, preserving the order of all other sub-groups' members.
        old_id_set = set(self._old_sg_comp_ids)
        new_members: List[str] = []
        inserted = False
        for cid in self._old_all_member_ids:
            if cid in old_id_set:
                if not inserted:
                    new_members.extend(self._new_comp_ids)
                    inserted = True
                # skip old IDs
            else:
                new_members.append(cid)
        if not inserted:
            new_members.extend(self._new_comp_ids)
        group.member_ids = new_members

        # 4. Update _cell_subgroups entry with new IDs and params
        self._updated_sg_entry["member_ids"] = list(self._new_comp_ids)
        cell_subgroups = getattr(group, "_cell_subgroups", [])
        if 0 <= self._sg_index < len(cell_subgroups):
            cell_subgroups[self._sg_index] = self._updated_sg_entry
        group._cell_subgroups = cell_subgroups

        design.is_dirty = True

    def undo(self, design: DesignScene) -> None:
        group = design.get_group(self._group_id)
        if group is None:
            return

        # Remove new components
        for cid in self._new_comp_ids:
            design.remove(cid)

        # Restore old components
        for comp in self._old_sg_comps:
            design.add(comp)

        # Restore the original flat member_ids list
        group.member_ids = list(self._old_all_member_ids)

        # Restore the original _cell_subgroups entry
        cell_subgroups = getattr(group, "_cell_subgroups", [])
        if 0 <= self._sg_index < len(cell_subgroups):
            cell_subgroups[self._sg_index] = self._old_sg_entry
        group._cell_subgroups = cell_subgroups

        design.is_dirty = True

    @property
    def description(self) -> str:
        return f"Edit {self._cdef_name}: {self._param_key} = {self._new_value}"