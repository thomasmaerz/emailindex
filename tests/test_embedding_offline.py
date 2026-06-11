import os
import sys
import builtins
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

SCRIPT_PATH = Path(__file__).parent.parent / "run-mcp-server.py"
SHELL_SCRIPT_PATH = Path(__file__).parent.parent / "run-mcp-server.sh"


def _make_dummy_server_module():
    """Return a fake mcp_server.server module with a no-op MCPServer."""
    class DummyServer:
        def __init__(self):
            pass

        def handle_request(self, request):
            return None

    return type("FakeModule", (), {"MCPServer": DummyServer})


def _exec_script(env_overrides=None, stdin_lines=None):
    """
    Execute run-mcp-server.py in an isolated namespace with mcp_server.server mocked out.

    Returns the fake_globals dict after execution and the env value of HF_HUB_OFFLINE
    that was observed at import time.
    """
    script_code = SCRIPT_PATH.read_text()
    fake_globals = {
        "__name__": "__main__",
        "__file__": str(SCRIPT_PATH),
    }
    real_import = builtins.__import__
    observed = {}

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp_server.server":
            observed["HF_HUB_OFFLINE_at_import"] = os.environ.get("HF_HUB_OFFLINE")
            return _make_dummy_server_module()
        return real_import(name, globals, locals, fromlist, level)

    original_argv = list(sys.argv)
    original_pythonpath = os.environ.get("PYTHONPATH")
    original_hf = os.environ.get("HF_HUB_OFFLINE")

    try:
        # Apply requested env overrides / removals
        if env_overrides is not None:
            for k, v in env_overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        stdin = [] if stdin_lines is None else stdin_lines
        with patch("builtins.__import__", side_effect=fake_import), \
             patch("sys.stdin", stdin):
            exec(compile(script_code, str(SCRIPT_PATH), "exec"), fake_globals)
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

    return fake_globals, observed


# ---------------------------------------------------------------------------
# Tests for run-mcp-server.py
# ---------------------------------------------------------------------------

def test_run_mcp_server_sets_hf_hub_offline_before_server_import():
    script_path = Path(__file__).parent.parent / "run-mcp-server.py"
    script_code = script_path.read_text()

    fake_globals = {
        "__name__": "__main__",
        "__file__": str(script_path),
    }
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp_server.server":
            assert os.environ.get("HF_HUB_OFFLINE") == "1", os.environ.get("HF_HUB_OFFLINE")

            class DummyServer:
                def __init__(self):
                    pass

                def handle_request(self, request):
                    return None

            return type("FakeModule", (), {"MCPServer": DummyServer})
        return real_import(name, globals, locals, fromlist, level)

    original_argv = list(sys.argv)
    original_pythonpath = os.environ.get("PYTHONPATH")
    original_hf = os.environ.get("HF_HUB_OFFLINE")

    try:
        os.environ.pop("HF_HUB_OFFLINE", None)
        with patch("builtins.__import__", side_effect=fake_import), \
             patch("sys.stdin", []):
            exec(compile(script_code, str(script_path), "exec"), fake_globals)
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


def test_run_mcp_server_does_not_override_existing_hf_hub_offline():
    """setdefault must not overwrite a pre-existing HF_HUB_OFFLINE value."""
    _, observed = _exec_script(env_overrides={"HF_HUB_OFFLINE": "0"})
    assert observed["HF_HUB_OFFLINE_at_import"] == "0", (
        "setdefault should preserve a pre-existing value of '0'"
    )


def test_run_mcp_server_preserves_hf_hub_offline_custom_value():
    """Any pre-existing value other than '1' must not be replaced."""
    _, observed = _exec_script(env_overrides={"HF_HUB_OFFLINE": "custom"})
    assert observed["HF_HUB_OFFLINE_at_import"] == "custom"


def test_run_mcp_server_default_hf_hub_offline_is_one():
    """When the variable is absent, the default must be exactly '1'."""
    _, observed = _exec_script(env_overrides={"HF_HUB_OFFLINE": None})
    assert observed["HF_HUB_OFFLINE_at_import"] == "1"


def test_run_mcp_server_empty_string_hf_hub_offline_not_overridden():
    """setdefault only sets when the key is missing; an empty string is a valid value."""
    _, observed = _exec_script(env_overrides={"HF_HUB_OFFLINE": ""})
    assert observed["HF_HUB_OFFLINE_at_import"] == "", (
        "setdefault must not replace an empty-string value"
    )


def test_run_mcp_server_hf_hub_offline_set_before_pythonpath_modification():
    """
    Regression: HF_HUB_OFFLINE line must appear before the PYTHONPATH / sys.path
    manipulation in the source file so the env is correct before any import.
    """
    source = SCRIPT_PATH.read_text()
    hf_pos = source.index("HF_HUB_OFFLINE")
    pythonpath_pos = source.index("PYTHONPATH")
    assert hf_pos < pythonpath_pos, (
        "HF_HUB_OFFLINE must be set before PYTHONPATH is modified"
    )


def test_run_mcp_server_setdefault_appears_before_import_in_source():
    """The setdefault call must precede the 'from mcp_server.server import' line."""
    source = SCRIPT_PATH.read_text()
    hf_pos = source.index("HF_HUB_OFFLINE")
    import_pos = source.index("from mcp_server.server import")
    assert hf_pos < import_pos, (
        "HF_HUB_OFFLINE must be set before mcp_server.server is imported"
    )


def test_run_mcp_server_uses_setdefault_not_direct_assignment():
    """The script must use os.environ.setdefault so pre-existing values are preserved."""
    source = SCRIPT_PATH.read_text()
    assert 'os.environ.setdefault("HF_HUB_OFFLINE"' in source or \
           "os.environ.setdefault('HF_HUB_OFFLINE'" in source, (
        "Script must use os.environ.setdefault for HF_HUB_OFFLINE"
    )


def test_run_mcp_server_appends_stdio_to_argv():
    """The script appends '--stdio' to sys.argv before importing the server."""
    original_argv = list(sys.argv)
    original_pythonpath = os.environ.get("PYTHONPATH")
    original_hf = os.environ.get("HF_HUB_OFFLINE")
    argv_at_import = {}

    script_code = SCRIPT_PATH.read_text()
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp_server.server":
            argv_at_import["value"] = list(sys.argv)
            return _make_dummy_server_module()
        return real_import(name, globals, locals, fromlist, level)

    try:
        os.environ.pop("HF_HUB_OFFLINE", None)
        fake_globals = {"__name__": "__main__", "__file__": str(SCRIPT_PATH)}
        with patch("builtins.__import__", side_effect=fake_import), \
             patch("sys.stdin", []):
            exec(compile(script_code, str(SCRIPT_PATH), "exec"), fake_globals)
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

    assert "--stdio" in argv_at_import.get("value", []), (
        "--stdio must be appended to sys.argv before server import"
    )


# ---------------------------------------------------------------------------
# Tests for run-mcp-server.sh
# ---------------------------------------------------------------------------

def test_shell_script_exports_hf_hub_offline():
    """The shell script must export HF_HUB_OFFLINE."""
    source = SHELL_SCRIPT_PATH.read_text()
    assert "HF_HUB_OFFLINE" in source, "HF_HUB_OFFLINE must appear in run-mcp-server.sh"
    # The line must be an export statement
    export_lines = [l for l in source.splitlines() if "HF_HUB_OFFLINE" in l]
    assert any(l.strip().startswith("export") for l in export_lines), (
        "HF_HUB_OFFLINE must be exported in run-mcp-server.sh"
    )


def test_shell_script_uses_default_value_expansion():
    """The shell script must use ${HF_HUB_OFFLINE:-1} to preserve pre-existing values."""
    source = SHELL_SCRIPT_PATH.read_text()
    assert "${HF_HUB_OFFLINE:-1}" in source, (
        "run-mcp-server.sh must use ${HF_HUB_OFFLINE:-1} default expansion"
    )


def test_shell_script_default_is_one():
    """The default value in the shell script expansion must be '1'."""
    source = SHELL_SCRIPT_PATH.read_text()
    # Extract the default from ${HF_HUB_OFFLINE:-<default>}
    import re
    match = re.search(r"\$\{HF_HUB_OFFLINE:-([^}]+)\}", source)
    assert match is not None, "Could not find ${HF_HUB_OFFLINE:-...} in run-mcp-server.sh"
    assert match.group(1) == "1", (
        f"Default value must be '1', got '{match.group(1)}'"
    )


def test_shell_script_hf_hub_offline_before_exec():
    """HF_HUB_OFFLINE export must appear before the exec line in the shell script."""
    source = SHELL_SCRIPT_PATH.read_text()
    lines = source.splitlines()
    hf_line_idx = next(
        (i for i, l in enumerate(lines) if "HF_HUB_OFFLINE" in l), None
    )
    exec_line_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("exec ")), None
    )
    assert hf_line_idx is not None, "No HF_HUB_OFFLINE line found in shell script"
    assert exec_line_idx is not None, "No exec line found in shell script"
    assert hf_line_idx < exec_line_idx, (
        "HF_HUB_OFFLINE must be exported before the exec line"
    )


def test_shell_script_is_executable_bash_script():
    """The shell script must have a bash shebang."""
    source = SHELL_SCRIPT_PATH.read_text()
    assert source.startswith("#!/bin/bash"), (
        "run-mcp-server.sh must start with #!/bin/bash"
    )
