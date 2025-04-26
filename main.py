import csv
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from time import sleep
from threading import Thread


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
        self.scroll_speed = 20  # Controls how fast horizontal scrolling moves
        self.delimiter = "\t"  # Default delimiter (tab)
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

        # Configure Treeview style for striped rows
        self.style = ttk.Style()
        self.style.configure("Treeview", rowheight=25)
        self.style.map("Treeview", background=[("selected", "#0078D7")])

        # Define colors for alternating rows
        self.odd_row_color = "#F0F0F0"  # Light gray for odd rows
        self.even_row_color = "#FFFFFF"  # White for even rows

        # Setup the UI components and event bindings
        self._setup_widgets()
        self._setup_bindings()
        self._open_file_from_thread()

    def _open_file_from_thread(self):
        # using another thread, else the GUI will not open until the sleep is over
        def _open_file():
            sleep(0.5)  # wait for the window to be ready
            self.open_file()

        thread = Thread(target=_open_file)
        thread.start()

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
        self.delimiter_var = tk.StringVar(value="\t")
        self.delimiter_dropdown = ttk.Combobox(
            top_controls, textvariable=self.delimiter_var, state="readonly", width=15
        )

        # Set dropdown values and display text
        self.delimiter_dropdown["values"] = [
            display for _, display in self.delimiter_options
        ]
        self.delimiter_dropdown.current(1)  # Default to Tab
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
        self.page_label = ttk.Label(self.frame, text="Page 1", font=("Helvetica", 10))
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
        if path:
            self.file_path = path
            self.current_page = 0  # Reset to first page
            self.hidden_columns.clear()  # Clear hidden columns for new file
            self._load_page()
            # Enable column visibility button once a file is loaded
            self.column_visibility_button.config(state="normal")

    def _load_page(self):
        """
        Load and display the current page of CSV data in the treeview.
        Handles pagination and column setup.
        """
        if not self.file_path:
            return

        # Calculate starting row based on current page
        start = self.current_page * self.page_size

        with open(self.file_path, "r", newline="", encoding="utf-8") as f:
            # Create CSV reader with selected delimiter
            reader = csv.reader(f, delimiter=self.delimiter)
            header = next(reader)  # Read header row
            self.column_headers = header  # Store for reference

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

            # Skip to current page position
            for _ in range(start):
                try:
                    next(reader)
                except StopIteration:
                    break  # End of file reached while skipping

            # Load rows for current page
            row_count = 0
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
                else:
                    # More rows exist beyond this page
                    self.has_next_page = True
                    break
            else:
                # If loop completed normally (not via break),
                # we've reached the end of the file
                self.has_next_page = False

            # Configure row tags for alternating colors
            self.tree.tag_configure("even", background=self.odd_row_color)
            self.tree.tag_configure("odd", background=self.even_row_color)

    def next_page(self):
        """Navigate to the next page of data if available."""
        if self.has_next_page:
            self.current_page += 1
            self._load_page()
            # Update the page label
            self.page_label.config(text=f"Page {self.current_page + 1}")
        else:
            messagebox.showinfo("End", "You are at the end of the file.")

    def prev_page(self):
        """Navigate to the previous page of data if available."""
        if self.current_page > 0:
            self.current_page -= 1
            self._load_page()
            # Update the page label
            self.page_label.config(text=f"Page {self.current_page + 1}")
        else:
            messagebox.showinfo("Start", "You are at the start of the file.")


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
