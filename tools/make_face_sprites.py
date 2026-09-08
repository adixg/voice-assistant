#!/usr/bin/env python3
"""Extracts the character sprites from the Catppuccin Macchiato sheets into
firmware/m5stick_bridge/src/sprites.h as RGB565 bitmaps ready for M5GFX's
canvas.pushImage(), and into assets/sprites/*.png for speech_orb.py.

Both outputs come from the same crops in the same run, so the desktop orb
shows pixel-identical art to the Stick — the PNGs are the same arrays
sprites.h holds, before the RGB565 packing.

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
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

# speech_orb.py loads these to draw the same character the Stick does. Kept as
# PNGs rather than re-parsing sprites.h's packed RGB565: same pixels, without
# a C-header parser in the GUI's import path.
ASSETS_DIR = "assets/sprites"

CANVAS_BG = (0x24, 0x27, 0x3A)  # must match main.cpp's COL_BASE exactly. Every
                                 # sheet's own background is much darker than
                                 # this (measured: v2 (7,10,22), v3 (9,13,25),
                                 # idle (11,15,27)) — normalised_crop()
                                 # recolours every non-content pixel in a crop
                                 # to this so a sprite's own margins blend into
                                 # main.cpp's canvas.fillScreen(COL_BASE)
                                 # instead of showing up as a visibly darker
                                 # box where the sheet's background meets it.
                                 # Most obvious on a narrow crop like the idle
                                 # animation frames, where background is most
                                 # of the frame, but every sprite has the same
                                 # mismatch underneath.

FINAL_W, FINAL_H = 220, 116  # spans nearly the full 240px screen width. 116 is
                              # tall enough that her chin/collar aren't cropped
                              # at WIDTH_ZOOM below (checked down to 100, which
                              # cropped into her chin on every row), while still
                              # leaving enough of the 135px canvas below her for
                              # main.cpp's status/caption banner — see
                              # TEXT_BAND_Y's comment there. Don't drop this
                              # below ~112 without re-checking the crop against
                              # every state (tools/make_face_sprites.py has no
                              # way to warn you the chin got cut).

# Measured content bands for each sheet's sprite rows (see module docstring —
# these come from a horizontal content projection, not from dividing up the
# sheet height).
ROW_BANDS = [(123, 294), (343, 488), (534, 661)]
SHEET_CONTENT_W = 1520  # ignore the palette panel further right

CONTENT_THRESHOLD = 45  # background is Crust/Base dark; brighter = art
MIN_BLOB_PX = 1200      # smaller than this is a sparkle, not a character
MAX_BLOB_W = 200        # wider than this is a row divider, not a character
# Crop width as a multiple of her detected body width. The old 1.62 cropped
# tight enough to cut off most of the side sparkles/wifi-icons/lightning that
# the sheet draws flanking her in every state; 2.1 keeps them without
# bleeding into the neighbouring cell — measured cell-to-cell spacing is at
# least 2.3x her body width on every row (row 0, the tightest), so 2.1 keeps
# a safety margin. Don't raise this past ~2.25 without re-checking that gap.
WIDTH_ZOOM = 2.1
TOP_MARGIN = 0.06       # headroom above her, as a fraction of the window height;
                        # much more than ~0.14 and the window reaches the
                        # sheet's own row titles printed above each row of
                        # cells. Lower than that just trims background above
                        # her (she sits higher in the final frame, and it's
                        # what buys the safety margin below her chin before
                        # main.cpp's TEXT_BAND_Y — re-measure both if this
                        # changes again)
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


MIN_SPARKLE_PX = 8    # smaller is antialiasing noise, not a real sparkle glyph
MAX_SPARKLE_PX = 30   # wider/taller than this (in either dimension) isn't one glyph
SPARKLE_BODY_MARGIN = 12  # extra clearance around her own body's x-range, so
                          # hair strands/collar/headphone details fragmented
                          # by the brightness threshold don't get mistaken
                          # for sparkles (checked directly — without this
                          # margin, roughly half the "sparkles" found were
                          # actually pieces of her own portrait)


def find_sparkles(bright, band, cell, left, top, win_w, win_h):
    """Real decorative sparkle/icon positions inside one cell's crop window,
    as (x, y) in the FINAL sprite's own pixel space — for main.cpp to twinkle
    on top of the baked-in art (see its drawSparkleTwinkle()). Anything at
    least MIN_BLOB_PX is her (see find_characters) rather than a sparkle;
    anything overlapping her body's x-range (with margin) is a fragment of
    her own art the brightness threshold split off, not a separate glyph."""
    y0, y1 = band
    strip = bright[y0:y1 + 1, :SHEET_CONTENT_W]
    labels, _ = ndimage.label(strip)
    bx0, bx1 = cell["ux0"] - SPARKLE_BODY_MARGIN, cell["ux1"] + SPARKLE_BODY_MARGIN
    pts = []
    for i, sl in enumerate(ndimage.find_objects(labels)):
        ys, xs = sl
        size = int((labels[sl] == (i + 1)).sum())
        if not (MIN_SPARKLE_PX <= size < MIN_BLOB_PX):
            continue
        if xs.stop - xs.start > MAX_SPARKLE_PX or ys.stop - ys.start > MAX_SPARKLE_PX:
            continue
        if xs.stop - 1 >= bx0 and xs.start <= bx1:  # overlaps her body's x-range
            continue
        cx = (xs.start + xs.stop - 1) / 2
        cy = y0 + (ys.start + ys.stop - 1) / 2
        if not (left <= cx < left + win_w and top <= cy < top + win_h):
            continue  # outside this cell's crop window entirely
        fx = round((cx - left) * FINAL_W / win_w)
        fy = round((cy - top) * FINAL_H / win_h)
        pts.append((min(fx, FINAL_W - 1), min(fy, FINAL_H - 1)))
    return pts



# --------------------------------------------------------------- idle animation sheet
# pixelart_idle.png: six 8-frame rows (breathing/blink/look/smile/yawn/sleepy),
# each cell a tight headshot with no side art of her own — unlike v3, whose
# rows have 2.3x+ cell-to-cell spacing to fit side sparkles, this sheet's
# cells sit only ~1.3-1.4x her own body width apart (measured directly:
# min cell-to-cell pitch / median body width per row is 1.29-1.40), so
# reusing v3's WIDTH_ZOOM (2.1) here would crop straight into both
# neighbouring frames. Cropped tighter and square instead of onto the
# wide 220x116 canvas the rest of the sprites share — these are meant to be
# pushImage'd at their own size into the same spot the character normally
# occupies, with the existing procedural particle field (main.cpp) still
# supplying everything around her, not baked side art.
IDLE_ANIM_PATH = "/mnt/c/Users/User/WSL/pixelart_idle.png"
IDLE_ANIM_CONTENT_W = 1210  # this sheet's sidebar (palette/sparkle-glyph legend) starts ~1225
IDLE_ANIM_W, IDLE_ANIM_H = 128, 128
IDLE_ANIM_WIDTH_ZOOM = 1.3   # keeps window_w (~130-140px) under the ~140-148px cell pitch
IDLE_ANIM_TOP_MARGIN = 0.10
# Row bands trimmed 2px off the bottom for character-finding only: each row
# ends in a full-width horizontal divider line that, left in, bridges every
# cell's body blob into one connected component spanning the whole row
# (fails MAX_BLOB_W and silently drops that row's leftmost cell — caught by
# comparing found-cell counts against each row's known 8). normalised_crop
# still gets the untrimmed bottom via band_bottom, so blank_above correctly
# paints over that divider line rather than leaving it in the final crop.
IDLE_ANIM_ROWS = [
    # (name prefix, row band, frame indices to keep, out of 8 columns —
    # curated by eye off the sheet, not the full 8: see the row_*.png crops
    # made while measuring this, e.g. blink's is open/half-closing/closed/half-open)
    ("idle_breathe", (124, 231), [0, 2, 4, 6]),
    ("idle_blink",   (274, 382), [0, 2, 3, 6]),
    ("idle_look",    (427, 539), [0, 3, 7, 0]),
    ("idle_smile",   (580, 688), [0, 3, 5, 7]),
    ("idle_yawn",    (729, 838), [0, 1, 5, 7]),
    ("idle_sleepy",  (882, 989), [0, 2, 4, 6]),
]


def extract_idle_anim(path=IDLE_ANIM_PATH):
    global SHEET_CONTENT_W
    saved_w = SHEET_CONTENT_W
    SHEET_CONTENT_W = IDLE_ANIM_CONTENT_W
    try:
        sheet = Image.open(path).convert("RGB")
        arr = np.array(sheet)
        bright = arr.max(axis=2) > CONTENT_THRESHOLD
        bright &= ~frame_lines(bright)
        sprites = []
        for prefix, band, frame_idx in IDLE_ANIM_ROWS:
            cells = find_characters(bright, (band[0], band[1] - 2))
            if len(cells) != 8:
                sys.exit(f"{path} row {band} ({prefix}): found {len(cells)} frames, "
                         f"expected 8 — re-measure this row's band")
            row_width = float(np.median([c["ux1"] - c["ux0"] + 1 for c in cells]))
            row_top = float(np.median([c["top"] for c in cells]))
            for n, i in enumerate(frame_idx):
                crop = normalised_crop(sheet, cells[i], row_width, row_top,
                                        CANVAS_BG, band[1],
                                        width_zoom=IDLE_ANIM_WIDTH_ZOOM,
                                        top_margin=IDLE_ANIM_TOP_MARGIN,
                                        final_w=IDLE_ANIM_W, final_h=IDLE_ANIM_H,
                                        bright=bright)
                sprites.append((f"{prefix}{n}", crop))
        return sprites
    finally:
        SHEET_CONTENT_W = saved_w


# A column or row that is "bright" across at least this fraction of the whole
# sheet is the sheet's own printed frame, not character art. Every sheet has
# one down each side (measured: v2 x=13 at 0.82, v3 x=20-21 at 0.86, idle
# x=16-17 at 0.81, plus their right-hand counterparts), and v3 also prints
# horizontal rules between its rows at 0.95-0.97. No character comes close:
# a cell is roughly a sixth of the sheet's height, so real art tops out
# around 0.2 on this measure.
FRAME_LINE_FRACTION = 0.8


def frame_lines(bright):
    """The sheet's own printed border and divider rules, as a mask.

    These are *bright*, so CONTENT_THRESHOLD keeps them and normalised_crop
    then preserves them as if they were art. That is invisible for every crop
    that sits well inside the sheet — but a crop whose window overhangs an
    edge drags the border line in with it, and it lands as a 1px vertical
    stripe running the full height of the sprite.

    That is exactly what happened to mouth_closed: the v2 sheet's left border
    at x=13 came through at canvas x=31, straight down the gap between two
    waveform bars, which read as the wave being broken in one place.
    """
    h, w = bright.shape
    mask = np.zeros_like(bright)
    mask[:, bright.sum(axis=0) >= h * FRAME_LINE_FRACTION] = True
    mask[bright.sum(axis=1) >= w * FRAME_LINE_FRACTION, :] = True
    return mask


def window_mask(bright, x0, y0, x1, y1):
    """`bright` over the window (x0,y0)-(x1,y1), which may hang off the sheet.

    Anything outside the sheet counts as background (False), so it gets
    recoloured to CANVAS_BG like any other margin. This is not a corner case:
    the first cell of a row sits close enough to the sheet's left edge that
    WIDTH_ZOOM's window runs past it — measured x0 = -8 for mouth_closed.

    Doing this by hand rather than with a plain `bright[y0:y1, x0:x1]` slice,
    because that slice is wrong in two ways at once when x0 is negative:
    numpy reads -8 as "8 from the right edge", so `bright[644:764, -8:217]`
    is *empty*, and PIL meanwhile pads the out-of-bounds crop with pure black.
    The guard that used to sit here compared shapes and skipped the recolour
    when they disagreed, which turned both bugs into one silent one — the
    whole sprite kept the sheet's near-black background while every other
    sprite got CANVAS_BG. See tests/test_make_face_sprites.py.
    """
    mask = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    sy0, sx0 = max(0, y0), max(0, x0)
    sy1, sx1 = min(bright.shape[0], y1), min(bright.shape[1], x1)
    if sy1 > sy0 and sx1 > sx0:
        mask[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = bright[sy0:sy1, sx0:sx1]
    return mask


def normalised_crop(sheet, cell, row_width, row_top, blank_above=None, band_bottom=None,
                     width_zoom=None, top_margin=None, final_w=None, final_h=None, bright=None):
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

    bright, when given the sheet-wide content mask (see find_characters —
    True where a pixel is bright enough to be art, not background), recolours
    every non-content pixel in the crop to CANVAS_BG. blank_above still does
    its own hard paint on top of that for the label/divider strips, since
    label text is itself bright and so wouldn't be touched by the mask alone.

    width_zoom/top_margin/final_w/final_h override the module defaults —
    used by extract_idle_anim() for pixelart_idle.png's tightly-packed cells,
    which can't take the same crop window as v3/v2 without bleeding into the
    next frame (see its own comment).
    """
    width_zoom = WIDTH_ZOOM if width_zoom is None else width_zoom
    top_margin = TOP_MARGIN if top_margin is None else top_margin
    final_w = FINAL_W if final_w is None else final_w
    final_h = FINAL_H if final_h is None else final_h
    win_w = row_width * width_zoom
    win_h = win_w * (final_h / final_w)
    top = row_top - win_h * top_margin
    cx = cell["cx"]
    x0, y0 = int(cx - win_w / 2), int(top)
    x1, y1 = int(cx + win_w / 2), int(top + win_h)
    crop = sheet.crop((x0, y0, x1, y1))
    if bright is not None:
        arr = np.array(crop)
        arr[~window_mask(bright, x0, y0, x1, y1)] = CANVAS_BG
        crop = Image.fromarray(arr)
    if blank_above is not None:
        head_top = max(0, int(row_top - top) - 2)  # a hair's clearance above her
        crop.paste(blank_above, (0, 0, crop.size[0], head_top))
        if band_bottom is not None:
            below = min(crop.size[1], max(0, int(band_bottom - top)))
            crop.paste(blank_above, (0, below, crop.size[0], crop.size[1]))
    return crop


def final_image(img, w=None, h=None):
    """The sprite at its output size — one NEAREST resize, no interpolation.

    Both outputs go through this, so the PNG speech_orb.py loads and the
    RGB565 array the Stick displays are the same pixels.
    """
    w = FINAL_W if w is None else w
    h = FINAL_H if h is None else h
    return img.convert("RGB").resize((w, h), Image.NEAREST)


def write_pngs(sprites, idle_anim_sprites, out_dir=ASSETS_DIR):
    os.makedirs(out_dir, exist_ok=True)
    for name, img in sprites:
        final_image(img).save(os.path.join(out_dir, f"{name}.png"))
    for name, img in idle_anim_sprites:
        final_image(img, IDLE_ANIM_W, IDLE_ANIM_H).save(os.path.join(out_dir, f"{name}.png"))
    return len(sprites) + len(idle_anim_sprites)


def rgb565_values(img, w=None, h=None):
    w = FINAL_W if w is None else w
    h = FINAL_H if h is None else h
    img = final_image(img, w, h)
    vals = []
    for y in range(h):
        for x in range(w):
            r, g, b = img.getpixel((x, y))
            vals.append(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
    return vals


def extract(path, row_bands, states, blank_labels=False, find_sparkle_dots=False):
    sheet = Image.open(path).convert("RGB")
    arr = np.array(sheet)
    bright = arr.max(axis=2) > CONTENT_THRESHOLD
    bright &= ~frame_lines(bright)
    blank_above = CANVAS_BG if blank_labels else None
    sprites = []
    sparkles = {}
    for band, names in zip(row_bands, states):
        cells = find_characters(bright, band)
        if len(cells) != len(names):
            sys.exit(f"{path} row {band}: found {len(cells)} characters, expected "
                     f"{len(names)} — the sheet layout changed, re-measure its row bands")
        row_width = float(np.median([c["ux1"] - c["ux0"] + 1 for c in cells]))
        row_top = float(np.median([c["top"] for c in cells]))
        for name, cell in zip(names, cells):
            if name is None:
                continue
            sprites.append((name, normalised_crop(sheet, cell, row_width, row_top,
                                                   blank_above, band[1], bright=bright)))
            if find_sparkle_dots:
                win_w = row_width * WIDTH_ZOOM
                win_h = win_w * (FINAL_H / FINAL_W)
                top = row_top - win_h * TOP_MARGIN
                left = cell["cx"] - win_w / 2
                sparkles[name] = find_sparkles(bright, band, cell, left, top, win_w, win_h)
    return sprites, sparkles


def main():
    v3 = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/Users/User/WSL/pixelart_v3.png"
    v2 = sys.argv[2] if len(sys.argv) > 2 else "/mnt/c/Users/User/WSL/pixelart_v2.png"

    sprites, sparkles = extract(v3, ROW_BANDS, STATES, find_sparkle_dots=True)
    v2_sprites, _ = extract(v2, V2_ROW_BANDS, V2_STATES, blank_labels=True)
    sprites += v2_sprites
    idle_anim_sprites = extract_idle_anim()

    out_path = "firmware/m5stick_bridge/src/sprites.h"
    with open(out_path, "w") as f:
        f.write("// Generated by tools/make_face_sprites.py from the Catppuccin Macchiato sheet.\n")
        f.write("// Packed RGB565, %dx%d each unless noted. Don't hand-edit — regenerate instead.\n" % (FINAL_W, FINAL_H))
        f.write("#pragma once\n#include <cstdint>\n\n")
        f.write(f"static const int kSpriteW = {FINAL_W};\n")
        f.write(f"static const int kSpriteH = {FINAL_H};\n\n")
        for name, img in sprites:
            vals = rgb565_values(img)
            f.write(f"static const uint16_t kSprite_{name}[] = {{\n")
            for i in range(0, len(vals), 16):
                f.write("  " + ",".join(str(v) for v in vals[i:i + 16]) + ",\n")
            f.write("};\n\n")

        # Real sparkle/icon positions baked into each v3 state's art (in that
        # sprite's own pixel space), for main.cpp's drawSparkleTwinkle() to
        # animate on top of instead of leaving them static. v2-sourced states
        # (blink/mouth_*) have none — SPEAKING fakes its own, see main.cpp.
        f.write("struct SparkleDot { uint8_t x, y; };\n\n")
        for name, pts in sparkles.items():
            body = ", ".join(f"{{{x},{y}}}" for x, y in pts) if pts else "{0,0}"  # never empty: a 0-length array is invalid
            f.write(f"static const SparkleDot kSparkles_{name}[] = {{{body}}};\n")
            f.write(f"static const int kSparkleCount_{name} = {len(pts)};\n\n")

        # pixelart_idle.png's curated idle-gesture frames — smaller and
        # square (own W/H, not kSpriteW/kSpriteH) since these are tight
        # headshots with no side art; see extract_idle_anim()'s comment.
        f.write(f"static const int kIdleAnimW = {IDLE_ANIM_W};\n")
        f.write(f"static const int kIdleAnimH = {IDLE_ANIM_H};\n\n")
        for name, img in idle_anim_sprites:
            vals = rgb565_values(img, IDLE_ANIM_W, IDLE_ANIM_H)
            f.write(f"static const uint16_t kSprite_{name}[] = {{\n")
            for i in range(0, len(vals), 16):
                f.write("  " + ",".join(str(v) for v in vals[i:i + 16]) + ",\n")
            f.write("};\n\n")
        for prefix, _, frame_idx in IDLE_ANIM_ROWS:
            names = ", ".join(f"kSprite_{prefix}{n}" for n in range(len(frame_idx)))
            f.write(f"static const uint16_t *const k{prefix.title().replace('_', '')}[] = {{{names}}};\n")
            f.write(f"static const int k{prefix.title().replace('_', '')}Count = {len(frame_idx)};\n\n")

    n_png = write_pngs(sprites, idle_anim_sprites)

    total_sparkles = sum(len(p) for p in sparkles.values())
    idle_anim_kb = sum(IDLE_ANIM_W * IDLE_ANIM_H * 2 for _ in idle_anim_sprites) // 1024
    print(f"wrote {out_path} ({len(sprites)} sprites, {FINAL_W}x{FINAL_H} each, "
          f"~{len(sprites) * FINAL_W * FINAL_H * 2 // 1024}KB, "
          f"{total_sparkles} sparkle dots across {len(sparkles)} states, "
          f"{len(idle_anim_sprites)} idle-gesture frames, {IDLE_ANIM_W}x{IDLE_ANIM_H} each, "
          f"~{idle_anim_kb}KB)")
    print(f"wrote {ASSETS_DIR}/ ({n_png} pngs, same crops — for speech_orb.py)")


if __name__ == "__main__":
    main()
