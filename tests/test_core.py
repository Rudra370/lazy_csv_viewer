"""Core logic tests for the Lazy CSV Viewer.

Covers the parts that are easy to get subtly wrong: the cancelable search
scan, the bounded row-count estimate, byte-offset paging (vs a full parse),
the lazy filter index, the detail-view backing data, and config persistence.

Run with: python3 -m pytest    (requires tkinter + a display)
"""

import csv
import json
import threading

import main


def write_csv(path, rows, header=("A", "B", "C")):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if header:
            writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def tree_rows(app):
    """Displayed rows with the leading row-number column stripped."""
    return [
        [str(c) for c in app.tree.item(item, "values")[1:]]
        for item in app.tree.get_children()
    ]


# --------------------------------------------------------------- scan_for_match


def test_scan_forward_and_bounds(tmp_path):
    path = write_csv(
        tmp_path / "f.csv",
        [["zebra" if i in (5, 250) else f"r{i}", "b", "c"] for i in range(300)],
    )
    assert main.scan_for_match(path, "utf-8", ",", "zebra", True, 0) == 5
    assert main.scan_for_match(path, "utf-8", ",", "zebra", True, 6) == 250
    assert main.scan_for_match(path, "utf-8", ",", "zebra", True, 251) is None
    assert main.scan_for_match(path, "utf-8", ",", "zebra", True, 0, stop_idx=5) is None


def test_scan_is_case_insensitive(tmp_path):
    path = write_csv(tmp_path / "f.csv", [["HeLLo World", "b", "c"]])
    assert main.scan_for_match(path, "utf-8", ",", "hello", True, 0) == 0


def test_scan_cancel(tmp_path):
    path = write_csv(tmp_path / "f.csv", [["zebra", "b", "c"]] * 100)
    event = threading.Event()
    event.set()
    assert (
        main.scan_for_match(path, "utf-8", ",", "zebra", True, 0, cancel_event=event)
        == "cancelled"
    )


def test_scan_no_header_counts_first_row_as_data(tmp_path):
    path = write_csv(
        tmp_path / "f.csv",
        [["zebra" if i == 0 else f"r{i}", "b"] for i in range(10)],
        header=None,
    )
    assert main.scan_for_match(path, "utf-8", ",", "zebra", False, 0) == 0


# ----------------------------------------------------------------- estimate


def test_estimate_exact_and_header_aware(app, tmp_path):
    path = write_csv(tmp_path / "f.csv", [[i, i, i] for i in range(500)])
    app.has_header = True
    assert app._estimate_total_rows(path) == (500, False)
    app.has_header = False
    assert app._estimate_total_rows(path) == (501, False)


def test_estimate_empty(app, tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("")
    assert app._estimate_total_rows(str(p)) == (0, False)


# ------------------------------------------------------------- byte-offset paging


def test_paging_matches_full_parse(app, tmp_path):
    data = [
        ["multi\nline" if i == 50 else f"r{i}", f"b{i}", f"c{i}"] for i in range(450)
    ]
    path = write_csv(tmp_path / "f.csv", data)
    with open(path, newline="", encoding="utf-8") as f:
        ground_truth = list(csv.reader(f))[1:]

    app.page_size = 100
    app._open_path(path)
    for page in [0, 1, 2, 3, 4, 2, 0, 4]:  # forward, back, re-jump
        app.current_page = page
        app._load_page()
        expected = [
            [str(c) for c in (row + [""] * (3 - len(row)))]
            for row in ground_truth[page * 100 : (page + 1) * 100]
        ]
        assert tree_rows(app) == expected, f"page {page}"


def test_go_to_row_selects(app, tmp_path):
    path = write_csv(tmp_path / "f.csv", [[f"r{i}", "b", "c"] for i in range(1000)])
    app.page_size = 100
    app._open_path(path)
    app.row_var.set("333")
    app.go_to_row()
    assert app.current_page == 3
    selected = app.tree.selection()
    assert selected and int(app.tree.item(selected[0], "values")[0]) == 333


# ----------------------------------------------------------------- filter


def test_filter_pages_over_matches(app, tmp_path):
    path = write_csv(
        tmp_path / "f.csv",
        [["MATCH" if i in (3, 7, 250) else f"x{i}", "b", "c"] for i in range(400)],
    )
    app.page_size = 2
    app._open_path(path)
    app.search_var.set("match")
    app.filter_var.set(True)
    app.toggle_filter()
    assert [int(app.tree.item(i, "values")[0]) for i in app.tree.get_children()] == [4, 8]
    app.next_page()
    assert app.filter_exhausted
    assert len(app.filter_matches) == 3


# --------------------------------------------------------------- no header


def test_no_header_mode(app, tmp_path):
    path = write_csv(
        tmp_path / "f.csv", [[f"v{i}", f"w{i}"] for i in range(100)], header=None
    )
    app.page_size = 50
    app._open_path(path)
    assert app.total_rows == 99  # header mode: first line treated as header
    app.no_header_var.set(True)
    app.toggle_header()
    assert app.total_rows == 100
    assert app.column_headers == ["Column 1", "Column 2"]
    assert tree_rows(app)[0] == ["v0", "w0"]


# --------------------------------------------------------------- detail view


def test_detail_keeps_hidden_columns(app, tmp_path):
    path = write_csv(
        tmp_path / "f.csv", [["n", "e", "s"]], header=("Name", "Email", "Secret")
    )
    app._open_path(path)
    app.hidden_columns = {1}
    app._load_page()
    # Hidden Email is gone from the table but retained for the detail view
    assert tree_rows(app)[0] == ["n", "s"]
    assert app._page_full_rows[0] == ["n", "e", "s"]


# ----------------------------------------------------------------- count worker


def test_count_worker_header_aware(app, tmp_path):
    path = write_csv(tmp_path / "f.csv", [[i, i] for i in range(3000)], header=("A", "B"))
    app._count_rows_worker(path, "utf-8", ",", True, 1)
    assert app._count_queue.get_nowait() == (1, 3000)
    app._count_rows_worker(path, "utf-8", ",", False, 1)
    assert app._count_queue.get_nowait() == (1, 3001)


# ----------------------------------------------------------------- config


def test_config_roundtrip(app):
    app.page_size = 77
    app.recent_files = ["/x/a.csv", "/x/b.csv"]
    app.delimiter = ";"
    app._save_config()
    with open(app.config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["page_size"] == 77
    assert cfg["recent_files"] == ["/x/a.csv", "/x/b.csv"]
    assert cfg["delimiter"] == ";"
