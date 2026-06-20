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
        self.expand_columns = False  # False = fit-to-window, True = full content width
        self.hidden_columns = set()  # Track hidden columns
        self.column_headers = []  # Store column headers for reference
        self.delimiter_options = [
            (",", "Comma (,)"),
            ("\t", "Tab (\\t)"),
            (";", "Semicolon (;)"),
            ("|", "Pipe (|)"),
            (" ", "Space ( )"),
        ]

        # Search / navigation state
        self._search_query = None  # Last query, to know when to restart a search
        self._last_match_row = -1  # Absolute index of the last search match

        # Background row-count state (exact count for big files, off the UI thread)
        self._count_queue = queue.Queue()
        self._count_token = 0  # Bumped per file to ignore stale worker results

        # Persisted settings (window size, recent files, last delimiter, ...)
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

        # Nav buttons start disabled until a file is loaded
        self._update_nav_buttons()

        # Restore window geometry (or fill the screen by default)
        self._apply_initial_geometry()

        # Save settings on close; poll for background row-count results
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._poll_count_queue)

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
            # If detection fails for any reason, fall back to the light palette
            return False

    def _apply_theme(self):
        """
        Choose row/text colors based on the OS appearance and apply them to
        the Treeview style. Row tag colors are stored on self for use when
        rows are inserted in _render_page().
        """
        if self._detect_dark_mode():
            # Dark mode: dark striped rows with light text
            self.odd_row_color = "#3A3A3A"  # Lighter gray for odd rows
            self.even_row_color = "#2B2B2B"  # Darker gray for even rows
            self.row_text_color = "#E8E8E8"  # Near-white text
            field_bg = "#1E1E1E"  # Empty area below the rows
            selected_bg = "#0A84FF"  # macOS system blue
        else:
            # Light mode: original light striped rows with dark text
            self.odd_row_color = "#F0F0F0"  # Light gray for odd rows
            self.even_row_color = "#FFFFFF"  # White for even rows
            self.row_text_color = "#000000"  # Black text
            field_bg = "#FFFFFF"
            selected_bg = "#0078D7"

        # Apply the body colors. tag foreground (set in _render_page) takes
        # precedence per-row, but setting it here too keeps the empty area
        # and any untagged rows consistent.
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

    # ----------------------------------------------------------- scroll helpers

    def on_horizontal_mousewheel(self, event):
        """
        Handle horizontal scrolling with the mousewheel when Shift is pressed.

        Args:
            event: The mousewheel event

        Returns:
            str: "break" to prevent default behavior
        """
        # Windows and macOS have different mousewheel directions
        direction = -1 if event.delta > 0 else 1
        amount = direction * (self.scroll_speed / 1000)
        current = self.tree.xview()[0]

        # Ensure scrolling stays within bounds (0 to 1)
        self.tree.xview_moveto(max(0, min(1, current + amount)))
        return "break"  # Prevent default behavior

    def scroll_horizontal(self, units):
        """
        Scroll the treeview horizontally by the specified number of units.

        Args:
            units: Number of units to scroll (positive for right, negative for left)
        """
        # Calculate scroll amount based on number of units
        amount = units * (self.scroll_speed / 100)
        current = self.tree.xview()[0]

        # Ensure scrolling stays within bounds (0 to 1)
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
        # Main frame with padding
        self.frame = ttk.Frame(self.root, padding="10")
        self.frame.grid(row=0, column=0, sticky="nsew")

        # --- Top controls (file / delimiter / page size / columns) ------------
        top_controls = ttk.Frame(self.frame)
        top_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=5)

        self.open_button = ttk.Button(
            top_controls, text="Open CSV File", command=self.open_file
        )
        self.open_button.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top_controls, text="Delimiter:").pack(side=tk.LEFT, padx=(10, 0))
        self.delimiter_var = tk.StringVar(value=self.delimiter)
        self.delimiter_dropdown = ttk.Combobox(
            top_controls, textvariable=self.delimiter_var, state="readonly", width=15
        )
        self.delimiter_dropdown["values"] = [
            display for _, display in self.delimiter_options
        ]
        self._select_delimiter_in_dropdown()
        self.delimiter_dropdown.pack(side=tk.LEFT, padx=5)
        self.delimiter_dropdown.bind("<<ComboboxSelected>>", self._on_delimiter_changed)

        ttk.Label(top_controls, text="Page Size:").pack(side=tk.LEFT, padx=(20, 0))
        self.page_size_var = tk.StringVar(value=str(self.page_size))
        self.page_size_entry = ttk.Entry(
            top_controls, textvariable=self.page_size_var, width=6
        )
        self.page_size_entry.pack(side=tk.LEFT, padx=5)
        self.page_size_entry.bind("<Return>", lambda e: self._on_page_size_changed())
        self.apply_page_size_button = ttk.Button(
            top_controls, text="Apply", command=self._on_page_size_changed
        )
        self.apply_page_size_button.pack(side=tk.LEFT)

        self.expand_columns_button = ttk.Button(
            top_controls, text="Expand Columns", command=self._toggle_expand_columns
        )
        self.expand_columns_button.pack(side=tk.LEFT, padx=(10, 0))

        self.column_visibility_button = ttk.Button(
            top_controls,
            text="Show/Hide Columns",
            command=self._show_column_selector,
            state="disabled",  # Disabled until a file is loaded
        )
        self.column_visibility_button.pack(side=tk.LEFT, padx=(10, 0))

        # --- Second controls row (search / jump) ------------------------------
        search_controls = ttk.Frame(self.frame)
        search_controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))

        ttk.Label(search_controls, text="Search:").pack(side=tk.LEFT, padx=(0, 0))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            search_controls, textvariable=self.search_var, width=28
        )
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", self.find_next)
        self.find_button = ttk.Button(
            search_controls, text="Find Next", command=self.find_next
        )
        self.find_button.pack(side=tk.LEFT)
        self.search_status = ttk.Label(search_controls, text="", width=18)
        self.search_status.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(search_controls, text="Go to row:").pack(side=tk.LEFT, padx=(20, 0))
        self.row_var = tk.StringVar()
        self.row_entry = ttk.Entry(search_controls, textvariable=self.row_var, width=10)
        self.row_entry.pack(side=tk.LEFT, padx=5)
        self.row_entry.bind("<Return>", self.go_to_row)
        self.go_button = ttk.Button(
            search_controls, text="Go", command=self.go_to_row
        )
        self.go_button.pack(side=tk.LEFT)

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

        # Configure grid weights for proper resizing
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
        # Horizontal scrolling with arrow keys
        self.root.bind("<Left>", lambda e: self.scroll_horizontal(-1))
        self.root.bind("<Right>", lambda e: self.scroll_horizontal(1))
        self.root.bind("<Shift-Left>", lambda e: self.scroll_horizontal(-5))
        self.root.bind("<Shift-Right>", lambda e: self.scroll_horizontal(5))

        # Page navigation with Cmd/Ctrl + arrows (plain arrows scroll the page)
        self.root.bind("<Command-Right>", lambda e: self.next_page())
        self.root.bind("<Command-Left>", lambda e: self.prev_page())
        self.root.bind("<Control-Right>", lambda e: self.next_page())
        self.root.bind("<Control-Left>", lambda e: self.prev_page())

        # Focus the search box with Cmd/Ctrl+F
        self.root.bind("<Command-f>", lambda e: self.search_entry.focus_set())
        self.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())

        # Open with Cmd/Ctrl+O
        self.root.bind("<Command-o>", lambda e: self.open_file())
        self.root.bind("<Control-o>", lambda e: self.open_file())

        # Horizontal scroll with Shift+wheel, and focus the tree on click
        self.tree.bind("<Shift-MouseWheel>", self.on_horizontal_mousewheel)
        self.tree.bind("<Button-1>", lambda e: self.tree.focus_set())

    def _setup_context_menu(self):
        """Right-click menu on the table: copy cell / row / column."""
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Copy Cell", command=self._copy_cell)
        self.context_menu.add_command(label="Copy Row", command=self._copy_row)
        self.context_menu.add_command(label="Copy Column", command=self._copy_column)
        self._ctx_item = None
        self._ctx_col = None
        # Right-click is Button-3 on most platforms, Button-2 / Ctrl-click on macOS
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
        # Skip the leading row-number column
        self._set_clipboard("\t".join(str(v) for v in values[1:]))

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
            self._load_page()

    def _on_page_size_changed(self):
        """Handle the page size change event."""
        try:
            new_page_size = int(self.page_size_var.get())
            if new_page_size > 0:
                self.page_size = new_page_size
                self.current_page = 0  # Reset to first page
                self.page_offsets = {}  # Boundaries depend on page size
                if self.file_path:  # Only reload if a file is already loaded
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
            cb = ttk.Checkbutton(scrollable_frame, text=header, variable=var)
            cb.pack(anchor="w", padx=5, pady=2)

        button_frame = ttk.Frame(column_window)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        select_all_btn = ttk.Button(
            button_frame,
            text="Select All",
            command=lambda: [var.set(True) for var in column_vars.values()],
        )
        select_all_btn.pack(side=tk.LEFT, padx=5)
        deselect_all_btn = ttk.Button(
            button_frame,
            text="Deselect All",
            command=lambda: [var.set(False) for var in column_vars.values()],
        )
        deselect_all_btn.pack(side=tk.LEFT, padx=5)
        apply_btn = ttk.Button(
            button_frame,
            text="Apply",
            command=lambda: self._apply_column_visibility(column_vars, column_window),
        )
        apply_btn.pack(side=tk.RIGHT, padx=5)
        cancel_btn = ttk.Button(
            button_frame, text="Cancel", command=column_window.destroy
        )
        cancel_btn.pack(side=tk.RIGHT, padx=5)

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
        # Hidden columns only affect display, not file offsets -> keep page_offsets
        self._load_page()

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

        # A new file invalidates any in-flight background count
        self._count_token += 1

        self.file_path = path
        self.encoding = encoding
        self.current_page = 0
        self.hidden_columns.clear()
        self.page_offsets = {}
        self._search_query = None
        self._last_match_row = -1
        self.search_status.config(text="")
        self.last_dir = os.path.dirname(os.path.abspath(path))

        # Bounded estimate first (never a full scan -> instant open)
        self.total_rows, self.total_is_estimate = self._estimate_total_rows(path)

        self._auto_detect_delimiter(path, encoding)
        self._add_recent(path)
        self._load_page()

        # If the total is only an estimate, refine it to an exact count in the
        # background so the UI stays responsive on huge files.
        if self.total_is_estimate:
            self._start_count(path, self.encoding, self.delimiter, self._count_token)

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
        Determine the number of data rows (excluding the header) without ever
        scanning more than a 1 MB sample. Returns (count, is_estimate).

        If the whole file fits in the sample it is parsed for an EXACT count
        (is_estimate False). Otherwise the count is extrapolated from the sample
        and file size via newline density (is_estimate True) -- approximate
        because multi-line quoted fields inflate the newline count, but it never
        touches more than 1 MB regardless of file size. Returns (None, False)
        when no count can be derived.
        """
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
            # Whole file is in the sample -> parse it for an exact row count.
            # Cheap (<=1 MB) and correct even with multi-line quoted fields.
            # Row boundaries don't depend on the delimiter, so the default is fine.
            text = sample.decode(self.encoding, errors="replace")
            rows = sum(1 for _ in csv.reader(io.StringIO(text)))
            return max(0, rows - 1), False  # minus the header row

        newlines = sample.count(b"\n")
        if newlines == 0:
            return None, False  # no line boundary in 1 MB -> can't extrapolate
        bytes_per_line = sample_size / newlines
        est_lines = filesize / bytes_per_line
        return max(0, int(est_lines) - 1), True  # minus the header row

    def _detect_encoding(self, path):
        """
        Return the first encoding that can decode a sample of the file, or
        None if the file can't be read at all. latin-1 is the last resort
        because it can decode any byte sequence, so this never reports a
        readable file as undecodable.
        """
        try:
            with open(path, "rb") as f:
                sample = f.read(65536)
        except OSError:
            return None

        # Trim to the last complete line so a multi-byte character split at the
        # sample boundary doesn't cause a false decode failure.
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
        """
        Sniff the delimiter from a sample and sync self.delimiter and the
        dropdown to match. Leaves the current selection untouched if detection
        is inconclusive. Space is excluded from sniffing to avoid false hits on
        prose; it stays available for manual selection.
        """
        try:
            with open(path, "r", newline="", encoding=encoding) as f:
                sample = f.read(8192)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            detected = dialect.delimiter
        except (csv.Error, OSError):
            return  # keep whatever delimiter is currently selected

        for index, (delim, _) in enumerate(self.delimiter_options):
            if delim == detected:
                self.delimiter = delim
                self.delimiter_dropdown.current(index)
                break

    # ------------------------------------------------------- background count

    def _start_count(self, path, encoding, delimiter, token):
        """Spawn a daemon thread to count rows exactly without blocking the UI."""
        thread = threading.Thread(
            target=self._count_rows_worker,
            args=(path, encoding, delimiter, token),
            daemon=True,
        )
        thread.start()

    def _count_rows_worker(self, path, encoding, delimiter, token):
        """Count data rows exactly (off the UI thread); post the result on a queue."""
        try:
            count = 0
            with open(path, "r", newline="", encoding=encoding, errors="replace") as f:
                reader = csv.reader(f, delimiter=delimiter)
                for _ in reader:
                    count += 1
            self._count_queue.put((token, max(0, count - 1)))  # minus the header
        except (OSError, csv.Error):
            pass  # leave the estimate in place

    def _poll_count_queue(self):
        """Apply any finished background count for the current file, then reschedule."""
        try:
            while True:
                token, count = self._count_queue.get_nowait()
                if token == self._count_token:  # ignore results for old files
                    self.total_rows = count
                    self.total_is_estimate = False
                    if self.file_path:
                        self.page_label.config(
                            text=self._page_label_text(len(self.tree.get_children()))
                        )
        except queue.Empty:
            pass
        self.root.after(300, self._poll_count_queue)

    # --------------------------------------------------------------- paging

    def _load_page(self):
        """
        Load the current page, surfacing any read/parse problem as a dialog
        instead of crashing. Delegates the actual rendering to _render_page().
        """
        if not self.file_path:
            return
        try:
            self._render_page()
        except StopIteration:
            # next(reader) on the header failed -> the file has no rows
            messagebox.showerror("Empty file", "This file has no data to display.")
            self._reset_view()
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            messagebox.showerror("Error", f"Could not read the file:\n{exc}")
            self._reset_view()

    def _row_iter(self, f):
        """
        Yield lines from f while recording the byte offset after each line in
        self._byte_pos. csv.reader pulls lines through this, so once it yields a
        complete row, self._byte_pos is the offset of the start of the next row.
        readline() (not the file iterator) is used because it keeps tell()
        accurate, which the read-ahead file iterator does not.
        """
        while True:
            line = f.readline()
            if not line:
                return
            self._byte_pos = f.tell()
            yield line

    def _render_page(self):
        """
        Render the current page of CSV data into the treeview, handling
        pagination and column setup. Navigation uses cached byte offsets so a
        page load seeks straight to the page instead of re-scanning from the top.
        """
        with open(self.file_path, "r", newline="", encoding=self.encoding) as f:
            reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
            header = next(reader)  # Read header row (StopIteration -> empty file)
            self.column_headers = header  # Store for reference
            # The position right after the header is the start of page 0's data
            self.page_offsets.setdefault(0, self._byte_pos)

            # Jump straight to the current page if we know where it starts;
            # otherwise fall back to a sequential skip (caching offsets en route).
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

            # Enable column visibility button since we now have column data
            self.column_visibility_button.config(state="normal")

            # Clear existing data from treeview
            self.tree.delete(*self.tree.get_children())

            # Get indices of visible columns and prepend the row-number column
            visible_columns = [
                i for i in range(len(header)) if i not in self.hidden_columns
            ]
            columns = [self.ROWNUM_ID] + visible_columns
            self.tree["columns"] = columns
            self.tree.heading(self.ROWNUM_ID, text="#")
            self.tree.column(self.ROWNUM_ID, anchor="e", stretch=False)
            for i in visible_columns:
                self.tree.heading(i, text=header[i])
                self.tree.column(i, anchor="w")

            # Load rows for current page
            row_base = self.current_page * self.page_size
            row_count = 0
            next_page_offset = None
            self.has_next_page = False
            sample_rows = []  # capped sample used to size columns to content
            for row in reader:
                if row_count < self.page_size:
                    # Pad short rows so every visible column has a value
                    padded_row = row + [""] * (len(header) - len(row))
                    visible_row = [padded_row[i] for i in visible_columns]
                    values = [row_base + row_count + 1] + visible_row
                    tag = "odd" if row_count % 2 else "even"
                    self.tree.insert("", "end", values=values, tags=(tag,))
                    if len(sample_rows) < 300:
                        sample_rows.append(values)
                    row_count += 1
                    # Capture the page boundary before peeking at the next row
                    if row_count == self.page_size:
                        next_page_offset = self._byte_pos
                else:
                    # More rows exist beyond this page
                    self.has_next_page = True
                    break

            # Remember where the next page starts so Next is an O(1) seek
            if self.has_next_page and next_page_offset is not None:
                self.page_offsets[self.current_page + 1] = next_page_offset

            # Configure row tags for alternating colors (readable in either palette)
            self.tree.tag_configure(
                "even", background=self.odd_row_color, foreground=self.row_text_color
            )
            self.tree.tag_configure(
                "odd", background=self.even_row_color, foreground=self.row_text_color
            )

            # Size columns to their content (and fill the window in fit mode)
            self._apply_column_widths(columns, header, sample_rows)

        # Keep the row indicator and nav buttons in sync with the loaded page
        self.page_label.config(text=self._page_label_text(row_count))
        self._update_nav_buttons()

    def _apply_column_widths(self, columns, header, sample_rows):
        """
        Size each column to fit its header and sampled cell content. In fit mode
        (expand_columns False) columns are capped and stretch to fill the window;
        in expanded mode they take full content width (horizontal scroll instead).
        """
        font = tkfont.nametofont("TkDefaultFont")
        pad = 24
        min_w = 50
        cap = 1200 if self.expand_columns else 360
        for pos, colid in enumerate(columns):
            cell_w = max(
                (font.measure(str(r[pos])) for r in sample_rows), default=0
            )
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

    def _page_label_text(self, row_count):
        """Build the 'Rows A-B of ~N' indicator for the current page."""
        if row_count == 0:
            return "No rows"
        start = self.current_page * self.page_size + 1
        end = self.current_page * self.page_size + row_count
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
        self._count_token += 1  # invalidate any in-flight background count
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = ()
        self.column_headers = []
        self.has_next_page = False
        self.current_page = 0
        self.page_offsets = {}
        self.total_rows = None
        self.total_is_estimate = False
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
        if self.total_rows is not None and not self.total_is_estimate:
            row = min(row, max(1, self.total_rows))
        self._go_to_row(row)

    def _go_to_row(self, row_1based):
        """Navigate to the page holding a 1-based data row and select it."""
        self.current_page = (row_1based - 1) // self.page_size
        self._load_page()
        if not self.file_path:  # load may have reset the view on error
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

    def find_next(self, event=None):
        """Find the next row containing the search text, scanning the whole file."""
        if not self.file_path:
            return
        query = self.search_var.get()
        if not query:
            return
        if query != self._search_query:
            self._search_query = query
            self._last_match_row = -1

        start = self._last_match_row + 1
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            match = self._scan_for_match(query.lower(), start)
            wrapped = False
            if match is None and start > 0:
                match = self._scan_for_match(query.lower(), 0, stop=start)
                wrapped = True
        finally:
            self.root.config(cursor="")

        if match is None:
            self.search_status.config(text="Not found")
            self._last_match_row = -1
            return
        self._last_match_row = match
        suffix = " (wrapped)" if wrapped else ""
        self.search_status.config(text=f"Row {match + 1:,}{suffix}")
        self._go_to_row(match + 1)

    def _scan_for_match(self, query_lower, start_row, stop=None):
        """
        Return the absolute 0-based data-row index of the next row whose cells
        contain query_lower, scanning from start_row (exclusive upper bound
        `stop`, used for wrap-around). Caches page offsets seen along the way so
        the subsequent navigation is an O(1) seek.
        """
        with open(self.file_path, "r", newline="", encoding=self.encoding,
                  errors="replace") as f:
            # Start from the nearest cached page boundary at or before start_row
            start_page = max(
                (p for p in self.page_offsets if p * self.page_size <= start_row),
                default=0,
            )
            if start_page in self.page_offsets:
                f.seek(self.page_offsets[start_page])
                self._byte_pos = self.page_offsets[start_page]
                reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
                idx = start_page * self.page_size
            else:
                reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
                try:
                    next(reader)  # header
                except StopIteration:
                    return None
                self.page_offsets.setdefault(0, self._byte_pos)
                idx = 0

            for row in reader:
                if idx >= start_row and (stop is None or idx < stop):
                    if any(query_lower in cell.lower() for cell in row):
                        return idx
                if (idx + 1) % self.page_size == 0:
                    self.page_offsets.setdefault(
                        (idx + 1) // self.page_size, self._byte_pos
                    )
                idx += 1
                if stop is not None and idx >= stop:
                    break
        return None


if __name__ == "__main__":
    # Create the main window and start the application
    root = tk.Tk()
    app = LazyCSVViewerGUI(root)
    root.mainloop()
