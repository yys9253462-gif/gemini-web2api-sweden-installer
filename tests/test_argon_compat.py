"""Tests for the installer-managed upstream model alias patch."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArgonCompatTests(unittest.TestCase):
    def test_patch_is_idempotent_and_reversible(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('systemctl restart "$SERVICE.service"', installer)
        self.assertIn('systemctl restart gemini-web2api-panel.service', installer)
        match = re.search(
            r"patch_argon_model\(\) \{\s+python3 - \"\$APP_DIR/gemini_web2api/models\.py\" \"\$1\" <<'PY'\n(.*?)\nPY\n}",
            installer,
            re.S,
        )
        self.assertIsNotNone(match, "could not locate installer patch helper")
        helper = match.group(1)

        with tempfile.TemporaryDirectory() as temp:
            model_file = Path(temp) / "models.py"
            original = 'MODELS = {\n    "example": {"mode": 1, "think": 4},\n}\n'
            model_file.write_text(original, encoding="utf-8")

            def run(action):
                return subprocess.run(
                    [sys.executable, "-c", helper, str(model_file), action],
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(run("apply").returncode, 0)
            patched = model_file.read_text(encoding="utf-8")
            self.assertIn('"gemini-4-argon"', patched)
            self.assertIn('"mode": 3, "think": 4', patched)
            self.assertEqual(run("apply").returncode, 0)
            self.assertEqual(model_file.read_text(encoding="utf-8").count('"gemini-4-argon"'), 1)
            self.assertEqual(run("remove").returncode, 0)
            self.assertEqual(model_file.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
