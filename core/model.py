"""
core/model.py — Data model for GDS Canvas Designer.

Design rules:
  - Pure Python dataclasses; zero Qt dependency.
  - All lengths in DBU (database units = nm). Conversion helpers at bottom.
  - GDSComponent is the single union type for all primitives.
    Kind-specific fields are Optional; unused fields stay None.
  - DesignScene owns the component list and dirty flag.

Phase 2 additions (backward-compatible):
  - GDSComponent.points  — polygon vertex list (None for rect)
  - GDSComponent.path_width — path half-width in DBU (None for rect/polygon)
  - ComponentKind.POLYGON and PATH were already in the enum stub
  - BBox now handles polygon bounding box from points list
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Callable


# ── Unit conversion ───────────────────────────────────────────────────────────

DBU_PER_UM = 1_000   # 1 µm = 1000 nm (DBU)

def um_to_dbu(um: float) -> int:
    return int(round(um * DBU_PER_UM))

def dbu_to_um(dbu: int) -> float:
    return dbu / DBU_PER_UM


# ── Primitives ────────────────────────────────────────────────────────────────

@dataclass
class Point:
    x: int   # DBU
    y: int   # DBU

    @staticmethod
    def from_um(x_um: float, y_um: float) -> "Point":
        return Point(um_to_dbu(x_um), um_to_dbu(y_um))

    def __eq__(self, other) -> bool:
        return isinstance(other, Point) and self.x == other.x and self.y == other.y

    def __repr__(self) -> str:
        return f"Point({dbu_to_um(self.x):.3f}µm, {dbu_to_um(self.y):.3f}µm)"


@dataclass
class BBox:
    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @property
    def width(self) -> int:
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        return self.y_max - self.y_min

    @staticmethod
    def from_points(pts: List[Point]) -> "BBox":
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        return BBox(min(xs), min(ys), max(xs), max(ys))


# ── Component kinds ───────────────────────────────────────────────────────────

class ComponentKind(Enum):
    RECTANGLE = auto()
    POLYGON   = auto()
    PATH      = auto()


# ── GDS Component ─────────────────────────────────────────────────────────────

@dataclass
class GDSComponent:
    """
    Union type for all canvas primitives.

    Rectangle:  origin + width + height (points=None, path_width=None)
    Polygon:    points (closed, ≥3 vertices); origin = points[0] for snap reference
                width/height are ignored (use bbox)
    Path:       points (open polyline) + path_width
                origin = points[0]; width/height ignored

    The id is a short UUID4 hex prefix — unique enough for a single session.
    """

    kind:       ComponentKind
    layer:      int
    origin:     Point

    # Rectangle fields
    width:      int = 0
    height:     int = 0

    # Polygon / Path fields
    points:     Optional[List[Point]] = None   # vertex list (includes origin)
    path_width: Optional[int]         = None   # path half-width in DBU

    # Identity
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # ── Derived geometry ──────────────────────────────────────────────────────

    @property
    def bbox(self) -> BBox:
        if self.kind == ComponentKind.RECTANGLE:
            return BBox(
                self.origin.x,
                self.origin.y,
                self.origin.x + self.width,
                self.origin.y + self.height,
            )
        elif self.points:
            bb = BBox.from_points(self.points)
            if self.kind == ComponentKind.PATH and self.path_width:
                hw = self.path_width // 2
                return BBox(bb.x_min - hw, bb.y_min - hw,
                            bb.x_max + hw, bb.y_max + hw)
            return bb
        # Fallback: empty bbox at origin
        return BBox(self.origin.x, self.origin.y, self.origin.x, self.origin.y)

    @property
    def vertex_count(self) -> int:
        if self.kind == ComponentKind.RECTANGLE:
            return 4
        return len(self.points) if self.points else 0

    def move_by(self, dx: int, dy: int) -> None:
        """Translate in-place by (dx, dy) DBU."""
        self.origin = Point(self.origin.x + dx, self.origin.y + dy)
        if self.points:
            self.points = [Point(p.x + dx, p.y + dy) for p in self.points]


# ── Design scene ──────────────────────────────────────────────────────────────

class DesignScene:
    """
    Top-level container. Owns the ordered component list and dirty state.
    Intentionally not a dataclass so we control mutation.
    """

    def __init__(self, name: str = "TOP") -> None:
        self.name = name
        self.is_dirty = False
        self._components: List[GDSComponent] = []

    @property
    def components(self) -> List[GDSComponent]:
        return list(self._components)   # return copy so callers can't mutate

    def add(self, comp: GDSComponent) -> None:
        self._components.append(comp)
        self.is_dirty = True

    def remove(self, comp_id: str) -> Optional[GDSComponent]:
        for i, c in enumerate(self._components):
            if c.id == comp_id:
                self.is_dirty = True
                return self._components.pop(i)
        return None

    def get(self, comp_id: str) -> Optional[GDSComponent]:
        for c in self._components:
            if c.id == comp_id:
                return c
        return None

    def clear(self) -> None:
        self._components.clear()
        self.is_dirty = False

    def __len__(self) -> int:
        return len(self._components)