"""
config.py — Central configuration for the GDS layout.

All physical dimensions are in micrometres (µm).
Edit values here; everything else adapts automatically.
"""


class Config:
    # ── Conductor geometry ──────────────────────────────────────────────────
    SQUARE_SIZE: float = 2.0       # Side length of each bonding square
    WIRE_WIDTH: float = 0.3        # Narrow lead width
    TAPER_WIDTH: float = 2.0       # Wide taper entry width (Turina/branch)

    # ── Lead lengths ────────────────────────────────────────────────────────
    L_SHORT: float = 1.5           # Short exit stub from a square
    L_LONG: float = 5.86           # Long connecting segment between squares
    L_TAPER: float = 6.1           # Taper length (except right-top)

    # ── Branch / turn geometry ───────────────────────────────────────────────
    BRANCH_RIGHT: float = L_TAPER  # Right-top taper length
    BRANCH_UP_PRE_TURN: float = 8.0
    BRANCH_LEFT_POST_TURN: float = 30.0
    TURN_RADIUS: float = 3.0       # Centreline turn radius

    # ── Final taper / pad (wire → thick lead) ────────────────────────────────
    FINAL_TAPER_WIDTH: float = 10.0
    FINAL_TAPER_LENGTH: float = 5.0
    FINAL_PAD_LENGTH: float = 2.0  # Overlap onto the thick lead

    # ── Manhattan junction ───────────────────────────────────────────────────
    JUNCTION_LEAD_LENGTH: float = 1.5
    JUNCTION_LEAD_WIDTH: float = 0.2
    JUNCTION_SQUARE_SIZE: float = JUNCTION_LEAD_WIDTH  # Square = lead width

    # ── Length compensation (snake route) ────────────────────────────────────
    LEN_COMPENSATE: float = (
        SQUARE_SIZE - WIRE_WIDTH / 2
        + L_LONG
        + SQUARE_SIZE * 2 - WIRE_WIDTH
        + L_SHORT
        + L_TAPER
        + BRANCH_RIGHT
        + JUNCTION_LEAD_LENGTH
        + JUNCTION_LEAD_WIDTH / 2
        - TURN_RADIUS * 2
    )

    # ── Undercut / cap geometry ──────────────────────────────────────────────
    CAP_H: float = 0.1             # Thin cap layer thickness
    CAP2_H: float = 0.7            # Thick cap layer thickness
    L_HORZ: float = 0.8            # Horizontal undercut extension

    # ── GDS layers ──────────────────────────────────────────────────────────
    LAYER_BRANCH: int = 1
    LAYER_CAP1: int = 4
    LAYER_BIYSK_JUNCTION: int = 5
    LAYER_CAP2: int = 6
    LAYER_JJ: int = 10
    LAYER_NARROW_END: int = 11      # 1 µm slice at the narrow end of selected tapers
    NARROW_END_LENGTH: float = 1.0  # µm extent of the reassigned region