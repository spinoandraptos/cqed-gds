"""
core/model.py — Immutable data model for GDS canvas elements.

Design principles:
  - All coordinates stored as integers in database units (1 DBU = 1 nm by default)
  - No floats in the model; floating point only lives in the view layer
  - Components are Python dataclasses for clean repr and easy serialization
  - Layer is a simple integer (GDS layer number, 0-255)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import IntEnum, auto
import uuid


# ── Units ─────────────────────────────────────────────────────────────────────

DBU_PER_UM = 1_000          # 1 µm = 1000 DBUs  (1 DBU = 1 nm)
DBU_PER_MM = 1_000_000      # 1 mm = 1,000,000 DBUs

def um_to_dbu(microns: float) -> int:
    """Convert micrometers to database units."""
    return int(round(microns * DBU_PER_UM))

def dbu_to_um(dbu: int) -> float:
    """Convert database units to micrometers."""
    return dbu / DBU_PER_UM


# ── Enumerations ──────────────────────────────────────────────────────────────

class ComponentKind(IntEnum):
    RECTANGLE = auto()
    POLYGON   = auto()
    PATH      = auto()


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Point:
    """2D integer point in database units."""
    x: int = 0
    y: int = 0

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def to_tuple(self) -> Tuple[int, int]:
        return (self.x, self.y)

    def to_um(self) -> Tuple[float, float]:
        return (dbu_to_um(self.x), dbu_to_um(self.y))

    @classmethod
    def from_um(cls, x_um: float, y_um: float) -> Point:
        return cls(um_to_dbu(x_um), um_to_dbu(y_um))


@dataclass
class BBox:
    """Axis-aligned bounding box in database units."""
    x_min: int = 0
    y_min: int = 0
    x_max: int = 0
    y_max: int = 0

    @property
    def width(self) -> int:
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        return self.y_max - self.y_min

    @property
    def center(self) -> Point:
        return Point((self.x_min + self.x_max) // 2,
                     (self.y_min + self.y_max) // 2)


@dataclass
class GDSComponent:
    """
    A single geometric component on the canvas.

    Coordinates are always in DBUs. The canvas view layer is responsible
    for converting to screen pixels using the current zoom/pan state.
    """
    id:       str          = field(default_factory=lambda: str(uuid.uuid4())[:8])
    kind:     ComponentKind = ComponentKind.RECTANGLE
    layer:    int           = 0
    datatype: int           = 0

    # For RECTANGLE: use origin + width/height
    origin:   Point        = field(default_factory=Point)
    width:    int          = um_to_dbu(10)   # 10 µm default
    height:   int          = um_to_dbu(5)    # 5 µm default

    # For POLYGON / PATH: list of vertices
    vertices: List[Point]  = field(default_factory=list)

    # Display / metadata
    label:    str          = ""
    locked:   bool         = False
    visible:  bool         = True

    @property
    def bbox(self) -> BBox:
        if self.kind == ComponentKind.RECTANGLE:
            return BBox(
                self.origin.x,
                self.origin.y,
                self.origin.x + self.width,
                self.origin.y + self.height,
            )
        elif self.vertices:
            xs = [p.x for p in self.vertices]
            ys = [p.y for p in self.vertices]
            return BBox(min(xs), min(ys), max(xs), max(ys))
        return BBox()

    def move_by(self, dx: int, dy: int) -> None:
        """Translate component in-place."""
        self.origin = Point(self.origin.x + dx, self.origin.y + dy)
        self.vertices = [Point(p.x + dx, p.y + dy) for p in self.vertices]


# ── Scene ─────────────────────────────────────────────────────────────────────

@dataclass
class DesignScene:
    """
    Top-level container for all components on the canvas.
    Analogous to a GDS top-level cell.
    """
    name:       str                   = "TOP"
    dbu:        int                   = 1          # 1 DBU = 1 nm
    components: List[GDSComponent]    = field(default_factory=list)
    is_dirty:   bool                  = False      # unsaved changes flag

    def add(self, comp: GDSComponent) -> None:
        self.components.append(comp)
        self.is_dirty = True

    def remove(self, comp_id: str) -> bool:
        for i, c in enumerate(self.components):
            if c.id == comp_id:
                del self.components[i]
                self.is_dirty = True
                return True
        return False

    def get(self, comp_id: str) -> Optional[GDSComponent]:
        for c in self.components:
            if c.id == comp_id:
                return c
        return None

    def clear(self) -> None:
        self.components.clear()
        self.is_dirty = True
