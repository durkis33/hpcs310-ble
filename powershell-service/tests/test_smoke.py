import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class SmokeTests(unittest.TestCase):
    def test_version(self):
        v=(ROOT/"VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(v,r"^\d+\.\d+\.\d+$")

    def test_config(self):
        json.loads((ROOT/"config.example.json").read_text(encoding="utf-8"))

    def test_openapi(self):
        text=(ROOT/"openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("operationId: exec",text)
        self.assertIn("operationId: getJob",text)

if __name__=="__main__":
    unittest.main()
