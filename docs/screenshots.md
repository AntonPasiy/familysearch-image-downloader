# Screenshot stitching and visible-desktop capture (Windows)

These are optional `fsd stitch` and `fsd capture` commands. The existing
`fsd download` command is unchanged. **Use only with collections for which
saving copies is permitted**. These tools do not access browser cookies,
hidden APIs or disabled download controls.

## Install

From the cloned fork in **Windows PowerShell**:

```powershell
git switch feature/frame-range-png
uv sync --locked --no-dev
uv pip install -r requirements-screenshots.txt
.\.venv\Scripts\fsd.exe --help
```

Use the direct `.venv\Scripts\fsd.exe` after installing optional dependencies;
a later `uv sync` may remove packages not declared in `uv.lock`.

## 1. Stitch existing screenshots

Place screenshots of ONE document page in a directory as `001.png`,
`002.png`, etc., ordered in a **snake path**: left-to-right, move down,
right-to-left, move down, and repeat. Keep the exact same screen capture
rectangle and viewer zoom. Move the image ~65% of the viewport width or
height between captures, leaving about 35% overlap.

```powershell
.\.venv\Scripts\fsd.exe stitch .\screens\frame0600 --out .\downloads\frame_0600.png
```

Matching uses feature points and integer translation only. It does not
rotate, rescale, improve, blend or invent pixels. When alignment is
ambiguous or canvas coverage is incomplete, it fails without saving
a supposedly complete image.

## 2. Capture the currently visible page automatically

**Before launching**, open the first page in your browser and sign in
yourself. The tool does not load a URL, nor can it verify the page number
shown by FamilySearch. You must establish that the browser is on the
expected starting page.

The user (or Cursor while inspecting the interactive desktop) determines
screen coordinates **once**:

- `--region X,Y,WIDTH,HEIGHT`: clean document viewport, excluding toolbars.
  These are physical screen coordinates. Do not resize the window while
  capturing.
- `--next-click X,Y`: on-screen Next-page control, OR `--next-key right`.
  Test it on a small range first.
- `--zoom-click X,Y --zoom-count N`: optional N clicks on the actual viewer
  zoom-in button **before the first page**. This uses real viewer zoom, not
  resizing of saved screenshots. Alternatively, set the zoom manually once.

Start with just ONE frame, replacing these placeholder coordinates:

```powershell
.\.venv\Scripts\fsd.exe capture --frames 600-600 `
  --region 140,120,1200,760 `
  --out .\downloads\trial
```

Then, **only after the first frame is verified**:

```powershell
.\.venv\Scripts\fsd.exe capture --frames 600-620 `
  --region 140,120,1200,760 `
  --next-click 1400,600 `
  --out .\downloads\capture
```

Alternative: `--next-key right`; optional `--zoom-click X,Y --zoom-count 2`,
`--step 0.65`, `--max-cols 12`, `--max-rows 12`, `--start-delay 5`,
`--next-delay 3`, `--allow-single` (only when a single viewport is
**visually confirmed** to contain the complete page).

The script seeks the top-left image edge, pans in a snake path, takes
overlapping PNG screenshots, stitches each page, then operates the
configured Next control. Images are saved to `frame_0600.png`, etc.;
unmodified captures stay in `raw/frame_0600/`.
`capture_manifest.json` records the expected page number and source URL
(`--url` for metadata only). It marks
`viewer_frame_number_verified: false`, because it cannot read the viewer
counter. Inspect this against the actual website before trusting a batch.

### Operational limits

- Requires an **unlocked interactive Windows desktop**; keep the browser
  visible and avoid using mouse/keyboard during capture.
- Emergency stop: move the pointer to the **top-left screen corner**
  (PyAutoGUI failsafe) or press Ctrl+C in the terminal.
- If panning is unsupported, missing content is detected, or the capture
  limit is reached, the command fails. Raw screenshots remain available.
- Source image detail is limited to what was *actually displayed* by the
  viewer. This does not recover original image data.
- The utility does not determine or bypass legal permissions to save a
  restricted archive collection.
