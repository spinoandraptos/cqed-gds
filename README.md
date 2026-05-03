# GDS Canvas Designer — Phase 1

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

## Phase 1 Features

- Dark industrial UI with amber accent (EDA-tool aesthetic)
- Infinite canvas with minor (1 µm) and major (10 µm) grid
- Origin crosshair at (0, 0)
- Smooth zoom: Ctrl+Scroll, +/− keys, F to fit
- Pan: middle-mouse drag or Space+drag
- Place rectangles on any of 8 GDS layers via palette or toolbar
- Drag-to-move with snap-to-grid
- Full undo/redo (Ctrl+Z / Ctrl+Shift+Z), 200-step history
- Selection with rubber-band drag; multi-select
- Delete selected (Delete key)
- Properties panel showing selected component geometry
- Status bar: cursor µm coordinates, zoom level, component count
- Unsaved-changes guard on close/new

## Project Structure

```
gds_canvas/
├── main.py                   Entry point
├── requirements.txt
├── core/
│   ├── model.py              GDSComponent, DesignScene, Point, BBox
│   └── commands.py           Command pattern (undo/redo)
└── ui/
    ├── theme.py              Colors, fonts, stylesheet, QPalette
    ├── canvas_scene.py       QGraphicsScene — owns items + command stack
    ├── canvas_view.py        QGraphicsView — zoom, pan, keyboard
    ├── panels.py             ComponentPalette + PropertiesPanel
    └── main_window.py        QMainWindow — menus, toolbar, docks, status
```

## Coordinate System

- **Internal**: integer database units (DBU). 1 DBU = 1 nm.
- **Display**: µm (shown in status bar and properties panel).
- **Y-axis**: screen Y increases downward (Qt convention). GDS export layer
  will negate Y. Do not flip in the canvas — keeps Qt geometry sane.
- **Grid**: 1 µm minor, 10 µm major. All snapping uses minor grid.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| F | Fit all |
| +/= | Zoom in |
| - | Zoom out |
| Ctrl+Scroll | Zoom |
| Space+drag | Pan |
| Middle-drag | Pan |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Ctrl+A | Select all |
| Delete | Delete selected |
| Ctrl+N | New design |
| Ctrl+E | Export GDS (Phase 4) |

## Roadmap

| Phase | Features |
|-------|---------|
| **1 (this)** | Skeleton, rectangle, undo/redo, grid, pan/zoom |
| 2 | Polygon + path tools, resize handles, layer visibility |
| 3 | Cell library, import existing GDS, hierarchy |
| 4 | GDS export via gdstk, save/load project JSON |
| 5 | Design rule check stubs, label text, ruler tool |
