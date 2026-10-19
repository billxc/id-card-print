# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.26,<3", "opencv-python>=4.10,<5", "Pillow>=10.4,<13",
#   "pillow-heif>=0.18,<2", "reportlab>=4.2,<5", "pypdf>=5,<7",
# ]
# ///
"""Synthetic shapes only: no real IDs are needed or stored for these tests."""

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

import cv2
import id_card_pdf as app
import numpy as np
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream


def synthetic_photo(background=35, foreground=225):
    image = np.full((900, 1200, 3), background, dtype=np.uint8)
    corners = np.float32([[210, 190], [1000, 250], [950, 750], [180, 660]])
    cv2.fillConvexPoly(image, corners.astype(np.int32), (foreground,) * 3)
    cv2.putText(image, "SAMPLE", (330, 440), cv2.FONT_HERSHEY_SIMPLEX,
                2, (120, 120, 120), 4)
    return image, corners


class GeometryTests(unittest.TestCase):
    def test_corner_order_is_independent_of_input_order(self):
        _, corners = synthetic_photo()
        expected = app.order_corners(corners)
        for order in ([2, 0, 3, 1], [3, 2, 1, 0], [1, 2, 3, 0]):
            np.testing.assert_array_equal(app.order_corners(corners[order]), expected)

    def test_reject_invalid_corners(self):
        for points in ([[0, 0]] * 4,
                       [[0, 0], [20, 0], [40, 0], [60, 0]],
                       [[0, 0], [500, 0], [0, 500], [10, 10]],
                       [[0, 0], [500, 0], [0, 500], [float("nan"), 10]]):
            with self.assertRaises(ValueError):
                app.order_corners(points)

    def test_detect_perspective_card_on_light_and_dark_background(self):
        for bg, fg in ((35, 225), (230, 40)):
            with self.subTest(background=bg):
                image, corners = synthetic_photo(bg, fg)
                detected = app.detect_card(image)
                np.testing.assert_allclose(detected, app.order_corners(corners), atol=12)

    def test_large_image_coordinates_restored(self):
        image, corners = synthetic_photo()
        image = cv2.resize(image, (2400, 1800))
        detected = app.detect_card(image)
        np.testing.assert_allclose(detected, app.order_corners(corners * 2), atol=20)

    def test_blank_image_does_not_fall_back_to_full_photo(self):
        with self.assertRaises(ValueError):
            app.detect_card(np.full((800, 1200, 3), 255, dtype=np.uint8))

    def test_rectification_dimensions_and_corner_colors(self):
        image, corners = synthetic_photo()
        colors = [(0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 255, 255)]
        for corner, color in zip(corners, colors):
            cv2.circle(image, tuple(corner.astype(int)), 30, color, -1)
        card = app.rectify_card(image, corners)
        self.assertEqual(card.shape[:2], (638, 1011))
        for position, color in zip(((5, 5), (5, -6), (-6, -6), (-6, 5)), colors):
            np.testing.assert_allclose(card[position], color, atol=10)

    def test_portrait_card_becomes_landscape_without_mirroring(self):
        image, corners = synthetic_photo()
        expected = app.rectify_card(image, corners)
        rotated = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        rotated_corners = np.column_stack((image.shape[0] - 1 - corners[:, 1], corners[:, 0]))
        actual = app.rectify_card(rotated, rotated_corners)
        # Depending on geometric starting corner, 180 degrees is ambiguous.
        error = min(np.abs(actual.astype(float) - other).mean()
                    for other in (expected, cv2.rotate(expected, cv2.ROTATE_180)))
        self.assertLess(error, 1)


class ImageAndPdfTests(unittest.TestCase):
    def test_exif_orientation_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phone.jpg"
            image = Image.new("RGB", (200, 100), "white")
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
            self.assertEqual(app.load_photo(path).shape[:2], (200, 100))

    def test_transparency_composited_on_white(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transparent.png"
            Image.new("RGBA", (200, 100), (0, 0, 0, 0)).save(path)
            self.assertTrue((app.load_photo(path) == 255).all())

    def test_pdf_page_and_all_four_physical_image_sizes(self):
        cards = [np.full((100, 160, 3), value, np.uint8) for value in (20, 60, 100, 140)]
        reader = PdfReader(BytesIO(app.render_pdf(cards)))
        self.assertEqual(len(reader.pages), 1)
        page = reader.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width) / app.mm, 210, places=3)
        self.assertAlmostEqual(float(page.mediabox.height) / app.mm, 297, places=3)
        self.assertEqual(reader.trailer["/Root"]["/ViewerPreferences"]["/PrintScaling"], "/None")
        content = ContentStream(page.get_contents(), reader)
        placements = []
        current = None
        for operands, operator in content.operations:
            if operator == b"cm":
                current = list(map(float, operands))
            elif operator == b"Do":
                placements.append(current)
        self.assertEqual(len(placements), 4)
        for matrix, (x, y) in zip(placements, app.card_positions()):
            a, b, c, d, tx, ty = matrix
            self.assertAlmostEqual(a / app.mm, 85.6, places=3)
            self.assertAlmostEqual(d / app.mm, 54, places=3)
            self.assertEqual((b, c), (0, 0))
            self.assertAlmostEqual(tx, x, places=3)
            self.assertAlmostEqual(ty, y, places=3)
            self.assertGreaterEqual(tx, 0)
            self.assertGreaterEqual(ty, 0)
            self.assertLessEqual(tx + a, float(page.mediabox.width))
            self.assertLessEqual(ty + d, float(page.mediabox.height))

    def test_pdf_rejects_wrong_card_count(self):
        with self.assertRaises(ValueError):
            app.render_pdf([])


class CliTests(unittest.TestCase):
    def run_main(self, args):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return app.main(args)

    def test_end_to_end_auto_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.png"
            output = Path(directory) / "result.pdf"
            image, _ = synthetic_photo()
            cv2.imwrite(str(source), image)
            args = [str(source)] * 4 + ["-o", str(output)]
            self.assertEqual(self.run_main(args), 0)
            self.assertEqual(len(PdfReader(output).pages), 1)
            original = output.read_bytes()
            self.assertEqual(self.run_main(args), 1)
            self.assertEqual(output.read_bytes(), original)

    def test_failure_does_not_create_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.pdf"
            source = Path(directory) / "blank.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            self.assertEqual(self.run_main([str(source)] * 4 + ["-o", str(output)]), 1)
            self.assertFalse(output.exists())

    def test_already_cropped_and_per_card_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cropped.png"
            output = Path(directory) / "result.pdf"
            Image.new("RGB", (856, 540), "white").save(source)
            args = [str(source)] * 4 + ["--already-cropped", "--rotate", "0", "180", "0", "180",
                                      "-o", str(output)]
            with patch.object(app.cv2, "rotate", wraps=cv2.rotate) as rotate:
                self.assertEqual(self.run_main(args), 0)
                self.assertEqual(rotate.call_count, 2)

    def test_manual_mode_uses_selected_corners(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.png"
            output = Path(directory) / "result.pdf"
            image, corners = synthetic_photo()
            cv2.imwrite(str(source), image)
            with patch.object(app, "select_corners", return_value=corners) as select:
                self.assertEqual(self.run_main([str(source)] * 4 + ["--manual", "-o", str(output)]), 0)
                self.assertEqual(select.call_count, 4)

    def test_review_offers_manual_selection_when_detection_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "blank.png"
            output = Path(directory) / "result.pdf"
            Image.new("RGB", (1000, 800), "white").save(source)
            corners = np.float32([[100, 100], [900, 100], [900, 600], [100, 600]])
            with patch.object(app, "select_corners", return_value=corners) as select:
                self.assertEqual(self.run_main([str(source)] * 4 + ["--review", "-o", str(output)]), 0)
                self.assertTrue(all(call.kwargs["initial"] is None for call in select.call_args_list))


if __name__ == "__main__":
    unittest.main()
