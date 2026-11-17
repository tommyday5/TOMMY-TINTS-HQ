"""Regression: the rendered JS regex must match numeric prices, not literal backslashes."""
import unittest
from pathlib import Path

class DraftPriceUiTests(unittest.TestCase):
    def test_pence_validation_uses_js_digit_class(self):
        source=Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        self.assertIn(r'if(!/^\d{1,7}(?:\.\d{1,2})?$/.test(v))',source)
        self.assertNotIn(r'if(!/^\\d{1,7}(?:\\.\\d{1,2})?$/.test(v))',source)

if __name__=="__main__": unittest.main()
