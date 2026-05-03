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
        if not self._comp.ports:
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
        # Snapshot source groups so undo can recreate them exactly
        self._source_snapshots: List[ComponentGroup] = [
            ComponentGroup(name=g.name, member_ids=list(g.member_ids), id=g.id)
            for g in source_groups
        ]
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

    def execute(self, design: DesignScene) -> None:
        # Dissolve every source group
        for snap in self._source_snapshots:
            design.remove_group(snap.id)
        # Add the flat merged group
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