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
    find_characters,
    normalised_crop,
    rgb565_values,
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
