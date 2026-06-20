# Project notes

Single-file Tkinter CSV viewer (`main.py`). Pure standard library — no runtime
dependencies beyond Python + Tkinter.

## Running

```
python3 main.py
```

The user's default `python3` is **Homebrew Python 3.13** (`/opt/homebrew/bin/python3`),
which has `tkinter` because `python-tk` is installed via Homebrew. The macOS
**system** `/usr/bin/python3` (3.9) also has Tkinter and is handy for quick
headless scripts.

## Testing & building — use a virtualenv

Homebrew's Python is **externally managed (PEP 668)**: `pip install …` is blocked
and bare `pip` doesn't exist. Install dev tools into a venv (which inherits
`tkinter` from the base install):

```
python3 -m venv .venv
source .venv/bin/activate
pip install pytest pyinstaller
python -m pytest        # tests in tests/test_core.py (need a display)
./build.sh              # -> dist/Lazy CSV Viewer.app
```

`build.sh` honors `PYTHON=…` to pick the interpreter. The app icon is generated
with no dependencies by `scripts/make_icon.py`.

## Gotchas

- On macOS, **Cmd+Q / app-menu Quit bypass `WM_DELETE_WINDOW`**. Settings are
  saved via both a `tk::mac::Quit` handler and a save-on-open, so don't rely on
  the close protocol alone for persistence.
- Config lives at `~/.lazy_csv_viewer.json` (window size, page size, delimiter,
  recent files).
- `.gitignore` ignores `test_data/` (generated fixtures) but tracks `tests/`.
