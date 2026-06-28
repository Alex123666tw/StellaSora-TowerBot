from __future__ import annotations

import unittest

import numpy as np

from tools.parse_guide_image import (
    TextBlock,
    _has_digit_block_above,
    _is_pink_card,
    spatial_pair_1_to_n,
)


class ParseGuideImageTests(unittest.TestCase):
    def test_detects_digit_block_above_card(self) -> None:
        digit = TextBlock(text="6", x=120, y=70, w=20, h=20)
        found = _has_digit_block_above(
            x=100,
            y=150,
            w=80,
            h=30,
            digit_blocks=[digit],
            img_shape=(300, 300, 3),
        )
        self.assertTrue(found)

    def test_pink_card_rejected_when_digit_block_exists(self) -> None:
        img = np.full((300, 300, 3), (255, 0, 255), dtype=np.uint8)
        digit = TextBlock(text="5", x=120, y=70, w=20, h=20)
        self.assertFalse(_is_pink_card(img, 100, 150, 80, 30, digit_blocks=[digit]))

    def test_pink_card_uses_color_fallback_without_digit_block(self) -> None:
        img = np.full((300, 300, 3), (255, 0, 255), dtype=np.uint8)
        self.assertTrue(_is_pink_card(img, 100, 150, 80, 30, digit_blocks=[]))

    def test_orange_card_rejected_by_color_fallback(self) -> None:
        img = np.full((300, 300, 3), (0, 180, 255), dtype=np.uint8)
        self.assertFalse(_is_pink_card(img, 100, 150, 80, 30, digit_blocks=[]))

    def test_spatial_pair_allows_half_height_block_mode_distance(self) -> None:
        blocks = [
            TextBlock(
                text="核心潛能:",
                x=500,
                y=400,
                w=80,
                h=20,
                block_type="label",
                matched_label="required",
            ),
            TextBlock(
                text="攻擊強化",
                x=50,
                y=400,
                w=80,
                h=20,
                block_type="potential",
                matched_name="攻擊強化",
            ),
        ]
        parsed = spatial_pair_1_to_n(
            blocks=blocks,
            img_height=1000,
            potentials_db=[],
            raw_img=np.zeros((1000, 1000, 3), dtype=np.uint8),
        )
        self.assertEqual(parsed["required"], ["攻擊強化"])
