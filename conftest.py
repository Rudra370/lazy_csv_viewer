"""Pytest fixtures for the Lazy CSV Viewer tests.

The tests construct the real Tk application, so they require ``tkinter`` and a
display. Where neither is available (e.g. headless CI) the ``app`` fixture skips
instead of failing.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A constructed LazyCSVViewerGUI on a hidden root, with config isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))  # never touch the real ~/.lazy_csv_viewer.json
    tk = pytest.importorskip("tkinter")
    import main

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for tkinter")
    root.withdraw()
    # Silence modal dialogs so error paths don't block the test run
    monkeypatch.setattr(main.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(main.messagebox, "showinfo", lambda *a, **k: None)

    application = main.LazyCSVViewerGUI(root)
    yield application
    root.destroy()
