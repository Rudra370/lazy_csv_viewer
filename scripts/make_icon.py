"""Generate a spreadsheet-style app icon with no third-party dependencies.

Writes assets/icon_1024.png (8-bit RGBA) using only the standard library, so it
works on a clean Python install. build.sh turns it into an .icns via iconutil.
"""

import os
import struct
import zlib

WIDTH = HEIGHT = 1024


def png_bytes(width, height, buf):
    """Encode an RGBA pixel buffer as a PNG byte string."""

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (none)
        raw += buf[y * stride : (y + 1) * stride]
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def build():
    buf = bytearray(WIDTH * HEIGHT * 4)  # transparent

    def put(x, y, color):
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            o = (y * WIDTH + x) * 4
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = color[0], color[1], color[2], 255

    margin, radius = 80, 180
    card = (45, 108, 223)  # blue body
    header = (27, 73, 158)  # darker header band
    grid = (255, 255, 255)

    def in_card(x, y):
        if x < margin or x >= WIDTH - margin or y < margin or y >= HEIGHT - margin:
            return False
        cx = min(max(x, margin + radius), WIDTH - margin - radius)
        cy = min(max(y, margin + radius), HEIGHT - margin - radius)
        dx, dy = x - cx, y - cy
        return dx * dx + dy * dy <= radius * radius

    header_h = margin + 170
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if in_card(x, y):
                put(x, y, header if y < header_h else card)

    # Spreadsheet grid lines over the body
    left, right = margin + 30, WIDTH - margin - 30
    top, bottom = header_h, HEIGHT - margin - 30
    cols, rows = 4, 5
    thickness = 6
    for c in range(cols + 1):
        x = left + (right - left) * c // cols
        for y in range(top, bottom):
            for t in range(thickness):
                put(x + t, y, grid)
    for r in range(rows + 1):
        y = top + (bottom - top) * r // rows
        for x in range(left, right):
            for t in range(thickness):
                put(x, y + t, grid)

    out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "icon_1024.png")
    with open(out_path, "wb") as f:
        f.write(png_bytes(WIDTH, HEIGHT, buf))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    build()
