# Lazy CSV Viewer

A lightweight, efficient CSV viewer application that loads and displays CSV files page by page. Built with Python and Tkinter.

## Features

- **Page-by-page loading**: Open huge CSV files in a fraction of a second — only the current page is read, and Next/Previous seek directly to each page
- **Row count**: Shows `Rows A–B of N`, using a bounded sample estimate (`~N`) that is refined to the exact total in the background
- **Search**: File-wide "Find Next" with wrap-around; runs in the background and can be cancelled
- **Filter**: Show only rows matching a query, paged across the whole file
- **Go to row**: Jump straight to any row number
- **Detail view**: Double-click a row to see the full record (every field, untruncated, including hidden columns)
- **Flexible delimiter support**: Auto-detected on open; comma, tab, semicolon, pipe, or space
- **Encoding-aware**: Detects UTF-8 (incl. BOM), Windows-1252, and Latin-1 so non-UTF-8 files open without errors
- **No-header mode**: Treat the first row as data and auto-name the columns
- **Customizable page size**: Adjust how many rows to load at once
- **Column management**: Show/hide columns, auto-fit to content, or expand to full width; a left row-number column
- **Copy**: Right-click to copy a cell, row, or column
- **Recent files & persistence**: Remembers window size, page size, delimiter, and recently opened files
- **Dark mode**: Matches the macOS appearance and re-themes live when you toggle it
- **Improved readability**: Alternating row colors for easier reading

## Installation

### Option 1: Homebrew (macOS)

```
brew install --cask rudra370/tap/lazy-csv-viewer
```

The app is unsigned, so the first time you launch it, right-click the app and
choose **Open** to get past Gatekeeper.

### Option 2: Download a prebuilt app

Grab the latest macOS `.app` or Windows `.exe` from the
[Releases](https://github.com/Rudra370/lazy_csv_viewer/releases) page and unzip it.

### Option 3: Run from source

1. Ensure you have Python 3.6+ installed
2. Clone this repository:
   ```
   git clone https://github.com/Rudra370/lazy_csv_viewer
   cd lazy_csv_viewer
   ```
3. Run the application:
   ```
   python main.py
   ```

### Option 4: Build a standalone macOS app

Build a double-clickable `.app` bundle (generates an icon and runs PyInstaller).
On Homebrew Python the system `pip` is locked down, so install tools into a
virtualenv first:

```
python3 -m venv .venv
source .venv/bin/activate
pip install pyinstaller
./build.sh
```

The bundle is created at `dist/Lazy CSV Viewer.app` — launch it with
`open "dist/Lazy CSV Viewer.app"`. On other platforms, PyInstaller still works;
run it directly (`python -m PyInstaller --windowed main.py`).

## Usage

1. Launch the application
2. Click "Open CSV File" and select your CSV file
3. Use the delimiter dropdown to select the appropriate delimiter for your file
4. Navigate through the data using the "Previous Page" and "Next Page" buttons
5. Customize your view:
   - Adjust page size with the "Page Size" field and "Apply" button
   - Toggle column width with the "Expand Columns" / "Collapse Columns" button
   - Show/hide specific columns with the "Show/Hide Columns" button

### Keyboard Shortcuts

- `Left Arrow` / `Right Arrow`: Scroll left / right
- `Shift + Left/Right Arrow`: Scroll faster
- `Shift + Mouse Wheel`: Horizontal scrolling
- `Cmd/Ctrl + Left/Right Arrow`: Previous / Next page
- `Cmd/Ctrl + F`: Focus the search box
- `Cmd/Ctrl + O`: Open a file
- `Enter` in the search box: Find next match

## Development

The tests require `tkinter` and a display. On Homebrew Python the system `pip`
is externally managed, so use a virtualenv (it inherits `tkinter` from the base
install):

```
python3 -m venv .venv
source .venv/bin/activate
pip install pytest
python -m pytest
```

## Requirements

- Python 3.6 or higher
- Tkinter (usually included with Python; on Homebrew Python install `python-tk`)

## License

[MIT License](LICENSE)

## Contributing

Contributions are welcome! Feel free to submit a Pull Request.
