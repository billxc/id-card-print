# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.26,<3", "opencv-python>=4.10,<5", "Pillow>=10.4,<13",
#   "pillow-heif>=0.18,<2", "reportlab>=4.2,<5", "Flask>=3.1,<4", "pypdf>=5,<7",
# ]
# ///
"""HTTP tests with synthetic cards, including privacy and input validation."""

import base64
import unittest
from io import BytesIO
from unittest.mock import patch

import cv2
import numpy as np
import web
from pypdf import PdfReader
from pypdf.generic import ContentStream
from test_id_card_pdf import synthetic_photo


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image, corners = synthetic_photo()
        cls.image = base64.b64encode(cv2.imencode(".png", image)[1]).decode()
        cls.corners = (corners / [image.shape[1] - 1, image.shape[0] - 1]).tolist()
        cls.card = {"image": cls.image, "corners": cls.corners, "rotation": 0}

    def setUp(self):
        self.app = web.create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {"X-Local-Token": self.app.config["LOCAL_TOKEN"]}

    def post(self, path, data, **kwargs):
        return self.client.post(path, json=data, headers=self.headers, **kwargs)

    def test_index_and_static_files(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.app.config["LOCAL_TOKEN"], response.text)
        self.assertNotIn("__LOCAL_TOKEN__", response.text)
        for path in ("/static/app.css", "/static/app.js"):
            with self.client.get(path) as static_response:
                self.assertEqual(static_response.status_code, 200)

    def test_responses_are_not_cached_or_framed(self):
        for response in (self.client.get("/"), self.post("/api/analyze", {})):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_reject_missing_or_wrong_token(self):
        for headers in ({}, {"X-Local-Token": "invalid"}):
            response = self.client.post("/api/analyze", json={"image": self.image}, headers=headers)
            self.assertEqual(response.status_code, 403)

    def test_reject_foreign_host(self):
        self.assertEqual(self.client.get("/", base_url="http://evil.example").status_code, 403)

    def test_reject_cross_origin_and_cross_site(self):
        for extra in ({"Origin": "https://evil.example"}, {"Origin": "http://localhost:9999"},
                      {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            response = self.client.post("/api/analyze", json={"image": self.image},
                                        headers={**self.headers, **extra})
            self.assertEqual(response.status_code, 403)

    def test_accept_same_origin(self):
        response = self.client.post("/api/analyze", json={"image": self.image},
                                    headers={**self.headers, "Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)

    def test_reject_non_json_and_non_object(self):
        response = self.client.post("/api/analyze", data="photo", headers=self.headers)
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.post("/api/analyze", []).status_code, 400)
        response = self.client.post("/api/analyze", data="{broken", content_type="application/json",
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_analyze_returns_oriented_preview_and_normalized_corners(self):
        response = self.post("/api/analyze", {"image": self.image})
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual((data["width"], data["height"]), (1200, 900))
        self.assertTrue(data["preview"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(data["crop"].startswith("data:image/jpeg;base64,"))
        np.testing.assert_allclose(data["corners"], self.corners, atol=0.015)

    def test_failed_detection_requires_manual_corners(self):
        blank = np.full((300, 500, 3), 255, dtype=np.uint8)
        image = base64.b64encode(cv2.imencode(".png", blank)[1]).decode()
        response = self.post("/api/analyze", {"image": image})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["corners"])
        self.assertIsNone(response.json["crop"])
        self.assertTrue(response.json["preview"])

    def test_invalid_photo_does_not_expose_tracebacks(self):
        for value in (None, "!not base64!", "", "SGVsbG8=", [], 12):
            response = self.post("/api/analyze", {"image": value})
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.json)
            self.assertNotIn("Traceback", response.text)

    def test_file_byte_limit(self):
        with patch.object(web, "MAX_FILE_BYTES", 100):
            response = self.post("/api/analyze", {"image": self.image})
        self.assertEqual(response.status_code, 400)

    def test_pixel_limit_before_conversion(self):
        with patch.object(web, "MAX_PIXELS", 100):
            response = self.post("/api/analyze", {"image": self.image})
        self.assertEqual(response.status_code, 400)
        self.assertIn("分辨率", response.json["error"])

    def test_request_body_limit(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        response = self.post("/api/analyze", {"image": self.image})
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)

    def test_crop_and_rotation(self):
        response = self.post("/api/crop", self.card)
        self.assertEqual(response.status_code, 200)
        rotated = self.post("/api/crop", {**self.card, "rotation": 180})
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(response.json["crop"], rotated.json["crop"])

    def test_invalid_corners(self):
        for corners in (None, [], [[0, 0]] * 4,
                        [[-0.1, 0], [1, 0], [1, 1], [0, 1]],
                        [[0, 0], [2, 0], [1, 1], [0, 1]],
                        [[0, 0], [1, 0], [1, float("nan")], [0, 1]]):
            response = self.post("/api/crop", {**self.card, "corners": corners})
            self.assertEqual(response.status_code, 400)

    def test_invalid_rotation(self):
        for rotation in (True, 90, "180", None):
            response = self.post("/api/crop", {**self.card, "rotation": rotation})
            self.assertEqual(response.status_code, 400)

    def test_pdf_is_one_a4_page_and_four_exact_size_cards(self):
        response = self.post("/api/pdf", {"cards": [self.card] * 4, "dpi": 300})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        pdf = PdfReader(BytesIO(response.data))
        self.assertEqual(len(pdf.pages), 1)
        page = pdf.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width) / web.core.mm, 210, places=3)
        self.assertAlmostEqual(float(page.mediabox.height) / web.core.mm, 297, places=3)
        transforms = []
        current = None
        for operands, operator in ContentStream(page.get_contents(), pdf).operations:
            if operator == b"cm":
                current = list(map(float, operands))
            elif operator == b"Do":
                transforms.append(current)
        self.assertEqual(len(transforms), 4)
        for a, b, c, d, x, y in transforms:
            self.assertAlmostEqual(a / web.core.mm, 85.6, places=3)
            self.assertAlmostEqual(d / web.core.mm, 54, places=3)

    def test_pdf_rejects_wrong_card_count_and_dpi(self):
        for cards in (None, [], [self.card] * 3, [self.card] * 5, "bad", [None] * 4):
            self.assertEqual(self.post("/api/pdf", {"cards": cards}).status_code, 400)
        for dpi in (True, 0, 1200, "300"):
            self.assertEqual(self.post("/api/pdf", {"cards": [self.card] * 4, "dpi": dpi}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
