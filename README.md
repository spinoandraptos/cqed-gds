# GDS Canvas Designer

A production-grade desktop GDSII layout editor built with Python + PyQt6.

## Setup

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Launch
python main.py
```

**Requires Python 3.11+**

## Features

### Canvas & Navigation
- Dark industrial UI with amber accent (EDA-tool aesthetic)
- Infinite canvas with minor (1 µm) and major (10 µm) grid
- Origin crosshair at (0, 0)
- Smooth zoom: Ctrl+Scroll, +/− keys, F to fit
- Pan: middle-mouse drag or Space+drag

### Primitives & Placement
- Place rectangles, polygons, and paths on any GDS layer via palette or drag-and-drop
- Drag-to-move with snap-to-grid
- Rotate selection 90° CW / CCW (R / Shift+R)
- Copy, paste, and duplicate with automatic ID remapping (Ctrl+C / Ctrl+V / Ctrl+D)
- Port-based snap: components snap together when facing ports align on drag

### Cell Library
Parametric cells drag onto the canvas from the Cell Library panel and are editable via the Properties panel after placement.

| Category | Cells |
|----------|-------|
| Junctions | Biysk JJ, Manhattan JJ, Bridge-Free JJ |
| Routing | Lead Segment, Tapered Lead, Smooth Taper Pad, Turn 90°, T-Junction |
| Substrate | Chip Outline |

### Editing
- Full undo/redo (Ctrl+Z / Ctrl+Shift+Z), 200-step history
- Selection with rubber-band drag; multi-select
- Group / ungroup selections (Ctrl+G / Ctrl+Shift+G)
- Delete selected (Delete key)
- Copy/paste preserves cell metadata and intra-group connections; inter-group connections are dropped (standard EDA behaviour)

### Measurement & Overlay
- Ruler tool (M): click-drag to measure distance with ΔX / ΔY breakdown and tick marks
- Undercut overlay: visualise etch undercut rings with adjustable offset; per-component exclusion masks; toggle on/off
- Parameter sweep (Ctrl+W): step a cell or component parameter across a range and preview results

### Save / Export
- Save and load project files as JSON (Ctrl+S / Ctrl+O); undercut masks are persisted
- Export to GDS via gdstk (Ctrl+E) with per-layer GDS layer / datatype mapping
- Post-export verification summary; one-click "Open in KLayout" if klayout is on PATH

### UI
- Properties panel: live geometry readout and cell parameter editing for selected component or group
- Status bar: cursor µm coordinates, zoom level, component count
- Unsaved-changes guard on close/new

## Project Structure

```
gds_canvas/
├── main.py                     Entry point
├── requirements.txt
├── core/
│   ├── model.py                GDSComponent, DesignScene, Point, BBox, Port, Connection
│   ├── commands.py             Command pattern (undo/redo stack)
│   ├── cell_library.py         Parametric cell builders and CELL_CATALOGUE
│   ├── clipboard.py            In-process copy/paste with ID remapping
│   ├── serialiser.py           JSON save/load (schema v1)
│   └── exporter.py             GDS export via gdstk
└── ui/
    ├── theme.py                Colors, fonts, stylesheet, QPalette
    ├── canvas_scene.py         QGraphicsScene — owns items + command stack
    ├── canvas_view.py          QGraphicsView — zoom, pan, drag-and-drop
    ├── panels.py               ComponentPalette + PropertiesPanel
    ├── cell_palette.py         Cell Library panel (drag source)
    ├── ruler_overlay.py        RulerItem — interactive measurement tool
    ├── undercut_overlay.py     UndercutOverlay — etch ring visualisation
    ├── sweep_dialog.py         Parameter sweep dialog
    ├── export_dialog.py        GDS export + layer mapping dialog
    └── main_window.py          QMainWindow — menus, toolbar, docks, status
```

## Coordinate System

- **Internal**: integer database units (DBU). 1 DBU = 1 nm.
- **Display**: µm (shown in status bar and properties panel).
- **Y-axis**: screen Y increases downward (Qt convention). GDS export negates Y. Do not flip in the canvas — keeps Qt geometry sane.
- **Grid**: 1 µm minor, 10 µm major. All snapping uses minor grid.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| F | Fit all |
| +/= | Zoom in |
| − | Zoom out |
| Ctrl+Scroll | Zoom |
| Space+drag | Pan |
| Middle-drag | Pan |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Ctrl+A | Select all |
| Delete | Delete selected |
| R | Rotate 90° CW |
| Shift+R | Rotate 90° CCW |
| M | Toggle ruler / measure mode |
| Ctrl+C | Copy |
| Ctrl+V | Paste |
| Ctrl+D | Duplicate |
| Ctrl+G | Group |
| Ctrl+Shift+G | Ungroup |
| Ctrl+W | Sweep parameter |
| Ctrl+N | New design |
| Ctrl+O | Open |
| Ctrl+S | Save |
| Ctrl+Shift+S | Save As |
| Ctrl+E | Export GDS |
| Ctrl+Q | Quit |
| Ctrl+H | Keyboard shortcuts help |