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
from typing import Dict, List, Optional, Callable, Set


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


# ── Port ──────────────────────────────────────────────────────────────────────

class PortSide(Enum):
    """Cardinal direction a port faces (outward normal)."""
    NORTH = auto()
    SOUTH = auto()
    EAST  = auto()
    WEST  = auto()

    @property
    def opposite(self) -> "PortSide":
        return {
            PortSide.NORTH: PortSide.SOUTH,
            PortSide.SOUTH: PortSide.NORTH,
            PortSide.EAST:  PortSide.WEST,
            PortSide.WEST:  PortSide.EAST,
        }[self]


@dataclass
class Port:
    """
    A named connection point on a component.

    - `offset` is in DBU, relative to the component's origin.
      It moves with the component automatically — no extra bookkeeping.
    - `side` is the outward-facing direction (the direction signal leaves).
      Snap is valid only when two ports face each other (side == other.opposite).
    - `name` is user-visible ("in", "out", "A", "B", …).
    """
    name:   str
    offset: Point          # relative to component origin, DBU
    side:   PortSide
    id:     str = field(default_factory=lambda: uuid.uuid4().hex[:6])

    def abs_pos(self, origin: Point) -> Point:
        """Absolute scene position given the component's current origin."""
        return Point(origin.x + self.offset.x, origin.y + self.offset.y)


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

    # Ports
    ports:      List[Port] = field(default_factory=list)

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

    def build_default_ports(self) -> None:
        """
        Auto-generate four edge-centre ports from the bounding box.
        Called once after the component is fully constructed.
        Safe to call again — replaces existing auto-ports.
        """
        bb = self.bbox
        cx = (bb.x_min + bb.x_max) // 2
        cy = (bb.y_min + bb.y_max) // 2
        # Offsets are relative to self.origin
        ox, oy = self.origin.x, self.origin.y
        self.ports = [
            Port("N", Point(cx - ox, bb.y_min - oy), PortSide.NORTH),
            Port("S", Point(cx - ox, bb.y_max - oy), PortSide.SOUTH),
            Port("W", Point(bb.x_min - ox, cy - oy), PortSide.WEST),
            Port("E", Point(bb.x_max - ox, cy - oy), PortSide.EAST),
        ]


# ── Connection ────────────────────────────────────────────────────────────────

@dataclass
class Connection:
    """
    An undirected bond between exactly two (component, port) pairs.
    Stores IDs only — no live object references.
    """
    comp_a: str = ""
    port_a: str = ""
    comp_b: str = ""
    port_b: str = ""
    id:     str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def _key(self) -> frozenset:
        return frozenset({(self.comp_a, self.port_a), (self.comp_b, self.port_b)})


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
        self._connections: List[Connection]  = []

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
                # Purge connections involving this component
                self._connections = [
                    cn for cn in self._connections
                    if cn.comp_a != comp_id and cn.comp_b != comp_id
                ]
                return self._components.pop(i)
        return None

    def get(self, comp_id: str) -> Optional[GDSComponent]:
        for c in self._components:
            if c.id == comp_id:
                return c
        return None

    def clear(self) -> None:
        self._components.clear()
        self._connections.clear()
        self.is_dirty = False

    def __len__(self) -> int:
        return len(self._components)

    # ── Connection API ────────────────────────────────────────────────────────

    @property
    def connections(self) -> List[Connection]:
        return list(self._connections)

    def connect(self, comp_a_id: str, port_a_id: str,
                comp_b_id: str, port_b_id: str) -> Connection:
        """Create a bond between two ports. Idempotent — returns existing if already connected."""
        new_key = frozenset({(comp_a_id, port_a_id), (comp_b_id, port_b_id)})
        for existing in self._connections:
            if existing._key == new_key:
                return existing
        conn = Connection(comp_a=comp_a_id, port_a=port_a_id,
                          comp_b=comp_b_id, port_b=port_b_id)
        self._connections.append(conn)
        self.is_dirty = True
        return conn

    def disconnect(self, connection_id: str) -> bool:
        for i, cn in enumerate(self._connections):
            if cn.id == connection_id:
                self._connections.pop(i)
                self.is_dirty = True
                return True
        return False

    def connections_for(self, comp_id: str) -> List[Connection]:
        return [cn for cn in self._connections
                if cn.comp_a == comp_id or cn.comp_b == comp_id]

    def connected_sides(self, comp_id: str) -> List[PortSide]:
        """Return which PortSides on comp_id currently have a connection."""
        comp = self.get(comp_id)
        if comp is None:
            return []
        port_map: Dict[str, Port] = {p.id: p for p in comp.ports}
        occupied: Set[str] = set()
        for cn in self.connections_for(comp_id):
            if cn.comp_a == comp_id:
                occupied.add(cn.port_a)
            else:
                occupied.add(cn.port_b)
        return [port_map[pid].side for pid in occupied if pid in port_map]

    def are_connected(self, comp_a_id: str, port_a_id: str,
                      comp_b_id: str, port_b_id: str) -> bool:
        key = frozenset({(comp_a_id, port_a_id), (comp_b_id, port_b_id)})
        return any(cn._key == key for cn in self._connections)