import csv
import io
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk


def scan_for_match(
    path,
    encoding,
    delimiter,
    query_lower,
    has_header,
    start_idx,
    stop_idx=None,
    start_offset=0,
    start_offset_idx=0,
    cancel_event=None,
):
    """
    Return the absolute 0-based data-row index of the first row at/after
    start_idx (and before stop_idx, if given) whose cells contain query_lower,
    or None if there is no such row. Returns the string "cancelled" if
    cancel_event fires mid-scan.

    Pure function (no shared state) so it is safe to run on a worker thread and
    easy to unit-test. If start_offset/start_offset_idx are given they must
    correspond (a byte offset and the row index that begins there) and are used
    to seek straight to the start instead of reading from the top.
    """
    with open(path, "r", newline="", encoding=encoding, errors="replace") as f:
        if start_offset and start_offset_idx > 0:
            f.seek(start_offset)
            idx = start_offset_idx
        else:
            idx = 0
            if has_header:
                if not f.readline():
                    return None
        reader = csv.reader(f, delimiter=delimiter)
        checked = 0
        for row in reader:
            if (
                cancel_event is not None
                and (checked & 0x1FFF) == 0
                and cancel_event.is_set()
            ):
                return "cancelled"
            checked += 1
            if idx >= start_idx:
                if stop_idx is not None and idx >= stop_idx:
                    break
                if any(query_lower in cell.lower() for cell in row):
                    return idx
            idx += 1
    return None


class LazyCSVViewerGUI:
    """
    A simple CSV viewer application that loads and displays CSV files page by page.
    Supports navigation between pages and horizontal/vertical scrolling.
    """

    ROWNUM_ID = "rownum"  # Column id for the synthetic row-number column

    def __init__(self, root: tk.Tk):
        """
        Initialize the application with the main window and default settings.

        Args:
            root: The tkinter root window
        """
        self.root = root
        self.root.title("Lazy CSV Viewer")

        # Application state variables
        self.file_path = None
        self.page_size = 200  # Default rows per page
        self.current_page = 0
        self.has_next_page = False
        # Byte offset of the first data row of each visited page, so Next/Prev
        # can seek() directly instead of re-scanning from the top (O(1) paging).
        self.page_offsets = {}
        self._byte_pos = 0  # Updated by _row_iter while reading
        self.total_rows = None  # Estimated/exact total data rows (None = unknown)
        self.total_is_estimate = False  # True when total_rows is a sample estimate
        self.scroll_speed = 20  # Controls how fast horizontal scrolling moves
        self.delimiter = ","  # Default delimiter (comma)
        self.encoding = "utf-8"  # Detected per-file when a file is opened
        self.has_header = True  # False => treat row 1 as data, synth column names
        self.expand_columns = False  # False = fit-to-window, True = full content width
        self.hidden_columns = set()  # Track hidden columns
        self.column_headers = []  # Store column headers for reference
        self._page_full_rows = []  # Full (unfiltered-columns) rows for the detail view
        self.delimiter_options = [
            (",", "Comma (,)"),
            ("\t", "Tab (\\t)"),
            (";", "Semicolon (;)"),
            ("|", "Pipe (|)"),
            (" ", "Space ( )"),
        ]

        # Search state (search runs on a worker thread, cancelable)
        self._search_query = None
        self._last_match_row = -1
        self._search_queue = queue.Queue()
        self._search_token = 0
        self._search_cancel = threading.Event()
        self._searching = False

        # Filter state (show only matching rows, paged, scanned lazily)
        self.filter_active = False
        self.filter_query = None
        self.filter_matches = []  # list of (abs_idx, full_padded_row)
        self.filter_scan_offset = None  # byte offset to resume scanning
        self.filter_scan_idx = 0  # next absolute row index to read
        self.filter_exhausted = False

        # Background exact-count state (off the UI thread)
        self._count_queue = queue.Queue()
        self._count_token = 0

        # Live appearance tracking (re-theme on macOS dark/light toggle)
        self._dark_mode = False

        # Persisted settings
        self.config_path = os.path.join(
            os.path.expanduser("~"), ".lazy_csv_viewer.json"
        )
        self.recent_files = []
        self.last_dir = None
        self._saved_geometry = None
        self._load_config()

        # Configure Treeview style and pick a palette that matches the OS appearance
        self.style = ttk.Style()
        self.style.configure("Treeview", rowheight=25)
        self._apply_theme()

        # Setup menu, UI components, and event bindings
        self._setup_menubar()
        self._setup_widgets()
        self._setup_bindings()
        self._setup_context_menu()

        self._update_nav_buttons()
        self._apply_initial_geometry()

        # Save on close; start the background pollers
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # On macOS, Cmd+Q and the app-menu Quit bypass WM_DELETE_WINDOW; route
        # them through the same handler so settings are still saved.
        if sys.platform == "darwin":
            try:
                self.root.createcommand("tk::mac::Quit", self._on_close)
            except tk.TclError:
                pass
        self.root.after(300, self._poll_count_queue)
        self.root.after(150, self._poll_search_queue)
        self.root.after(3000, self._poll_appearance)

    # ------------------------------------------------------------------ config

    def _load_config(self):
        """Load persisted settings; missing/corrupt config falls back to defaults."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        self._saved_geometry = cfg.get("geometry")
        page_size = cfg.get("page_size")
        if isinstance(page_size, int) and page_size > 0:
            self.page_size = page_size
        recent = cfg.get("recent_files")
        if isinstance(recent, list):
            self.recent_files = [p for p in recent if isinstance(p, str)]
        last_dir = cfg.get("last_dir")
        if isinstance(last_dir, str):
            self.last_dir = last_dir
        delimiter = cfg.get("delimiter")
        if isinstance(delimiter, str) and any(
            delimiter == d for d, _ in self.delimiter_options
        ):
            self.delimiter = delimiter

    def _save_config(self):
        """Persist window size and recent settings; failures are non-fatal."""
        cfg = {
            "geometry": self.root.geometry(),
            "page_size": self.page_size,
            "recent_files": self.recent_files[:8],
            "last_dir": self.last_dir,
            "delimiter": self.delimiter,
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except OSError:
            pass

    def _on_close(self):
        """Save settings, then close the window."""
        self._save_config()
        self.root.destroy()

    def _apply_initial_geometry(self):
        """Restore the saved window geometry, or fill the screen on first run."""
        if self._saved_geometry:
            try:
                self.root.geometry(self._saved_geometry)
                return
            except tk.TclError:
                pass
        try:
            self.root.state("zoomed")  # Works on Windows and recent macOS Tk
        except tk.TclError:
            pass
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        self.root.geometry(f"{width}x{height}+0+0")

    # ------------------------------------------------------------------- theme

    def _detect_dark_mode(self):
        """
        Return True if the OS is using a dark appearance.

        Only macOS is probed (via `defaults read -g AppleInterfaceStyle`,
        which returns "Dark" in dark mode and errors out in light mode).
        On every other platform this returns False so the original light
        palette is kept unchanged.
        """
        if sys.platform != "darwin":
            return False
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return result.stdout.strip() == "Dark"
        except Exception:
            return False

    def _apply_theme(self):
        """
        Choose row/text colors based on the OS appearance and apply them to the
        Treeview style. Row tag colors are stored on self for use when rows are
        inserted in _display().
        """
        self._dark_mode = self._detect_dark_mode()
        if self._dark_mode:
            # Dark mode: dark striped rows with light text
            self.odd_row_color = "#3A3A3A"
            self.even_row_color = "#2B2B2B"
            self.row_text_color = "#E8E8E8"
            field_bg = "#1E1E1E"
            selected_bg = "#0A84FF"
        else:
            # Light mode: original light striped rows with dark text
            self.odd_row_color = "#F0F0F0"
            self.even_row_color = "#FFFFFF"
            self.row_text_color = "#000000"
            field_bg = "#FFFFFF"
            selected_bg = "#0078D7"

        self.style.configure(
            "Treeview",
            background=field_bg,
            fieldbackground=field_bg,
            foreground=self.row_text_color,
        )
        self.style.map(
            "Treeview",
            background=[("selected", selected_bg)],
            foreground=[("selected", "#FFFFFF")],
        )

    def _poll_appearance(self):
        """Re-theme if the macOS appearance changed since the last check."""
        if self._detect_dark_mode() != self._dark_mode:
            self._apply_theme()
            if self.file_path:
                self._load_page()  # re-render so row tags pick up the new colors
        self.root.after(3000, self._poll_appearance)

    # ----------------------------------------------------------- scroll helpers

    def on_horizontal_mousewheel(self, event):
        """Handle horizontal scrolling with the mousewheel when Shift is pressed."""
        direction = -1 if event.delta > 0 else 1
        amount = direction * (self.scroll_speed / 1000)
        current = self.tree.xview()[0]
        self.tree.xview_moveto(max(0, min(1, current + amount)))
        return "break"

    def scroll_horizontal(self, units):
        """Scroll the treeview horizontally by the specified number of units."""
        amount = units * (self.scroll_speed / 100)
        current = self.tree.xview()[0]
        self.tree.xview_moveto(max(0, min(1, current + amount)))

    # ------------------------------------------------------------- ui assembly

    def _setup_menubar(self):
        """Create the menu bar with File -> Open / Open Recent / Quit."""
        menubar = tk.Menu(self.root)
        self.file_menu = tk.Menu(menubar, tearoff=0)
        self.file_menu.add_command(
            label="Open…", command=self.open_file, accelerator="Cmd+O"
        )
        self.recent_menu = tk.Menu(self.file_menu, tearoff=0)
        self.file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=self.file_menu)
        self.root.config(menu=menubar)
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        """Rebuild the Open Recent submenu from self.recent_files."""
        self.recent_menu.delete(0, "end")
        if not self.recent_files:
            self.recent_menu.add_command(label="(none)", state="disabled")
            return
        for path in self.recent_files:
            self.recent_menu.add_command(
                label=path, command=lambda p=path: self._open_path(p)
            )

    def _setup_widgets(self):
        """Create and arrange all UI widgets for the application."""
        self.frame = ttk.Frame(self.root, padding="10")
        self.frame.grid(row=0, column=0, sticky="nsew")

        # --- Top controls -----------------------------------------------------
        top = ttk.Frame(self.frame)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=5)

        self.open_button = ttk.Button(top, text="Open CSV File", command=self.open_file)
        self.open_button.pack(side=tk.LEFT, padx=(0, 6))
        self.reload_button = ttk.Button(top, text="Reload", command=self.reload_file)
        self.reload_button.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, text="Delimiter:").pack(side=tk.LEFT, padx=(10, 0))
        self.delimiter_var = tk.StringVar(value=self.delimiter)
        self.delimiter_dropdown = ttk.Combobox(
            top, textvariable=self.delimiter_var, state="readonly", width=15
        )
        self.delimiter_dropdown["values"] = [d for _, d in self.delimiter_options]
        self._select_delimiter_in_dropdown()
        self.delimiter_dropdown.pack(side=tk.LEFT, padx=5)
        self.delimiter_dropdown.bind("<<ComboboxSelected>>", self._on_delimiter_changed)

        self.no_header_var = tk.BooleanVar(value=False)
        self.no_header_check = ttk.Checkbutton(
            top, text="No header row", variable=self.no_header_var,
            command=self.toggle_header,
        )
        self.no_header_check.pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(top, text="Page Size:").pack(side=tk.LEFT, padx=(16, 0))
        self.page_size_var = tk.StringVar(value=str(self.page_size))
        self.page_size_entry = ttk.Entry(top, textvariable=self.page_size_var, width=6)
        self.page_size_entry.pack(side=tk.LEFT, padx=5)
        self.page_size_entry.bind("<Return>", lambda e: self._on_page_size_changed())
        ttk.Button(top, text="Apply", command=self._on_page_size_changed).pack(
            side=tk.LEFT
        )

        self.expand_columns_button = ttk.Button(
            top, text="Expand Columns", command=self._toggle_expand_columns
        )
        self.expand_columns_button.pack(side=tk.LEFT, padx=(10, 0))
        self.column_visibility_button = ttk.Button(
            top, text="Show/Hide Columns", command=self._show_column_selector,
            state="disabled",
        )
        self.column_visibility_button.pack(side=tk.LEFT, padx=(10, 0))

        # --- Search / filter / jump row --------------------------------------
        search = ttk.Frame(self.frame)
        search.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))

        ttk.Label(search, text="Search:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var, width=26)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", self.find_next)
        self.find_button = ttk.Button(search, text="Find Next", command=self.find_next)
        self.find_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            search, text="Cancel", command=self.cancel_search, state="disabled"
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(4, 0))
        self.filter_var = tk.BooleanVar(value=False)
        self.filter_check = ttk.Checkbutton(
            search, text="Filter", variable=self.filter_var, command=self.toggle_filter
        )
        self.filter_check.pack(side=tk.LEFT, padx=(8, 0))
        self.search_status = ttk.Label(search, text="", width=20)
        self.search_status.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(search, text="Go to row:").pack(side=tk.LEFT, padx=(16, 0))
        self.row_var = tk.StringVar()
        self.row_entry = ttk.Entry(search, textvariable=self.row_var, width=10)
        self.row_entry.pack(side=tk.LEFT, padx=5)
        self.row_entry.bind("<Return>", lambda e: self.go_to_row())
        ttk.Button(search, text="Go", command=self.go_to_row).pack(side=tk.LEFT)

        # --- Data table -------------------------------------------------------
        self.tree = ttk.Treeview(self.frame, show="headings")
        self.tree.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(
            self.frame, orient="vertical", command=self.tree.yview
        )
        self.scrollbar.grid(row=2, column=2, sticky="ns")
        self.xscrollbar = ttk.Scrollbar(
            self.frame, orient="horizontal", command=self.tree.xview
        )
        self.xscrollbar.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.tree.configure(
            yscrollcommand=self.scrollbar.set, xscrollcommand=self.xscrollbar.set
        )

        # --- Navigation -------------------------------------------------------
        self.prev_button = ttk.Button(
            self.frame, text="Previous Page", command=self.prev_page
        )
        self.prev_button.grid(row=4, column=0, pady=5)
        self.page_label = ttk.Label(
            self.frame, text="No file loaded", font=("Helvetica", 10)
        )
        self.page_label.grid(row=4, column=0, columnspan=2, pady=5)
        self.next_button = ttk.Button(
            self.frame, text="Next Page", command=self.next_page
        )
        self.next_button.grid(row=4, column=1, pady=5)

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(2, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=1)

    def _select_delimiter_in_dropdown(self):
        """Point the delimiter dropdown at the current self.delimiter."""
        for index, (delim, _) in enumerate(self.delimiter_options):
            if delim == self.delimiter:
                self.delimiter_dropdown.current(index)
                return
        self.delimiter_dropdown.current(0)

    def _setup_bindings(self):
        """Set up keyboard shortcuts and mouse bindings for the application."""
        self.root.bind("<Left>", lambda e: self.scroll_horizontal(-1))
        self.root.bind("<Right>", lambda e: self.scroll_horizontal(1))
        self.root.bind("<Shift-Left>", lambda e: self.scroll_horizontal(-5))
        self.root.bind("<Shift-Right>", lambda e: self.scroll_horizontal(5))

        # Page navigation with Cmd/Ctrl + arrows (plain arrows scroll the page)
        self.root.bind("<Command-Right>", lambda e: self.next_page())
        self.root.bind("<Command-Left>", lambda e: self.prev_page())
        self.root.bind("<Control-Right>", lambda e: self.next_page())
        self.root.bind("<Control-Left>", lambda e: self.prev_page())

        self.root.bind("<Command-f>", lambda e: self.search_entry.focus_set())
        self.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.root.bind("<Command-o>", lambda e: self.open_file())
        self.root.bind("<Control-o>", lambda e: self.open_file())

        self.tree.bind("<Shift-MouseWheel>", self.on_horizontal_mousewheel)
        self.tree.bind("<Button-1>", lambda e: self.tree.focus_set())
        self.tree.bind("<Double-Button-1>", self._show_detail)

    def _setup_context_menu(self):
        """Right-click menu on the table: view record / copy cell / row / column."""
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="View Record…", command=self._show_detail)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Cell", command=self._copy_cell)
        self.context_menu.add_command(label="Copy Row", command=self._copy_row)
        self.context_menu.add_command(label="Copy Column", command=self._copy_column)
        self._ctx_item = None
        self._ctx_col = None
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.tree.bind(seq, self._show_context_menu)

    # ------------------------------------------------------- copy / clipboard

    def _show_context_menu(self, event):
        """Pop up the copy menu for the clicked cell."""
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or not col:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        self._ctx_item = row
        self._ctx_col = col
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _display_col_index(self, display_col):
        """Map a Treeview display column like '#2' to a 0-based values index."""
        try:
            return int(display_col[1:]) - 1
        except (ValueError, IndexError):
            return -1

    def _set_clipboard(self, text):
        """Replace the clipboard contents and flash a brief confirmation."""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.search_status.config(text="Copied ✓")

    def _copy_cell(self):
        if not self._ctx_item:
            return
        values = self.tree.item(self._ctx_item, "values")
        i = self._display_col_index(self._ctx_col)
        if 0 <= i < len(values):
            self._set_clipboard(str(values[i]))

    def _copy_row(self):
        if not self._ctx_item:
            return
        values = self.tree.item(self._ctx_item, "values")
        self._set_clipboard("\t".join(str(v) for v in values[1:]))  # skip rownum

    def _copy_column(self):
        i = self._display_col_index(self._ctx_col)
        if i < 0:
            return
        column = []
        for item in self.tree.get_children():
            values = self.tree.item(item, "values")
            if i < len(values):
                column.append(str(values[i]))
        self._set_clipboard("\n".join(column))

    # ------------------------------------------------------------ detail view

    def _show_detail(self, event=None):
        """Open a popup with the full record for the selected (or clicked) row."""
        item = None
        if event is not None:
            clicked = self.tree.identify_row(event.y)
            if clicked:
                item = clicked
        if item is None:
            sel = self.tree.selection()
            item = sel[0] if sel else None
        if not item:
            return
        children = self.tree.get_children()
        try:
            pos = children.index(item)
        except ValueError:
            return
        if pos >= len(self._page_full_rows):
            return
        rownum = self.tree.item(item, "values")[0]
        self._open_detail_window(rownum, self._page_full_rows[pos])

    def _open_detail_window(self, rownum, full_row):
        """Build the scrollable record popup for one row."""
        win = tk.Toplevel(self.root)
        win.title(f"Record — row {rownum}")
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(frame, highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        headers = self.column_headers
        for i, name in enumerate(headers):
            value = str(full_row[i]) if i < len(full_row) else ""
            ttk.Label(inner, text=str(name), font=("Helvetica", 10, "bold")).grid(
                row=i, column=0, sticky="nw", padx=(0, 10), pady=3
            )
            text = tk.Text(inner, width=60, wrap="word", height=min(5, value.count("\n") + 1))
            text.insert("1.0", value)
            text.configure(state="disabled")
            text.grid(row=i, column=1, sticky="ew", pady=3)
            ttk.Button(
                inner, text="Copy", command=lambda v=value: self._set_clipboard(v)
            ).grid(row=i, column=2, padx=5)
        inner.grid_columnconfigure(1, weight=1)

        buttons = ttk.Frame(win, padding=(10, 0, 10, 10))
        buttons.pack(fill=tk.X)
        ttk.Button(
            buttons,
            text="Copy All",
            command=lambda: self._set_clipboard(
                "\n".join(
                    f"{h}\t{full_row[i] if i < len(full_row) else ''}"
                    for i, h in enumerate(headers)
                )
            ),
        ).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side=tk.RIGHT)
        win.geometry("620x520")

    # --------------------------------------------------------- control events

    def _on_delimiter_changed(self, event=None):
        """Handle the delimiter selection change event."""
        selected_display = self.delimiter_dropdown.get()
        for delim, display in self.delimiter_options:
            if display == selected_display:
                self.delimiter = delim
                break
        # Row boundaries don't depend on the delimiter, so cached offsets stay valid
        if self.file_path:
            self._reset_filter_index()
            self._load_page()

    def _on_page_size_changed(self):
        """Handle the page size change event."""
        try:
            new_page_size = int(self.page_size_var.get())
            if new_page_size > 0:
                self.page_size = new_page_size
                self.current_page = 0
                self.page_offsets = {}  # Boundaries depend on page size
                self._reset_filter_index()
                self._save_config()  # persist immediately, regardless of quit method
                if self.file_path:
                    self._load_page()
            else:
                messagebox.showerror("Error", "Page size must be a positive integer.")
        except ValueError:
            messagebox.showerror("Error", "Invalid page size. Please enter a number.")

    def _toggle_expand_columns(self):
        """Toggle between fit-to-window columns and full-content-width columns."""
        self.expand_columns = not self.expand_columns
        self.expand_columns_button.config(
            text="Collapse Columns" if self.expand_columns else "Expand Columns"
        )
        if self.file_path:
            self._load_page()

    def toggle_header(self):
        """Switch between 'first row is a header' and 'first row is data'."""
        self.has_header = not self.no_header_var.get()
        if not self.file_path:
            return
        self._count_token += 1
        self.current_page = 0
        self.page_offsets = {}
        self._reset_filter_index()
        self.total_rows, self.total_is_estimate = self._estimate_total_rows(
            self.file_path
        )
        self._load_page()
        if self.total_is_estimate:
            self._start_count(
                self.file_path, self.encoding, self.delimiter, self.has_header,
                self._count_token,
            )

    def _show_column_selector(self):
        """Open a popup window with checkboxes to show/hide columns."""
        if not self.column_headers:
            messagebox.showinfo("No Data", "Please load a CSV file first.")
            return

        column_window = tk.Toplevel(self.root)
        column_window.title("Show/Hide Columns")
        column_window.transient(self.root)
        column_window.grab_set()

        checkbox_frame = ttk.Frame(column_window)
        checkbox_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        canvas = tk.Canvas(checkbox_frame)
        scrollbar = ttk.Scrollbar(
            checkbox_frame, orient="vertical", command=canvas.yview
        )
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        column_vars = {}
        for i, header in enumerate(self.column_headers):
            var = tk.BooleanVar(value=i not in self.hidden_columns)
            column_vars[i] = var
            ttk.Checkbutton(scrollable_frame, text=header, variable=var).pack(
                anchor="w", padx=5, pady=2
            )

        button_frame = ttk.Frame(column_window)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(
            button_frame, text="Select All",
            command=lambda: [v.set(True) for v in column_vars.values()],
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            button_frame, text="Deselect All",
            command=lambda: [v.set(False) for v in column_vars.values()],
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            button_frame, text="Apply",
            command=lambda: self._apply_column_visibility(column_vars, column_window),
        ).pack(side=tk.RIGHT, padx=5)
        ttk.Button(
            button_frame, text="Cancel", command=column_window.destroy
        ).pack(side=tk.RIGHT, padx=5)

        column_window.update()
        min_width = min(500, column_window.winfo_width())
        min_height = min(400, column_window.winfo_height())
        column_window.minsize(min_width, min_height)
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (min_width // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (min_height // 2)
        column_window.geometry(f"{min_width}x{min_height}+{x}+{y}")

    def _apply_column_visibility(self, column_vars, window):
        """Apply the column visibility settings from checkboxes."""
        self.hidden_columns.clear()
        for col_idx, var in column_vars.items():
            if not var.get():
                self.hidden_columns.add(col_idx)
        window.destroy()
        self._load_page()  # display-only change; file offsets unaffected

    # ----------------------------------------------------------- file opening

    def open_file(self):
        """Open a file dialog and load the selected CSV file."""
        initial = self.last_dir if self.last_dir and os.path.isdir(self.last_dir) else None
        path = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=initial,
        )
        if path:
            self._open_path(path)

    def reload_file(self):
        """Re-read the current file from disk (e.g. it changed externally)."""
        if not self.file_path:
            return
        self._count_token += 1
        self.page_offsets = {}
        self._reset_filter_index()
        self.total_rows, self.total_is_estimate = self._estimate_total_rows(
            self.file_path
        )
        self._load_page()
        if self.total_is_estimate:
            self._start_count(
                self.file_path, self.encoding, self.delimiter, self.has_header,
                self._count_token,
            )

    def _open_path(self, path):
        """Load a file by path (shared by the dialog and the Recent menu)."""
        if not os.path.exists(path):
            messagebox.showerror("Error", f"File not found:\n{path}")
            self._remove_recent(path)
            return

        encoding = self._detect_encoding(path)
        if encoding is None:
            messagebox.showerror("Error", "Could not read the selected file.")
            return

        self._count_token += 1  # invalidate any in-flight background count

        self.file_path = path
        self.encoding = encoding
        self.current_page = 0
        self.hidden_columns.clear()
        self.page_offsets = {}
        self._search_query = None
        self._last_match_row = -1
        self._reset_filter_index()
        self.filter_var.set(False)
        self.filter_active = False
        self.search_status.config(text="")
        self.last_dir = os.path.dirname(os.path.abspath(path))

        self.total_rows, self.total_is_estimate = self._estimate_total_rows(path)
        self._auto_detect_delimiter(path, encoding)
        self._add_recent(path)
        self._save_config()  # persist recent files/last dir even if force-quit
        self._load_page()

        if self.total_is_estimate:
            self._start_count(
                path, self.encoding, self.delimiter, self.has_header, self._count_token
            )

    def _add_recent(self, path):
        """Add path to the front of the recent-files list (deduped, capped)."""
        path = os.path.abspath(path)
        self.recent_files = [path] + [p for p in self.recent_files if p != path]
        self.recent_files = self.recent_files[:8]
        self._refresh_recent_menu()

    def _remove_recent(self, path):
        """Drop a missing path from the recent-files list."""
        path = os.path.abspath(path)
        self.recent_files = [p for p in self.recent_files if p != path]
        self._refresh_recent_menu()

    def _estimate_total_rows(self, path):
        """
        Determine the number of data rows without ever scanning more than a 1 MB
        sample. Returns (count, is_estimate). Exact when the whole file fits in
        the sample, extrapolated otherwise. (None, False) when undeterminable.
        """
        header_rows = 1 if self.has_header else 0
        try:
            filesize = os.path.getsize(path)
            if filesize == 0:
                return 0, False
            sample_size = min(filesize, 1 << 20)  # 1 MB
            with open(path, "rb") as f:
                sample = f.read(sample_size)
        except OSError:
            return None, False

        if sample_size == filesize:
            text = sample.decode(self.encoding, errors="replace")
            rows = sum(1 for _ in csv.reader(io.StringIO(text)))
            return max(0, rows - header_rows), False

        newlines = sample.count(b"\n")
        if newlines == 0:
            return None, False
        bytes_per_line = sample_size / newlines
        est_lines = filesize / bytes_per_line
        return max(0, int(est_lines) - header_rows), True

    def _detect_encoding(self, path):
        """
        Return the first encoding that can decode a sample of the file, or None
        if it can't be read. latin-1 is the last resort (it decodes any bytes),
        so a readable file is never reported as undecodable.
        """
        try:
            with open(path, "rb") as f:
                sample = f.read(65536)
        except OSError:
            return None

        newline = sample.rfind(b"\n")
        if newline != -1:
            sample = sample[: newline + 1]

        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                sample.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                continue
        return "latin-1"

    def _auto_detect_delimiter(self, path, encoding):
        """Sniff the delimiter from a sample and sync self.delimiter + dropdown."""
        try:
            with open(path, "r", newline="", encoding=encoding) as f:
                sample = f.read(8192)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            detected = dialect.delimiter
        except (csv.Error, OSError):
            return

        for index, (delim, _) in enumerate(self.delimiter_options):
            if delim == detected:
                self.delimiter = delim
                self.delimiter_dropdown.current(index)
                break

    # ------------------------------------------------------- background count

    def _start_count(self, path, encoding, delimiter, has_header, token):
        """Spawn a daemon thread to count rows exactly without blocking the UI."""
        threading.Thread(
            target=self._count_rows_worker,
            args=(path, encoding, delimiter, has_header, token),
            daemon=True,
        ).start()

    def _count_rows_worker(self, path, encoding, delimiter, has_header, token):
        """Count data rows exactly (off the UI thread); post the result on a queue."""
        try:
            count = 0
            with open(path, "r", newline="", encoding=encoding, errors="replace") as f:
                for _ in csv.reader(f, delimiter=delimiter):
                    count += 1
            self._count_queue.put((token, max(0, count - (1 if has_header else 0))))
        except (OSError, csv.Error):
            pass

    def _poll_count_queue(self):
        """Apply any finished background count for the current file, then reschedule."""
        try:
            while True:
                token, count = self._count_queue.get_nowait()
                if token == self._count_token:
                    self.total_rows = count
                    self.total_is_estimate = False
                    if self.file_path and not self.filter_active:
                        self.page_label.config(
                            text=self._page_label_text(len(self.tree.get_children()))
                        )
        except queue.Empty:
            pass
        self.root.after(300, self._poll_count_queue)

    # --------------------------------------------------------------- paging

    def _load_page(self):
        """
        Load the current page (filtered or not), surfacing read/parse problems
        as a dialog instead of crashing.
        """
        if not self.file_path:
            return
        try:
            if self.filter_active:
                self._render_filter_page()
            else:
                self._render_page()
        except StopIteration:
            messagebox.showerror("Empty file", "This file has no data to display.")
            self._reset_view()
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            messagebox.showerror("Error", f"Could not read the file:\n{exc}")
            self._reset_view()

    def _row_iter(self, f):
        """
        Yield lines from f while recording the byte offset after each line in
        self._byte_pos, so once csv.reader yields a complete row, self._byte_pos
        is the start of the next row. readline() keeps tell() accurate (the
        read-ahead file iterator does not).
        """
        while True:
            line = f.readline()
            if not line:
                return
            self._byte_pos = f.tell()
            yield line

    def _read_header_and_seek(self, f):
        """
        Read (or synthesize) the header, set page_offsets[0], and return a reader
        positioned at the start of the data. Used by both the normal and filter
        render paths.
        """
        reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
        if self.has_header:
            header = next(reader)
            self.column_headers = header
            self.page_offsets.setdefault(0, self._byte_pos)
            return header, reader
        # No header: peek the first data row only to learn the column count,
        # then rewind so it is rendered as data.
        first = next(reader)
        header = [f"Column {i + 1}" for i in range(len(first))]
        self.column_headers = header
        self.page_offsets.setdefault(0, 0)
        f.seek(0)
        self._byte_pos = 0
        reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
        return header, reader

    def _render_page(self):
        """Render the current (unfiltered) page using cached byte offsets."""
        with open(self.file_path, "r", newline="", encoding=self.encoding,
                  errors="replace") as f:
            header, reader = self._read_header_and_seek(f)

            if self.current_page in self.page_offsets:
                offset = self.page_offsets[self.current_page]
                f.seek(offset)
                self._byte_pos = offset
                reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
            else:
                skipped = 0
                for _ in range(self.current_page * self.page_size):
                    try:
                        next(reader)
                    except StopIteration:
                        break
                    skipped += 1
                    if skipped % self.page_size == 0:
                        self.page_offsets.setdefault(
                            skipped // self.page_size, self._byte_pos
                        )

            row_base = self.current_page * self.page_size
            rows = []
            next_page_offset = None
            self.has_next_page = False
            count = 0
            for row in reader:
                if count < self.page_size:
                    padded = row + [""] * (len(header) - len(row))
                    rows.append((row_base + count, padded))
                    count += 1
                    if count == self.page_size:
                        next_page_offset = self._byte_pos
                else:
                    self.has_next_page = True
                    break

            if self.has_next_page and next_page_offset is not None:
                self.page_offsets[self.current_page + 1] = next_page_offset

        self._display(header, rows)

    def _render_filter_page(self):
        """Render a page of only the rows matching the active filter."""
        needed = (self.current_page + 1) * self.page_size
        self._ensure_filter_matches(needed)
        start = self.current_page * self.page_size
        page = self.filter_matches[start:needed]
        # There is a next page if we already have more matches, or if more might
        # still be found further in the file.
        self.has_next_page = len(self.filter_matches) > needed or (
            not self.filter_exhausted and len(self.filter_matches) >= needed
        )
        header = self.column_headers
        self._display(header, page, filtered=True)

    def _ensure_filter_matches(self, needed):
        """
        Scan forward (resuming where we left off) until at least `needed` matches
        are collected or the file is exhausted. Runs on the UI thread with a busy
        cursor; bounded by how far the user pages, not the whole file up front.
        """
        if self.filter_exhausted or len(self.filter_matches) >= needed:
            return
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            with open(self.file_path, "r", newline="", encoding=self.encoding,
                      errors="replace") as f:
                if self.filter_scan_offset is not None:
                    f.seek(self.filter_scan_offset)
                    self._byte_pos = self.filter_scan_offset
                    idx = self.filter_scan_idx
                    reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
                else:
                    reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
                    if self.has_header:
                        next(reader)
                    self.page_offsets.setdefault(0, self._byte_pos)
                    idx = 0

                header_len = len(self.column_headers)
                for row in reader:
                    if any(self.filter_query in cell.lower() for cell in row):
                        padded = row + [""] * (header_len - len(row))
                        self.filter_matches.append((idx, padded))
                        if len(self.filter_matches) >= needed:
                            self.filter_scan_offset = self._byte_pos
                            self.filter_scan_idx = idx + 1
                            return
                    idx += 1
                self.filter_exhausted = True
                self.filter_scan_offset = self._byte_pos
                self.filter_scan_idx = idx
        finally:
            self.root.config(cursor="")

    def _reset_filter_index(self):
        """Clear the lazily-built filter match index."""
        self.filter_matches = []
        self.filter_scan_offset = None
        self.filter_scan_idx = 0
        self.filter_exhausted = False

    def _display(self, header, rows, filtered=False):
        """
        Render a list of (abs_idx, full_padded_row) into the treeview, including
        the row-number column, alternating colors, content-fit widths, and the
        page indicator. Shared by the normal and filtered render paths.
        """
        self.column_visibility_button.config(state="normal")
        self.tree.delete(*self.tree.get_children())

        visible_columns = [
            i for i in range(len(header)) if i not in self.hidden_columns
        ]
        columns = [self.ROWNUM_ID] + visible_columns
        self.tree["columns"] = columns
        self.tree.heading(self.ROWNUM_ID, text="#")
        self.tree.column(self.ROWNUM_ID, anchor="e", stretch=False)
        for i in visible_columns:
            self.tree.heading(i, text=str(header[i]))
            self.tree.column(i, anchor="w")

        self._page_full_rows = []
        sample_rows = []
        for position, (abs_idx, full_row) in enumerate(rows):
            visible_row = [full_row[i] for i in visible_columns]
            values = [abs_idx + 1] + visible_row
            tag = "odd" if position % 2 else "even"
            self.tree.insert("", "end", values=values, tags=(tag,))
            self._page_full_rows.append(full_row)
            if len(sample_rows) < 300:
                sample_rows.append(values)

        self.tree.tag_configure(
            "even", background=self.odd_row_color, foreground=self.row_text_color
        )
        self.tree.tag_configure(
            "odd", background=self.even_row_color, foreground=self.row_text_color
        )
        self._apply_column_widths(columns, header, sample_rows)

        self.page_label.config(text=self._page_label_text(len(rows), filtered=filtered))
        self._update_nav_buttons()

    def _apply_column_widths(self, columns, header, sample_rows):
        """
        Size each column to fit its header and sampled cell content. Fit mode
        (expand_columns False) caps width and stretches to fill the window;
        expanded mode uses full content width (horizontal scroll instead).
        """
        font = tkfont.nametofont("TkDefaultFont")
        pad = 24
        min_w = 50
        cap = 1200 if self.expand_columns else 360
        for pos, colid in enumerate(columns):
            cell_w = max((font.measure(str(r[pos])) for r in sample_rows), default=0)
            if colid == self.ROWNUM_ID:
                content = max(font.measure("#"), cell_w)
                self.tree.column(
                    colid, width=min(160, max(40, content + pad)), stretch=False
                )
                continue
            head_w = font.measure(str(header[colid]))
            content = max(head_w, cell_w)
            width = min(cap, max(min_w, content + pad))
            self.tree.column(colid, width=width, stretch=not self.expand_columns)

    def _page_label_text(self, row_count, filtered=False):
        """Build the page indicator text for the current (filtered) page."""
        if row_count == 0:
            return "No matching rows" if filtered else "No rows"
        start = self.current_page * self.page_size + 1
        end = self.current_page * self.page_size + row_count
        if filtered:
            total = len(self.filter_matches)
            more = "" if self.filter_exhausted else "+"
            return f"Matches {start:,}–{end:,} of {total:,}{more}"
        label = f"Rows {start:,}–{end:,}"
        if self.total_rows is not None:
            approx = "~" if self.total_is_estimate else ""
            label += f" of {approx}{self.total_rows:,}"
        return label

    def _update_nav_buttons(self):
        """Enable/disable the page nav buttons based on the current position."""
        self.prev_button.config(
            state="normal" if self.current_page > 0 else "disabled"
        )
        self.next_button.config(
            state="normal" if self.has_next_page else "disabled"
        )

    def _reset_view(self):
        """Clear the view back to an empty state after a failed load."""
        self.file_path = None
        self._count_token += 1
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = ()
        self.column_headers = []
        self._page_full_rows = []
        self.has_next_page = False
        self.current_page = 0
        self.page_offsets = {}
        self.total_rows = None
        self.total_is_estimate = False
        self.filter_active = False
        self.filter_var.set(False)
        self._reset_filter_index()
        self.page_label.config(text="No file loaded")
        self.column_visibility_button.config(state="disabled")
        self._update_nav_buttons()

    def next_page(self):
        """Navigate to the next page of data if available."""
        if self.has_next_page:
            self.current_page += 1
            self._load_page()

    def prev_page(self):
        """Navigate to the previous page of data if available."""
        if self.current_page > 0:
            self.current_page -= 1
            self._load_page()

    # --------------------------------------------------------- search / jump

    def go_to_row(self, event=None):
        """Jump to (and select) a 1-based row number from the entry box."""
        if not self.file_path:
            return
        try:
            row = int(self.row_var.get())
        except ValueError:
            messagebox.showerror("Error", "Enter a row number.")
            return
        if row < 1:
            row = 1
        if (
            not self.filter_active
            and self.total_rows is not None
            and not self.total_is_estimate
        ):
            row = min(row, max(1, self.total_rows))
        self._go_to_row(row)

    def _go_to_row(self, row_1based):
        """Navigate to the page holding a 1-based row and select it."""
        self.current_page = (row_1based - 1) // self.page_size
        self._load_page()
        if not self.file_path:
            return
        self._select_row_in_view((row_1based - 1) % self.page_size)

    def _select_row_in_view(self, local_index):
        """Select, focus, and scroll to a row by its position on the current page."""
        children = self.tree.get_children()
        if 0 <= local_index < len(children):
            item = children[local_index]
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)

    def toggle_filter(self):
        """Turn the row filter on/off, using the current search text as the query."""
        if not self.file_path:
            self.filter_var.set(False)
            return
        if self.filter_var.get():
            query = self.search_var.get()
            if not query:
                self.filter_var.set(False)
                self.search_status.config(text="Enter text to filter")
                return
            self.filter_active = True
            self.filter_query = query.lower()
            self._reset_filter_index()
            self.current_page = 0
            self._load_page()
        else:
            self.filter_active = False
            self.filter_query = None
            self.current_page = 0
            self._load_page()

    def find_next(self, event=None):
        """Start a background search for the next row containing the search text."""
        if not self.file_path or self._searching:
            return
        query = self.search_var.get()
        if not query:
            return
        if query != self._search_query:
            self._search_query = query
            self._last_match_row = -1

        start = self._last_match_row + 1
        start_page = max(
            (p for p in self.page_offsets if p * self.page_size <= start), default=0
        )
        start_offset = self.page_offsets.get(start_page, 0)
        start_offset_idx = start_page * self.page_size if start_offset else 0

        self._search_token += 1
        token = self._search_token
        self._search_cancel = threading.Event()
        self._set_searching(True)
        threading.Thread(
            target=self._search_worker,
            args=(
                self.file_path, self.encoding, self.delimiter, query.lower(),
                self.has_header, start, start_offset, start_offset_idx,
                self._search_cancel, token,
            ),
            daemon=True,
        ).start()

    def _search_worker(self, path, encoding, delimiter, query_lower, has_header,
                       start, start_offset, start_offset_idx, cancel, token):
        """Run the (cancelable) file-wide scan on a worker thread."""
        try:
            result = scan_for_match(
                path, encoding, delimiter, query_lower, has_header, start,
                None, start_offset, start_offset_idx, cancel,
            )
            wrapped = False
            if result is None and start > 0:
                result = scan_for_match(
                    path, encoding, delimiter, query_lower, has_header, 0, start,
                    0, 0, cancel,
                )
                wrapped = True
            self._search_queue.put((token, result, wrapped))
        except (OSError, csv.Error):
            self._search_queue.put((token, None, False))

    def _poll_search_queue(self):
        """Apply a finished search result (for the current search), then reschedule."""
        try:
            while True:
                token, result, wrapped = self._search_queue.get_nowait()
                if token != self._search_token:
                    continue
                self._set_searching(False)
                if result == "cancelled":
                    self.search_status.config(text="Cancelled")
                elif result is None:
                    self.search_status.config(text="Not found")
                    self._last_match_row = -1
                else:
                    self._last_match_row = result
                    suffix = " (wrapped)" if wrapped else ""
                    self.search_status.config(text=f"Row {result + 1:,}{suffix}")
                    self._go_to_row(result + 1)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_search_queue)

    def cancel_search(self):
        """Request cancellation of the in-flight search."""
        if self._searching:
            self._search_cancel.set()

    def _set_searching(self, on):
        """Reflect the searching state in the buttons, status, and cursor."""
        self._searching = on
        self.find_button.config(state="disabled" if on else "normal")
        self.cancel_button.config(state="normal" if on else "disabled")
        if on:
            self.search_status.config(text="Searching…")
            self.root.config(cursor="watch")
        else:
            self.root.config(cursor="")


if __name__ == "__main__":
    root = tk.Tk()
    app = LazyCSVViewerGUI(root)
    root.mainloop()
