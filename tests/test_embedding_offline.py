import os
import sys
import builtins
import importlib.util
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_run_mcp_server_sets_hf_hub_offline_before_server_import():
    script_path = Path(__file__).parent.parent / "run-mcp-server.py"
    real_import = builtins.__import__

    def fake_import(name, globs=None, locs=None, fromlist=(), level=0):
        if name == "mcp_server.server":
            assert os.environ.get("HF_HUB_OFFLINE") == "1", os.environ.get("HF_HUB_OFFLINE")

            class DummyServer:
                def __init__(self):
                    pass

                def handle_request(self, request):
                    return None

            return type("FakeModule", (), {"MCPServer": DummyServer})
        return real_import(name, globs, locs, fromlist, level)

    original_argv = list(sys.argv)
    original_pythonpath = os.environ.get("PYTHONPATH")
    original_hf = os.environ.get("HF_HUB_OFFLINE")

    try:
        os.environ.pop("HF_HUB_OFFLINE", None)
        spec = importlib.util.spec_from_file_location("run_mcp_server_test_module", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=fake_import), \
             patch("sys.stdin", []):
            spec.loader.exec_module(module)
    finally:
        sys.argv[:] = original_argv
        if original_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = original_pythonpath
        if original_hf is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = original_hf
