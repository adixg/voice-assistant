#!/usr/bin/env python3
"""Extracts the character sprites from the Catppuccin Macchiato sheets into
firmware/m5stick_bridge/src/sprites.h as RGB565 bitmaps ready for M5GFX's
canvas.pushImage().

Mostly pixelart_v3.png (13 states, each a character flanked by side effects),
plus the blink and mouth-shape frames from pixelart_v2.png, which v3 doesn't
have — v3 is one frame per state.

Nothing here assumes a uniform grid, because the sheets aren't on one: cell
pitch drifts several px per column (v2's measured centre-to-centre spacing
runs 136.5-144.5px, so stepping a fixed cell width eventually lands in the
neighbouring cell), and she's drawn at noticeably different sizes per row
(v3 is ~169px tall in row 0, ~122px in row 2). Cropping fixed windows out of
that makes her jump around in size and position between animation frames,
which is exactly what it looked like.

So instead: find her in each cell as the biggest connected blob (side
sparkles, audio pulses and battery icons are separate blobs, and dense
enough to fool a simple density test), then crop a window whose size and
vertical anchor come from that row's *median* — per-cell numbers wobble with
her pose, and any wobble reads as her moving. Horizontal position stays
per-cell so she's actually centred.

Scaling to the final size is a single NEAREST NEIGHBOR resize — no
smoothing/interpolation.

    python3 tools/make_face_sprites.py [v3.png] [v2.png]
"""
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

FINAL_W, FINAL_H = 120, 96  # most of the M5StickS3's 135px canvas height

# Measured content bands for each sheet's sprite rows (see module docstring —
# these come from a horizontal content projection, not from dividing up the
# sheet height).
ROW_BANDS = [(123, 294), (343, 488), (534, 661)]
SHEET_CONTENT_W = 1520  # ignore the palette panel further right

CONTENT_THRESHOLD = 45  # background is Crust/Base dark; brighter = art
MIN_BLOB_PX = 1200      # smaller than this is a sparkle, not a character
MAX_BLOB_W = 200        # wider than this is a row divider, not a character
WIDTH_ZOOM = 1.62       # crop width as a multiple of her detected body width
TOP_MARGIN = 0.14       # headroom above her, as a fraction of the window height;
                        # much more than this and the window reaches the sheet's
                        # own row titles printed above each row of cells
BACKGROUND_SAMPLE_STEP = 4  # subsampling used to find a sheet's background colour

# Left-to-right within each row band; None skips a cell.
STATES = [
    ["idle", "listening", "thinking", "speaking"],
    ["done", "excited", "confused", "surprised"],
    ["error", "disconnected", "charging", "low_battery", "sleepy"],
]

# v3 has one frame per state, which loses mouth-shape and blink animation, so
# those specific frames come from the older v2 sheet. Both draw the same
# character and every frame is normalised the same way, so they line up. v2
# labels each cell in text directly above her head, hence blank_above.
V2_ROW_BANDS = [(84, 256), (600, 757)]
V2_STATES = [
    [None, "blink", "blink2", None, None, None, None, None],
    ["mouth_closed", "mouth_small", "mouth_medium", "mouth_wide", None, None],
]


def find_characters(bright, band):
    """Her body's (x0, x1, y0, y1) box in each cell of one row band, left to right.

    Found as the biggest connected blob per cell rather than by grid
    position or column density: side effects are separate blobs, and the
    dense ones (audio pulses, battery icons, lightning) would otherwise be
    mistaken for part of her and drag the crop sideways.
    """
    y0, y1 = band
    strip = bright[y0:y1 + 1, :SHEET_CONTENT_W]
    labels, _ = ndimage.label(strip)

    blobs = []
    for i, sl in enumerate(ndimage.find_objects(labels)):
        ys, xs = sl
        if xs.stop - xs.start > MAX_BLOB_W:
            continue
        size = int((labels[sl] == (i + 1)).sum())
        if size >= MIN_BLOB_PX:
            blobs.append((xs.start, xs.stop - 1, y0 + ys.start, y0 + ys.stop - 1, size))

    # One cell can yield several qualifying blobs (her body, and the
    # headphone/ear outline around it); keep the biggest per cell, which is
    # reliably her body — its width barely moves between poses, while its
    # height does (the thinking pose brings a hand up to her chin).
    blobs.sort(key=lambda b: b[0])
    cells = []
    for b in blobs:
        x0, x1, y0, y1, size = b
        cx = (x0 + x1) / 2
        if cells and abs(cx - cells[-1]["ux_mid"]) < 70:
            c = cells[-1]
            c["ux0"], c["ux1"] = min(c["ux0"], x0), max(c["ux1"], x1)
            c["ux_mid"] = (c["ux0"] + c["ux1"]) / 2
            c["top"] = min(c["top"], y0)
            if size > c["size"]:  # her body: the biggest blob, unmoved by side effects
                c["size"], c["cx"] = size, cx
        else:
            cells.append({"ux0": x0, "ux1": x1, "ux_mid": cx,
                          "top": y0, "size": size, "cx": cx})
    return cells


def sheet_background(arr):
    """The sheet's own background colour, so blanked areas don't show up as a
    lighter patch. Measured, not assumed: the sheets sit darker than the
    palette's Base (v2 is (7,10,22), v3 (9,13,25))."""
    sub = arr[::BACKGROUND_SAMPLE_STEP, ::BACKGROUND_SAMPLE_STEP].reshape(-1, 3)
    colours, counts = np.unique(sub, axis=0, return_counts=True)
    return tuple(int(v) for v in colours[counts.argmax()])


def normalised_crop(sheet, cell, row_width, row_top, blank_above=None, band_bottom=None):
    """A window scaled to her silhouette and hung off her head-top, so she
    lands at the same size in the same place in every single sprite.

    Size and vertical anchor come from the row's median rather than this
    cell's own measurements: a single cell's numbers get thrown off by poses
    and effects (the thinking pose's raised hand, a mouth-closed face whose
    blob splits differently), and any such wobble becomes her jumping about
    between animation frames. Horizontal position is per-cell, taken from her
    body blob so side effects don't drag it sideways.

    blank_above, when given a background colour, paints out everything above
    her hair and below her row — for sheets that print a cell label right over
    her head, and the next row's title just below her.
    """
    win_w = row_width * WIDTH_ZOOM
    win_h = win_w * (FINAL_H / FINAL_W)
    top = row_top - win_h * TOP_MARGIN
    cx = cell["cx"]
    crop = sheet.crop((int(cx - win_w / 2), int(top),
                       int(cx + win_w / 2), int(top + win_h)))
    if blank_above is not None:
        head_top = max(0, int(row_top - top) - 2)  # a hair's clearance above her
        crop.paste(blank_above, (0, 0, crop.size[0], head_top))
        if band_bottom is not None:
            below = min(crop.size[1], max(0, int(band_bottom - top)))
            crop.paste(blank_above, (0, below, crop.size[0], crop.size[1]))
    return crop


def rgb565_values(img):
    img = img.convert("RGB").resize((FINAL_W, FINAL_H), Image.NEAREST)  # no smoothing/interpolation
    vals = []
    for y in range(FINAL_H):
        for x in range(FINAL_W):
            r, g, b = img.getpixel((x, y))
            vals.append(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
    return vals


def extract(path, row_bands, states, blank_labels=False):
    sheet = Image.open(path).convert("RGB")
    arr = np.array(sheet)
    bright = arr.max(axis=2) > CONTENT_THRESHOLD
    blank_above = sheet_background(arr) if blank_labels else None
    sprites = []
    for band, names in zip(row_bands, states):
        cells = find_characters(bright, band)
        if len(cells) != len(names):
            sys.exit(f"{path} row {band}: found {len(cells)} characters, expected "
                     f"{len(names)} — the sheet layout changed, re-measure its row bands")
        row_width = float(np.median([c["ux1"] - c["ux0"] + 1 for c in cells]))
        row_top = float(np.median([c["top"] for c in cells]))
        for name, cell in zip(names, cells):
            if name is not None:
                sprites.append((name, normalised_crop(sheet, cell, row_width, row_top,
                                                       blank_above, band[1])))
    return sprites


def main():
    v3 = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/Users/User/WSL/pixelart_v3.png"
    v2 = sys.argv[2] if len(sys.argv) > 2 else "/mnt/c/Users/User/WSL/pixelart_v2.png"

    sprites = extract(v3, ROW_BANDS, STATES)
    sprites += extract(v2, V2_ROW_BANDS, V2_STATES, blank_labels=True)

    out_path = "firmware/m5stick_bridge/src/sprites.h"
    with open(out_path, "w") as f:
        f.write("// Generated by tools/make_face_sprites.py from the Catppuccin Macchiato sheet.\n")
        f.write("// Packed RGB565, %dx%d each. Don't hand-edit — regenerate instead.\n" % (FINAL_W, FINAL_H))
        f.write("#pragma once\n#include <cstdint>\n\n")
        f.write(f"static const int kSpriteW = {FINAL_W};\n")
        f.write(f"static const int kSpriteH = {FINAL_H};\n\n")
        for name, img in sprites:
            vals = rgb565_values(img)
            f.write(f"static const uint16_t kSprite_{name}[] = {{\n")
            for i in range(0, len(vals), 16):
                f.write("  " + ",".join(str(v) for v in vals[i:i + 16]) + ",\n")
            f.write("};\n\n")

    print(f"wrote {out_path} ({len(sprites)} sprites, {FINAL_W}x{FINAL_H} each, "
          f"~{len(sprites) * FINAL_W * FINAL_H * 2 // 1024}KB)")


if __name__ == "__main__":
    main()
