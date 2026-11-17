import hashlib
import struct
import unittest

from app import app


class IconTests(unittest.TestCase):
    def assert_icon(self, response, size):
        hashes = {
            180: "773109236d3e097b8d79cd9ee7ac2ab7b238326c7d1122bf636028b848a94979",
            512: "7f19cfe8cbec68ab9692e93d77d900c1f9301a882bf764b86b7cebcd67b4133d",
            1024: "b599aafbb5b5720166b25fec009f1cbdcdf5480a851599c4053870bea08dc4df",
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
            with client.get("/static/manifest.webmanifest?v=20261117c") as response:
                self.assertEqual(response.status_code, 200)
                icons = response.get_json(force=True)["icons"]
            self.assertCountEqual(
                [icon["sizes"] for icon in icons],
                ["180x180", "512x512", "1024x1024"],
            )
            for icon in icons:
                with self.subTest(path=icon["src"]):
                    size = int(icon["sizes"].split("x")[0])
                    self.assertEqual(icon["src"], f"/hq-icon-{size}.png?v=20261117c")
                    self.assertEqual(icon["type"], "image/png")
                    self.assert_icon(client.get(icon["src"]), size)

    def test_browser_fallback_icons_serve_original_apple_png(self):
        with app.test_client() as client:
            for path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                with self.subTest(path=path):
                    self.assert_icon(client.get(path), 180)
