# Lazy CSV Viewer

A lightweight, efficient CSV viewer application that loads and displays CSV files page by page. Built with Python and Tkinter.

## Demo

![Demo](demo.gif)

## Features

- **Page-by-page loading**: Handle large CSV files efficiently by loading only what you need
- **Flexible delimiter support**: View files with different delimiters (comma, tab, semicolon, pipe, space)
- **Customizable page size**: Adjust how many rows to load at once
- **Column management**:
  - Show/hide specific columns
  - Expand or collapse column widths
- **Navigation**:
  - Horizontal scrolling (mouse wheel with Shift key or arrow keys)
  - Previous/Next page buttons
- **Improved readability**: Alternating row colors for easier reading

## Installation

### Option 1: Run from Source

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

### Option 2: Standalone Executable

You can build a standalone executable using PyInstaller:

```
pip install pyinstaller
pyinstaller --onefile --noconsole main.py
```

The executable will be created in the `dist` directory.

## Usage

1. Launch the application
2. Click "Open CSV File" and select your CSV file
3. Use the delimiter dropdown to select the appropriate delimiter for your file
4. Navigate through the data using the "Previous Page" and "Next Page" buttons
5. Customize your view:
   - Adjust page size with the "Page Size" field and "Apply" button
   - Toggle column expansion with "Expand/Collapse All Columns"
   - Show/hide specific columns with the "Show/Hide Columns" button

### Keyboard Shortcuts

- `Left Arrow`: Scroll left
- `Right Arrow`: Scroll right
- `Shift + Left Arrow`: Scroll left faster
- `Shift + Right Arrow`: Scroll right faster
- `Shift + Mouse Wheel`: Horizontal scrolling

## Requirements

- Python 3.6 or higher
- Tkinter (usually included with Python)

## License

[MIT License](LICENSE)

## Contributing

Contributions are welcome! Feel free to submit a Pull Request.
