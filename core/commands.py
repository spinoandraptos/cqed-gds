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

from core.model import DesignScene, GDSComponent, Point

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