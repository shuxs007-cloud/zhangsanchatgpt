import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from opensms_bitbrowser_430 import resolve_current_430_cdp


class Existing430Tests(unittest.TestCase):
    def test_only_profile_active_port_not_parent_or_default_port(self):
        with tempfile.TemporaryDirectory() as directory:
            active=Path(directory)/"DevToolsActivePort"
            active.write_text("49343\n/devtools/browser/exact\n")
            self.assertEqual(resolve_current_430_cdp(active_port=active),"http://127.0.0.1:49343")
            for invalid in ("", "0\n/devtools/browser/exact", "65536\n/devtools/browser/exact",
                            "60904", "49343\n/wrong", "49343\n/devtools/browser/exact\nextra"):
                active.write_text(invalid)
                with self.assertRaisesRegex(RuntimeError,"430_SESSION_UNAVAILABLE"):
                    resolve_current_430_cdp(active_port=active)
            with self.assertRaisesRegex(RuntimeError,"430_SESSION_UNAVAILABLE"):
                resolve_current_430_cdp(active_port=Path(directory)/"missing")


if __name__ == "__main__":
    unittest.main()
