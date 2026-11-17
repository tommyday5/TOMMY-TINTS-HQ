import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app

ROOT = Path(__file__).resolve().parents[1]


class BookingShowroomTests(unittest.TestCase):
    def test_public_book_entry_and_separate_install_identity(self):
        with app.test_client() as client:
            with client.get("/book", follow_redirects=True) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'YOUR VEHICLE. <span>OUR CRAFT.</span>', response.data)
                self.assertRegex(response.get_data(as_text=True), r"\.hidden\s*\{\s*display\s*:\s*none")
                self.assertIn(b'/static/booking/icon-180.png', response.data)
                self.assertIn(b'/api/public/customer-request/status', response.data)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with client.get("/static/booking/manifest.webmanifest") as response:
                self.assertEqual(response.get_json(force=True)["start_url"], "/book")
        for name in ("tint", "wrap", "paint", "wheel", "detail", "interior", "bespoke", "hero"):
            self.assertTrue((ROOT / "static" / "booking" / f"{name}.webp").is_file())

    def test_interior_restoration_submits_through_existing_public_intake(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as path, patch.dict(os.environ, {
            "HQ_DATA_DIR": path, "HQ_PUBLIC_REQUESTS_ENABLED": "true",
        }), app.test_client() as client:
            response = client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Test Customer", "email": "customer@example.invalid",
                "phone": "07700 900123", "service": "Interior & Leather Restoration",
                "make": "Audi", "model": "A4", "notes": "Work options: Seat bolster",
                "privacy_acknowledged": True,
            })
            self.assertEqual(response.status_code, 201, response.json)
            self.assertTrue(response.json["reference"])
