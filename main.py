import csv
import io
import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class LazyCSVViewerGUI:
    """
    A simple CSV viewer application that loads and displays CSV files page by page.
    Supports navigation between pages and horizontal/vertical scrolling.
    """

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
        self.page_size = 200  # Changed default to 200 rows per page
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
        self.expand_columns = False  # Track state for column expansion
        self.hidden_columns = set()  # Track hidden columns
        self.column_headers = []  # Store column headers for reference
        self.delimiter_options = [
            (",", "Comma (,)"),
            ("\t", "Tab (\\t)"),
            (";", "Semicolon (;)"),
            ("|", "Pipe (|)"),
            (" ", "Space ( )"),
        ]

        # Configure Treeview style and pick a palette that matches the OS appearance
        self.style = ttk.Style()
        self.style.configure("Treeview", rowheight=25)
        self._apply_theme()

        # Setup the UI components and event bindings
        self._setup_widgets()
        self._setup_bindings()

        # Nav buttons start disabled until a file is loaded
        self._update_nav_buttons()

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
        rows are inserted in _load_page().
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

        # Apply the body colors. tag foreground (set in _load_page) takes
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

    def _setup_bindings(self):
        """Set up keyboard shortcuts and mouse bindings for the application."""
        # Add keyboard shortcuts for faster horizontal scrolling
        self.root.bind("<Left>", lambda e: self.scroll_horizontal(-1))
        self.root.bind("<Right>", lambda e: self.scroll_horizontal(1))
        self.root.bind(
            "<Shift-Left>", lambda e: self.scroll_horizontal(-5)
        )  # Faster scrolling with Shift
        self.root.bind(
            "<Shift-Right>", lambda e: self.scroll_horizontal(5)
        )  # Faster scrolling with Shift

        # Bind mousewheel for horizontal scrolling when Shift is pressed
        self.tree.bind("<Shift-MouseWheel>", self.on_horizontal_mousewheel)

        # Focus tree view when clicking on it
        self.tree.bind("<Button-1>", lambda e: self.tree.focus_set())

        self.tree.bind("<ButtonRelease-1>", self.select_item_and_copy)

    def select_item_and_copy(self, event):
        # check if click cell to copy is enabled
        if not self.click_cell_to_copy_var.get():
            return

        cur_item = self.tree.item(self.tree.focus())

        col = self.tree.identify_column(event.x)
        self.root.clipboard_clear()
        self.root.clipboard_append(cur_item["values"][int(col[1:]) - 1])

    def _setup_widgets(self):
        """Create and arrange all UI widgets for the application."""
        # Main frame with padding
        self.frame = ttk.Frame(self.root, padding="10")
        self.frame.grid(row=0, column=0, sticky="nsew")

        # Top controls frame
        top_controls = ttk.Frame(self.frame)
        top_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=5)

        # File open button
        self.open_button = ttk.Button(
            top_controls, text="Open CSV File", command=self.open_file
        )
        self.open_button.pack(side=tk.LEFT, padx=(0, 10))

        # Delimiter selection
        ttk.Label(top_controls, text="Delimiter:").pack(side=tk.LEFT, padx=(10, 0))

        # Delimiter dropdown
        self.delimiter_var = tk.StringVar(value=",")
        self.delimiter_dropdown = ttk.Combobox(
            top_controls, textvariable=self.delimiter_var, state="readonly", width=15
        )

        # Set dropdown values and display text
        self.delimiter_dropdown["values"] = [
            display for _, display in self.delimiter_options
        ]
        self.delimiter_dropdown.current(0)  # Default to Comma
        self.delimiter_dropdown.pack(side=tk.LEFT, padx=5)

        # Bind delimiter change event
        self.delimiter_dropdown.bind("<<ComboboxSelected>>", self._on_delimiter_changed)

        # Page size control
        ttk.Label(top_controls, text="Page Size:").pack(side=tk.LEFT, padx=(20, 0))

        # Create a StringVar for the entry and set to default page size
        self.page_size_var = tk.StringVar(value=str(self.page_size))

        # Entry widget for page size
        self.page_size_entry = ttk.Entry(
            top_controls,
            textvariable=self.page_size_var,
            width=6,
        )
        self.page_size_entry.pack(side=tk.LEFT, padx=5)

        # Apply button for page size changes
        self.apply_page_size_button = ttk.Button(
            top_controls,
            text="Apply",
            command=self._on_page_size_changed,
        )
        self.apply_page_size_button.pack(side=tk.LEFT)

        # Expand all columns toggle button
        self.expand_columns_button = ttk.Button(
            top_controls,
            text="Collapse All Columns",
            command=self._toggle_expand_columns,
        )
        self.expand_columns_button.pack(side=tk.LEFT, padx=(10, 0))

        # Column visibility button
        self.column_visibility_button = ttk.Button(
            top_controls,
            text="Show/Hide Columns",
            command=self._show_column_selector,
            state="disabled",  # Disabled until a file is loaded
        )
        self.column_visibility_button.pack(side=tk.LEFT, padx=(10, 0))

        # Checkbox for copying cell content on click
        self.click_cell_to_copy_var = tk.BooleanVar(value=False)
        self.click_cell_to_copy_checkbox = ttk.Checkbutton(
            top_controls,
            text="Click Cell to Copy",
            variable=self.click_cell_to_copy_var,
        )
        self.click_cell_to_copy_checkbox.pack(side=tk.LEFT, padx=(10, 0))
        # Treeview for displaying CSV data
        self.tree = ttk.Treeview(self.frame, show="headings")
        self.tree.grid(row=1, column=0, columnspan=2, sticky="nsew")

        # Vertical scrollbar
        self.scrollbar = ttk.Scrollbar(
            self.frame, orient="vertical", command=self.tree.yview
        )
        self.scrollbar.grid(row=1, column=2, sticky="ns")

        # Horizontal scrollbar
        self.xscrollbar = ttk.Scrollbar(
            self.frame, orient="horizontal", command=self.tree.xview
        )
        self.xscrollbar.grid(row=2, column=0, columnspan=2, sticky="ew")

        # Connect scrollbars to treeview
        self.tree.configure(
            yscrollcommand=self.scrollbar.set,
            xscrollcommand=self.xscrollbar.set,
        )
        # Navigation buttons
        self.prev_button = ttk.Button(
            self.frame, text="Previous Page", command=self.prev_page
        )
        self.prev_button.grid(row=3, column=0, pady=5)

        # Add page number label between the buttons
        self.page_label = ttk.Label(
            self.frame, text="No file loaded", font=("Helvetica", 10)
        )
        self.page_label.grid(row=3, column=0, columnspan=2, pady=5)

        self.next_button = ttk.Button(
            self.frame, text="Next Page", command=self.next_page
        )
        self.next_button.grid(row=3, column=1, pady=5)

        # Configure grid weights for proper resizing
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(1, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=1)

    def _on_delimiter_changed(self, event=None):
        """Handle the delimiter selection change event."""
        # Get the selected display text
        selected_display = self.delimiter_dropdown.get()

        # Find the corresponding delimiter value
        for delim, display in self.delimiter_options:
            if display == selected_display:
                self.delimiter = delim
                break

        # Reload the current page with the new delimiter if a file is loaded
        if self.file_path:
            self._load_page()

    def _on_page_size_changed(self):
        """Handle the page size change event."""
        try:
            # Get the new page size from the entry widget
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
        """Toggle the expansion of all columns."""
        if self.expand_columns:
            self.expand_columns_button.config(text="Collapse All Columns")
            # Expand all columns to fit their content
            self._load_page()  # Reload to adjust column widths
        else:
            self.expand_columns_button.config(text="Expand All Columns")
            # Reset columns to default width
            for col in self.tree["columns"]:
                self.tree.column(col, width=100, stretch=False)

        self.expand_columns = not self.expand_columns
        # Force update of the display
        self.tree.update()

    def _show_column_selector(self):
        """Open a popup window with checkboxes to show/hide columns."""
        if not self.column_headers:
            messagebox.showinfo("No Data", "Please load a CSV file first.")
            return

        # Create popup window
        column_window = tk.Toplevel(self.root)
        column_window.title("Show/Hide Columns")
        column_window.transient(
            self.root
        )  # Set as a transient window of the main window
        column_window.grab_set()  # Make the window modal

        # Frame for checkboxes with scrollbar
        checkbox_frame = ttk.Frame(column_window)
        checkbox_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Add a canvas with scrollbar for many columns
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

        # Pack the canvas and scrollbar
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Variables to track checkbox states
        column_vars = {}

        # Create a checkbox for each column
        for i, header in enumerate(self.column_headers):
            var = tk.BooleanVar(value=i not in self.hidden_columns)
            column_vars[i] = var
            cb = ttk.Checkbutton(scrollable_frame, text=header, variable=var)
            cb.pack(anchor="w", padx=5, pady=2)

        # Button frame
        button_frame = ttk.Frame(column_window)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        # Select/Deselect All buttons
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

        # Apply and Cancel buttons
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

        # Set minimum size for the window
        column_window.update()
        min_width = min(500, column_window.winfo_width())
        min_height = min(400, column_window.winfo_height())
        column_window.minsize(min_width, min_height)

        # Center the window relative to the main window
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (min_width // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (min_height // 2)
        column_window.geometry(f"{min_width}x{min_height}+{x}+{y}")

    def _apply_column_visibility(self, column_vars, window):
        """Apply the column visibility settings from checkboxes."""
        # Clear current hidden columns set
        self.hidden_columns.clear()

        # Add columns with unchecked boxes to hidden columns
        for col_idx, var in column_vars.items():
            if not var.get():
                self.hidden_columns.add(col_idx)

        # Close the window
        window.destroy()

        # Reload the data with updated column visibility
        self._load_page()

    def open_file(self):
        """Open a file dialog and load the selected CSV file."""
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if not path:
            return

        # Figure out the text encoding before committing to the file
        encoding = self._detect_encoding(path)
        if encoding is None:
            messagebox.showerror("Error", "Could not read the selected file.")
            return

        self.file_path = path
        self.encoding = encoding
        self.current_page = 0  # Reset to first page
        self.hidden_columns.clear()  # Clear hidden columns for new file
        self.page_offsets = {}  # Offsets are file-specific; drop the old ones

        # Estimate the total row count from a bounded sample (never a full scan,
        # so opening a huge file stays instant).
        self.total_rows, self.total_is_estimate = self._estimate_total_rows(path)

        # Auto-detect the delimiter and sync the dropdown before loading
        self._auto_detect_delimiter(path, encoding)
        self._load_page()

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
            # otherwise fall back to a sequential skip from the first data row.
            if self.current_page in self.page_offsets:
                offset = self.page_offsets[self.current_page]
                f.seek(offset)
                self._byte_pos = offset
                reader = csv.reader(self._row_iter(f), delimiter=self.delimiter)
            else:
                for _ in range(self.current_page * self.page_size):
                    try:
                        next(reader)
                    except StopIteration:
                        break

            # Enable column visibility button since we now have column data
            self.column_visibility_button.config(state="normal")

            # Clear existing data from treeview
            self.tree.delete(*self.tree.get_children())

            # Get indices of visible columns
            visible_columns = [
                i for i in range(len(header)) if i not in self.hidden_columns
            ]

            # Setup columns based on header (only visible ones)
            self.tree["columns"] = visible_columns
            for i in visible_columns:
                self.tree.heading(i, text=header[i])
                self.tree.column(i, anchor="w")  # Left-align text

            # Load rows for current page
            row_count = 0
            next_page_offset = None
            self.has_next_page = False
            for row in reader:
                if row_count < self.page_size:
                    # Handle rows with fewer columns than header by padding with empty strings
                    padded_row = row + [""] * (len(header) - len(row))
                    # Only include visible columns in the values
                    visible_row = [padded_row[i] for i in visible_columns]
                    # Insert row with alternating colors
                    tag = "odd" if row_count % 2 else "even"
                    self.tree.insert("", "end", values=visible_row, tags=(tag,))
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

            # Configure row tags for alternating colors (with a readable text
            # color for the current light/dark palette)
            self.tree.tag_configure(
                "even", background=self.odd_row_color, foreground=self.row_text_color
            )
            self.tree.tag_configure(
                "odd", background=self.even_row_color, foreground=self.row_text_color
            )

        # Keep the row indicator and nav buttons in sync with the loaded page
        self.page_label.config(text=self._page_label_text(row_count))
        self._update_nav_buttons()

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


if __name__ == "__main__":
    # Create the main window and start the application
    root = tk.Tk()
    # Set window to full screen by default
    root.state("zoomed")  # Works on Windows
    # Fallback to geometry method for other platforms
    width = root.winfo_screenwidth()
    height = root.winfo_screenheight()
    root.geometry(f"{width}x{height}+0+0")
    app = LazyCSVViewerGUI(root)
    root.mainloop()
