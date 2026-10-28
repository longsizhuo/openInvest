"""Offline label alignment and corrupt-input regression checks (no production DB)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("ta_eval", Path(__file__).with_name("eval_ta_signal.py"))
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


class ReplayChecks(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "prices.csv"
            p.write_text(text)
            ev.load_prices(p)

    def test_calendar_horizon_weekend_and_immaturity(self):
        self.load("date,symbol,close\n2022-01-07,X,100\n2022-02-07,X,110\n")
        self.assertAlmostEqual(ev._fwd_return("X", "2022-01-08", 30), .1)
        self.assertIsNone(ev._fwd_return("X", "2022-01-08", 60))
        self.assertIsNone(ev._fwd_return("X", "2022-01-01", 30))

    def test_duplicate_price_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.load("date,symbol,close\n2022-01-01,X,100\n2022-01-01,X,101\n")

    def test_invalid_prices_rejected(self):
        for value in ("0", "-1", "NaN", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(f"date,symbol,close\n2022-01-01,X,{value}\n")

    def test_missing_symbol_rejected(self):
        self.load("date,symbol,close\n2022-01-01,X,100\n")
        with self.assertRaisesRegex(ValueError, "missing required symbol"):
            ev._fwd_return("Y", "2022-01-01", 30)

    def test_zero_return_not_directional_hit(self):
        self.assertFalse(ev._hit("bullish", 0))
        self.assertFalse(ev._hit("bearish", 0))


if __name__ == "__main__":
    unittest.main()
