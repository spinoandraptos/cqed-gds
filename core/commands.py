"""
core/commands.py — Command pattern for undo/redo.

Every user action that mutates the scene is wrapped in a Command subclass.
The CommandStack manages history with configurable depth.

This is Phase 1 scaffolding — only AddComponent and MoveComponent are
fully implemented. More commands follow in Phase 2+.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, Optional, Callable
from dataclasses import dataclass

from core.model import DesignScene, GDSComponent, Point


# ── Abstract base ─────────────────────────────────────────────────────────────

class Command(ABC):
    """A reversible mutation of the design scene."""

    @abstractmethod
    def execute(self, scene: DesignScene) -> None: ...

    @abstractmethod
    def undo(self, scene: DesignScene) -> None: ...

    @property
    def description(self) -> str:
        return self.__class__.__name__


# ── Concrete commands ─────────────────────────────────────────────────────────

@dataclass
class AddComponent(Command):
    component: GDSComponent

    def execute(self, scene: DesignScene) -> None:
        scene.add(self.component)

    def undo(self, scene: DesignScene) -> None:
        scene.remove(self.component.id)

    @property
    def description(self) -> str:
        return f"Add {self.component.kind.name.lower()} on layer {self.component.layer}"


@dataclass
class RemoveComponent(Command):
    component: GDSComponent

    def execute(self, scene: DesignScene) -> None:
        scene.remove(self.component.id)

    def undo(self, scene: DesignScene) -> None:
        scene.add(self.component)

    @property
    def description(self) -> str:
        return f"Remove {self.component.id}"


@dataclass
class MoveComponent(Command):
    comp_id: str
    old_origin: Point
    new_origin: Point

    def execute(self, scene: DesignScene) -> None:
        c = scene.get(self.comp_id)
        if c:
            dx = self.new_origin.x - self.old_origin.x
            dy = self.new_origin.y - self.old_origin.y
            c.move_by(dx, dy)

    def undo(self, scene: DesignScene) -> None:
        c = scene.get(self.comp_id)
        if c:
            dx = self.old_origin.x - self.new_origin.x
            dy = self.old_origin.y - self.new_origin.y
            c.move_by(dx, dy)

    @property
    def description(self) -> str:
        return f"Move {self.comp_id}"


# ── Stack ─────────────────────────────────────────────────────────────────────

class CommandStack:
    """
    Manages undo/redo history.
    on_change is fired after every execute/undo/redo so the UI can update.
    """

    MAX_DEPTH = 200

    def __init__(
        self,
        scene: DesignScene,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._scene    = scene
        self._undo_stack: Deque[Command] = deque(maxlen=self.MAX_DEPTH)
        self._redo_stack: Deque[Command] = deque(maxlen=self.MAX_DEPTH)
        self._on_change = on_change or (lambda: None)

    # ── Public API ────────────────────────────────────────────────────────────

    def execute(self, cmd: Command) -> None:
        """Execute a command and push it onto the undo stack."""
        cmd.execute(self._scene)
        self._undo_stack.append(cmd)
        self._redo_stack.clear()   # new action clears redo history
        self._on_change()

    def undo(self) -> Optional[str]:
        """Undo the last command. Returns its description or None."""
        if not self._undo_stack:
            return None
        cmd = self._undo_stack.pop()
        cmd.undo(self._scene)
        self._redo_stack.append(cmd)
        self._on_change()
        return cmd.description

    def redo(self) -> Optional[str]:
        """Redo the last undone command."""
        if not self._redo_stack:
            return None
        cmd = self._redo_stack.pop()
        cmd.execute(self._scene)
        self._undo_stack.append(cmd)
        self._on_change()
        return cmd.description

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._on_change()

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
