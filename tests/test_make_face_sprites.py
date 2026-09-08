"""tools/make_face_sprites.py.

rgb565_values() is pure bit-packing, tested against solid-color images so the
result doesn't depend on the resize step (a solid color stays solid at any
size).

find_characters()/normalised_crop() are what keep her from drifting between
animation frames, which has regressed twice: once because the sheet's column
pitch isn't uniform (stepping a fixed cell width accumulates px of drift and
eventually slides into the neighbouring cell), and once because she's drawn
at different sizes per row, so fixed-size crops rescale her differently.
Both are covered here on a synthetic sheet."""
import numpy as np
from PIL import Image

from tools.make_face_sprites import (
    FINAL_H,
    FINAL_W,
    MIN_BLOB_PX,
    CANVAS_BG,
    FRAME_LINE_FRACTION,
    find_characters,
    frame_lines,
    normalised_crop,
    rgb565_values,
    window_mask,
)


def _solid(color):
    return Image.new("RGB", (4, 4), color)


class TestRgb565Values:
    def test_output_length_matches_final_dimensions(self):
        assert len(rgb565_values(_solid((0, 0, 0)))) == FINAL_W * FINAL_H

    def test_black_packs_to_zero(self):
        assert all(v == 0 for v in rgb565_values(_solid((0, 0, 0))))

    def test_white_packs_to_all_ones(self):
        assert all(v == 0xFFFF for v in rgb565_values(_solid((255, 255, 255))))

    def test_pure_red_only_sets_the_top_5_bits(self):
        assert rgb565_values(_solid((255, 0, 0)))[0] == 0xF800

    def test_pure_green_only_sets_the_middle_6_bits(self):
        assert rgb565_values(_solid((0, 255, 0)))[0] == 0x07E0

    def test_pure_blue_only_sets_the_bottom_5_bits(self):
        assert rgb565_values(_solid((0, 0, 255)))[0] == 0x001F

    def test_low_bits_of_each_channel_are_dropped_not_rounded(self):
        # r=7 has no bits in its top 5, g=3 has no bits in its top 6 -> both drop to 0
        assert rgb565_values(_solid((7, 3, 0)))[0] == 0


def _sheet_with_characters(specs, size=(1520, 300), background=(24, 25, 38)):
    """Synthetic sheet: each spec is (cx, cy, w, h) drawn as a filled block,
    standing in for a character. Anything smaller than MIN_BLOB_PX is treated
    as a decorative sparkle by the detector."""
    img = Image.new("RGB", size, background)
    arr = np.array(img)
    for cx, cy, w, h in specs:
        arr[cy - h // 2:cy + h // 2, cx - w // 2:cx + w // 2] = (200, 200, 200)
    return Image.fromarray(arr), arr.max(axis=2) > 45


BAND = (60, 250)


def _row_geometry(cells):
    """What extract() derives per row: median size and vertical anchor."""
    return (float(np.median([c["ux1"] - c["ux0"] + 1 for c in cells])),
            float(np.median([c["top"] for c in cells])))


def _her_centre_x(img):
    arr = np.array(img.convert("RGB"))
    xs = np.nonzero((arr.max(axis=2) > 45).any(axis=0))[0]
    return (xs.min() + xs.max()) / 2


class TestFindCharacters:
    def test_finds_every_character_left_to_right(self):
        specs = [(200, 150, 100, 120), (600, 150, 100, 120), (1000, 150, 100, 120)]
        _, bright = _sheet_with_characters(specs)
        cells = find_characters(bright, BAND)
        assert len(cells) == 3
        found = [c["cx"] for c in cells]
        assert found == sorted(found)
        for (cx, *_), centre in zip(specs, found):
            assert abs(centre - cx) <= 1

    def test_uneven_spacing_is_fine(self):
        # the real sheets are NOT on a uniform pitch — detection must not care
        specs = [(150, 150, 90, 110), (480, 150, 90, 110), (1010, 150, 90, 110)]
        _, bright = _sheet_with_characters(specs)
        assert len(find_characters(bright, BAND)) == 3

    def test_sparkles_are_not_mistaken_for_characters(self):
        big = (600, 150, 100, 120)
        sparkle_w = 8  # 8*8 = 64px, far under MIN_BLOB_PX
        assert sparkle_w * sparkle_w < MIN_BLOB_PX
        specs = [(400, 120, sparkle_w, sparkle_w), big, (800, 180, sparkle_w, sparkle_w)]
        _, bright = _sheet_with_characters(specs)
        cells = find_characters(bright, BAND)
        assert len(cells) == 1
        assert abs(cells[0]["cx"] - big[0]) <= 1

    def test_row_wide_divider_line_is_ignored(self):
        # a row border spanning the sheet, clear of her (as on the real sheet)
        specs = [(600, 150, 100, 120), (760, 70, 1500, 4)]
        _, bright = _sheet_with_characters(specs)
        assert len(find_characters(bright, BAND)) == 1

    def test_dense_side_effect_does_not_drag_her_centre(self):
        """Audio pulses and battery icons are dense enough to survive the blob
        filter and land in her cell; her centre must still come from her own
        body, or the crop slides sideways."""
        her_cx = 600
        # beside her (her body ends at 650) but near enough to join her cell
        specs = [(her_cx, 150, 100, 120), (her_cx + 68, 150, 30, 90)]
        _, bright = _sheet_with_characters(specs)
        cells = find_characters(bright, BAND)
        assert len(cells) == 1
        assert cells[0]["ux1"] > her_cx + 68  # the effect did join her cell
        assert abs(cells[0]["cx"] - her_cx) <= 2  # ...but her centre is still hers


class TestNormalisedCrop:
    def test_one_odd_cell_does_not_change_its_own_framing(self):
        """The framing bug: a pose that makes one cell measure differently used
        to rescale that frame, so she'd jump on that frame alone. Size comes
        from the row median instead, so all frames in a row match."""
        specs = [(300, 150, 100, 120), (700, 150, 100, 120), (1100, 150, 140, 120)]
        sheet, bright = _sheet_with_characters(specs)
        cells = find_characters(bright, BAND)
        w, top = _row_geometry(cells)
        sizes = {normalised_crop(sheet, c, w, top).size for c in cells}
        assert len(sizes) == 1

    def test_crop_keeps_the_output_aspect_ratio(self):
        sheet, bright = _sheet_with_characters([(600, 150, 100, 120)])
        cells = find_characters(bright, BAND)
        crop = normalised_crop(sheet, cells[0], *_row_geometry(cells))
        assert abs(crop.size[0] / crop.size[1] - FINAL_W / FINAL_H) < 0.05

    def test_crop_is_horizontally_centred_on_her(self):
        sheet, bright = _sheet_with_characters([(300, 150, 100, 120), (900, 150, 100, 120)])
        cells = find_characters(bright, BAND)
        w, top = _row_geometry(cells)
        for cell in cells:
            crop = normalised_crop(sheet, cell, w, top)
            assert abs(_her_centre_x(crop) - crop.size[0] / 2) <= 2

    def test_blank_above_removes_a_label_printed_over_her_head(self):
        background = (7, 10, 22)  # as the real sheets measure
        sheet, bright = _sheet_with_characters(
            [(600, 160, 100, 100), (600, 96, 60, 8)],  # her, and a label above her
            background=background,
        )
        cells = find_characters(bright, BAND)
        w, top = _row_geometry(cells)

        plain = np.array(normalised_crop(sheet, cells[0], w, top))
        blanked = np.array(normalised_crop(sheet, cells[0], w, top, blank_above=background))

        assert plain.shape == blanked.shape
        assert plain.max() > 100, "the label should be visible without blanking"
        head_row = int(top) - int(top - w * (FINAL_H / FINAL_W) * 0.14) - 2
        assert blanked[:head_row].max() <= max(background), "label survived blanking"
        assert blanked[head_row:].max() > 100, "she should be untouched below it"


class TestWindowMask:
    """A crop window is hung off her body, so the first cell in a row can put
    it past the sheet's left edge — measured x0 = -8 for mouth_closed. Two
    things then go wrong at once: PIL pads an out-of-bounds crop with pure
    black, and `bright[y0:y1, -8:x1]` is *empty* because numpy reads -8 as
    "8 from the right edge". The old code compared shapes and skipped the
    background recolour when they disagreed, so mouth_closed alone kept the
    sheet's near-black background — visible on the Stick as a dark box behind
    the waveform bars, every time she fell silent."""

    def _bright(self):
        b = np.zeros((100, 200), dtype=bool)
        b[20:80, 40:160] = True
        return b

    def test_a_window_inside_the_sheet_is_just_the_slice(self):
        b = self._bright()
        assert np.array_equal(window_mask(b, 10, 10, 60, 60), b[10:60, 10:60])

    def test_the_mask_always_matches_the_window_size(self):
        b = self._bright()
        for box in [(-8, 0, 217, 120), (150, 50, 260, 130), (-20, -20, 20, 20)]:
            x0, y0, x1, y1 = box
            assert window_mask(b, x0, y0, x1, y1).shape == (y1 - y0, x1 - x0), box

    def test_a_negative_x0_does_not_wrap_to_the_far_edge(self):
        """The specific numpy bug: bright[:, -8:217] selects from x=192, not
        from the left edge, and is empty besides."""
        b = self._bright()
        mask = window_mask(b, -8, 20, 100, 80)
        assert mask.any()                       # not empty, unlike the old slice
        assert not mask[:, :8].any()            # off-sheet columns are background
        assert np.array_equal(mask[:, 48:], b[20:80, 40:100])

    def test_off_sheet_pixels_count_as_background(self):
        b = np.ones((10, 10), dtype=bool)
        mask = window_mask(b, -3, -3, 5, 5)
        assert not mask[:3, :].any() and not mask[:, :3].any()
        assert mask[3:, 3:].all()

    def test_a_window_entirely_off_the_sheet_is_all_background(self):
        b = np.ones((10, 10), dtype=bool)
        assert not window_mask(b, 50, 50, 60, 60).any()


class TestOffSheetCropsGetTheCanvasBackground:
    """The end-to-end version of the above: whatever the window overhangs must
    come out as CANVAS_BG, so the sprite's margins are invisible against
    main.cpp's fillScreen(COL_BASE) instead of showing as a darker box."""

    def test_a_cell_near_the_left_edge_still_gets_a_clean_background(self):
        # Her centre is close enough to x=0 that WIDTH_ZOOM's window overhangs.
        sheet, bright = _sheet_with_characters([(70, 150, 100, 140), (400, 150, 100, 140)])
        cells = find_characters(bright, BAND)
        row_width, row_top = _row_geometry(cells)
        crop = normalised_crop(sheet, cells[0], row_width, row_top, bright=bright)

        arr = np.array(crop.convert("RGB"))
        assert (arr.sum(axis=2) == 0).sum() == 0, "PIL's black out-of-bounds padding survived"
        # The overhanging left column must be background, not the sheet's own
        # darker colour and not black.
        assert np.array_equal(arr[0, 0], np.array(CANVAS_BG))


class TestFrameLines:
    """Every sheet prints a border down each side, and it is bright, so
    CONTENT_THRESHOLD keeps it and normalised_crop preserves it as if it were
    art. Invisible for a crop well inside the sheet — but a crop that
    overhangs an edge drags the line in, and it lands as a 1px vertical stripe
    the full height of the sprite. On the Stick that stripe fell in the gap
    between two waveform bars and read as the wave being broken."""

    def _sheet(self, h=200, w=300):
        b = np.zeros((h, w), dtype=bool)
        b[40:120, 100:200] = True      # a character: tall, but nowhere near full height
        return b

    def test_a_full_height_column_is_a_frame_line(self):
        b = self._sheet()
        b[:, 13] = True
        assert frame_lines(b)[:, 13].all()

    def test_a_full_width_row_is_a_frame_line(self):
        b = self._sheet()
        b[7, :] = True
        assert frame_lines(b)[7, :].all()

    def test_character_art_is_not_mistaken_for_a_frame(self):
        """A cell is roughly a sixth of a sheet's height; real art tops out
        around 0.2 on this measure, well under the threshold."""
        b = self._sheet()
        assert not frame_lines(b).any()

    def test_the_threshold_leaves_real_headroom(self):
        assert 0.5 < FRAME_LINE_FRACTION < 1.0

    def test_a_frame_line_is_excluded_from_a_crop_that_overhangs(self):
        """The end-to-end case: her window runs past the left edge, so without
        this the border line comes through in the middle of the sprite."""
        BORDER = (150, 90, 90)  # distinct from the character, so it's traceable
        sheet, _ = _sheet_with_characters([(70, 150, 100, 140), (400, 150, 100, 140)])
        arr = np.array(sheet)
        arr[:, 13] = BORDER                   # the sheet's own left border
        sheet = Image.fromarray(arr)
        bright = arr.max(axis=2) > 45
        assert bright[:, 13].all()            # bright, so the threshold alone keeps it
        bright &= ~frame_lines(bright)

        cells = find_characters(bright, BAND)
        row_width, row_top = _row_geometry(cells)
        crop = normalised_crop(sheet, cells[0], row_width, row_top, bright=bright)

        out = np.array(crop.convert("RGB"))
        assert not (out == np.array(BORDER)).all(axis=2).any(), "the border line came through"
