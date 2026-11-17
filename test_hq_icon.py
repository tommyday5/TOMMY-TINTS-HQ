import hashlib
import struct
import unittest

from app import app


class IconTests(unittest.TestCase):
    def assert_icon(self, response, size):
        hashes = {
            180: "b5154fbf7a88a59f4d46b3ce7a36f04b571c3d5c9faad213f0c63d0ed8288810",
            512: "0ae06b0e9d419be991bec5ff11fd5fb7d00fbe4dc6ca0c4ef58185f966d38e1b",
            1024: "fa82b870b557cf77fc15a31718889c6345f02105e29e4cb16517c4c2fd8d7dee",
        }
        with response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            data = response.data
            self.assertEqual(data[:16], b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))
            # Pin the supplied artwork so a lookalike with the same dimensions fails.
            self.assertEqual(hashlib.sha256(data).hexdigest(), hashes[size])

    def test_manifest_icons_serve_original_pngs(self):
        with app.test_client() as client:
            with client.get("/static/manifest.webmanifest?v=20261117d") as response:
                self.assertEqual(response.status_code, 200)
                icons = response.get_json(force=True)["icons"]
            self.assertCountEqual(
                [icon["sizes"] for icon in icons],
                ["180x180", "512x512", "1024x1024"],
            )
            for icon in icons:
                with self.subTest(path=icon["src"]):
                    size = int(icon["sizes"].split("x")[0])
                    self.assertEqual(icon["src"], f"/hq-icon-{size}.png?v=20261117d")
                    self.assertEqual(icon["type"], "image/png")
                    self.assert_icon(client.get(icon["src"]), size)

    def test_browser_fallback_icons_serve_original_apple_png(self):
        with app.test_client() as client:
            for path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                with self.subTest(path=path):
                    self.assert_icon(client.get(path), 180)
