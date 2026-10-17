from __future__ import annotations

import argparse
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_dispatch_module():
    test_path = Path(__file__).resolve()
    workspace_script = test_path.parents[3] / "scripts" / "opensms_process_document.py"
    package_script = test_path.parents[1] / "opensms_process_document.py"
    script = workspace_script if workspace_script.is_file() else package_script
    import sys

    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("opensms_process_document", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


dispatch = load_dispatch_module()


class DispatchTests(unittest.TestCase):
    def make_args(self, root: Path) -> argparse.Namespace:
        export = root / "export4day.xlsx"
        association = root / "association.xlsx"
        export.write_bytes(b"fresh-export")
        association.write_bytes(b"fresh-association")
        return argparse.Namespace(
            export=export,
            association=association,
            server_date="2026-08-19",
            output_dir=root / "output",
            base_url=None,
            token=None,
            api_config=root / "OPENSMS_LAN_API.md",
            local_builder=None,
            force_local=False,
            connect_timeout=0.1,
            request_timeout=1,
            poll_interval=0.01,
            wait_timeout=1,
            idempotency_key=None,
            insecure=False,
        )

    def test_parses_primary_backup_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "OPENSMS_LAN_API.md"
            config.write_text(
                "- 主地址（有线）：`http://192.168.8.246:8787`\n"
                "- 备用地址（Wi-Fi）：`http://192.168.8.222:8787`\n"
                "- Bearer Token：`test-token`\n",
                encoding="utf-8",
            )
            urls, token = dispatch.parse_api_config(config)
            self.assertEqual(
                urls,
                ["http://192.168.8.246:8787", "http://192.168.8.222:8787"],
            )
            self.assertEqual(token, "test-token")

    def test_remote_success_does_not_run_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root)
            remote = {
                "processing_mode": "remote_api",
                "output_path": str(root / "remote.xlsx"),
                "receipt_path": str(root / "remote.json"),
            }
            with (
                mock.patch.object(
                    dispatch,
                    "resolve_api_settings",
                    return_value=(["http://server:8787"], "token", "test"),
                ),
                mock.patch.object(
                    dispatch,
                    "remote_process",
                    return_value=(remote, [{"available": True}], None),
                ),
                mock.patch.object(dispatch, "run_local") as local,
            ):
                payload, code = dispatch.execute(args)
            self.assertEqual(code, 0)
            self.assertEqual(payload["processing_mode"], "remote_api")
            local.assert_not_called()

    def test_unavailable_api_falls_back_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root)
            local_result = {
                "result": "成功",
                "output_path": str(root / "local.xlsx"),
                "receipt_path": str(root / "local.json"),
            }
            with (
                mock.patch.object(
                    dispatch,
                    "resolve_api_settings",
                    return_value=(["http://offline:8787"], "token", "test"),
                ),
                mock.patch.object(
                    dispatch,
                    "remote_process",
                    return_value=(
                        None,
                        [{"available": False, "reason": "health_unreachable"}],
                        "health_unreachable",
                    ),
                ),
                mock.patch.object(
                    dispatch, "find_local_builder", return_value=root / "builder.sh"
                ),
                mock.patch.object(dispatch, "run_local", return_value=local_result) as local,
            ):
                payload, code = dispatch.execute(args)
            self.assertEqual(code, 0)
            self.assertEqual(payload["processing_mode"], "local_fallback")
            self.assertEqual(payload["fallback_reason"], "health_unreachable")
            local.assert_called_once()
            self.assertTrue(Path(payload["dispatch_receipt_path"]).is_file())

    def test_remote_processing_failure_is_not_hidden_by_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root)
            with (
                mock.patch.object(
                    dispatch,
                    "resolve_api_settings",
                    return_value=(["http://server:8787"], "token", "test"),
                ),
                mock.patch.object(
                    dispatch,
                    "remote_process",
                    side_effect=dispatch.RemoteProcessingError("远端复核失败"),
                ),
                mock.patch.object(dispatch, "run_local") as local,
            ):
                payload, code = dispatch.execute(args)
            self.assertEqual(code, 4)
            self.assertEqual(
                payload["processing_mode"], "remote_api_failed_no_local_fallback"
            )
            self.assertIn("远端复核失败", payload["error"])
            local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
