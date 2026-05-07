"""
core/clipboard.py — In-process clipboard for copy/paste of GDS components.

Design notes:
  - Singleton pattern: Clipboard.instance() always returns the same object.
  - Uses deep-copy so editing originals never corrupts clipboard contents.
  - paste() regenerates all component IDs and remaps group member_ids so
    every paste produces fully independent objects with no ID collisions.
  - Connections are intentionally NOT copied — pasted components get new IDs
    so inter-component connections would need full remapping. Dropping them
    on paste matches standard EDA tool behaviour.
  - Cell group metadata (cell_id, _cell_params) is preserved via deepcopy
    so pasted parametric cells remain editable in the Properties panel.
  - paste_count tracks consecutive pastes for offset accumulation — each
    paste nudges further so repeated pastes don't stack invisibly on top.
    Reset by calling reset_paste_count() after any non-paste user action.
"""

from __future__ import annotations

import copy
import uuid
from typing import Optional

from core.model import GDSComponent, ComponentGroup


class Clipboard:
    """
    Singleton in-process clipboard for GDS canvas copy/paste.

    Usage
    -----
        Clipboard.instance().copy(components, group)   # on Ctrl+C
        comps, group = Clipboard.instance().paste(offset_dbu=10_000)  # on Ctrl+V
    """

    _instance: Optional["Clipboard"] = None

    def __init__(self) -> None:
        self._entries: list[GDSComponent]       = []
        self._group_template: Optional[ComponentGroup] = None
        self._paste_count: int                  = 0

    @classmethod
    def instance(cls) -> "Clipboard":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Write ─────────────────────────────────────────────────────────────────

    def copy(self, components: list[GDSComponent],
             group: Optional[ComponentGroup] = None) -> None:
        """
        Snapshot components (and optionally their group) into the clipboard.
        Deep-copies everything so later edits to originals are safe.
        Resets the paste-offset counter so the first paste lands at +1 offset.
        """
        self._entries        = [copy.deepcopy(c) for c in components]
        self._group_template = copy.deepcopy(group)
        self._paste_count    = 0

    @property
    def is_empty(self) -> bool:
        return not self._entries

    # ── Read ──────────────────────────────────────────────────────────────────

    def paste(self, base_offset_dbu: int = 10_000,
              target_center: Optional[tuple[int, int]] = None,
              ) -> tuple[list[GDSComponent], Optional[ComponentGroup]]:
        """
        Return fresh deep-copies of clipboard contents with:
          - New unique IDs for every component (and the group, if any)
          - Group member_ids remapped to the new component IDs
          - Cell metadata (cell_id, _cell_params) preserved
          - Positioning:
              • If target_center (x_dbu, y_dbu) is given, the pasted selection
                is centred on that point (viewport centre) and then nudged by
                base_offset_dbu × paste_count so consecutive pastes stagger.
                This ensures paste always lands on-screen regardless of where
                the source objects live in scene space.
              • If target_center is None (legacy path), behaviour is unchanged:
                position = source_position + base_offset_dbu × paste_count.

        Increments internal paste_count — call reset_paste_count() whenever
        the user does something other than paste (move, new copy, etc.).

        Returns (components, group).  group is None if no group was copied.
        """
        if self.is_empty:
            return [], None

        self._paste_count += 1

        id_map: dict[str, str] = {}   # old_id → new_id
        pasted: list[GDSComponent] = []

        if target_center is not None:
            # Centre the pasted selection exactly on the cursor — no stagger.
            xs = [t.origin.x for t in self._entries]
            ys = [t.origin.y for t in self._entries]
            src_cx = (min(xs) + max(xs)) // 2
            src_cy = (min(ys) + max(ys)) // 2
            base_dx = target_center[0] - src_cx
            base_dy = target_center[1] - src_cy
        else:
            # Legacy fallback: offset from source position.
            offset  = base_offset_dbu * self._paste_count
            base_dx = offset
            base_dy = offset

        for template in self._entries:
            comp    = copy.deepcopy(template)
            old_id  = comp.id
            comp.id = uuid.uuid4().hex[:8]
            id_map[old_id] = comp.id
            comp.move_by(base_dx, base_dy)
            # Regenerate port IDs so pasted ports never alias originals
            for port in comp.ports:
                port.id = uuid.uuid4().hex[:6]
            pasted.append(comp)

        new_group: Optional[ComponentGroup] = None
        if self._group_template is not None:
            new_group            = copy.deepcopy(self._group_template)
            new_group.id         = uuid.uuid4().hex[:8]
            new_group.member_ids = [id_map.get(oid, oid)
                                    for oid in new_group.member_ids]
            # Preserve cell metadata if present (set by PlaceCellCommand).
            # deepcopy carries dynamic attrs automatically, BUT _cell_subgroups
            # contains its own "member_ids" lists that must also be remapped —
            # otherwise param edits on pasted cells look up stale (pre-paste)
            # component IDs, find nothing, and place a duplicate at the origin.
            for sg in getattr(new_group, "_cell_subgroups", []):
                if "member_ids" in sg:
                    sg["member_ids"] = [id_map.get(oid, oid)
                                        for oid in sg["member_ids"]]

        return pasted, new_group

    def reset_paste_count(self) -> None:
        """
        Reset the paste-offset counter.  Call this after any non-paste action
        (move, new copy, undo, etc.) so the next paste starts at offset ×1.
        """
        self._paste_count = 0