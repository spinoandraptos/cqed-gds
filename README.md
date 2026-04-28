# GDS Layout Editor

A GUI tool for placing and connecting GDS components from your existing
primitives/components library, with live polygon rendering and GDS export.

## Setup

```bash
pip install PyQt6 gdspy numpy
```

## Run

```bash
python main.py
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point |
| `app.py` | Main window, palette, properties panel, toolbar |
| `canvas.py` | GDS canvas: grid, snap, pan/zoom, port wiring |
| `component_model.py` | Data model + gdspy render bridge |
| `config.py` | Your original Config (unchanged) |
| `primitives.py` | Your original primitives (unchanged) |
| `undercuts.py` | Your original undercuts (unchanged) |
| `components_lib.py` | Your original components (unchanged) |
| `layout.py` | Your original layout (unchanged) |

## Usage

1. **Place** — drag a component card from the left panel onto the canvas
2. **Select** — click a component to see its parameters in the right panel
3. **Move** — drag a selected component; it snaps to 0.5 µm grid
4. **Wire** — switch to Wire tool, click a port dot (blue), click destination port
5. **Edit params** — change cap style, direction, layer etc. in the right panel; canvas updates live
6. **Layer toggle** — check/uncheck layers in the left panel
7. **Export** — click "Export GDS…" to write a timestamped .gds file
8. **Delete** — select a component and click "Delete sel." or press Delete
9. **Pan/Zoom** — scroll wheel to zoom, switch to Pan tool to drag the viewport
