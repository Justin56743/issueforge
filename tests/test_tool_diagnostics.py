from pathlib import Path
import pytest

from issueforge.agents.agy_runner import ToolCallDiagnostic, ToolCallNormalizer


def test_tool_call_normalizer_path_containment(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    # 1. Normal relative path
    norm_rel = ToolCallNormalizer.normalize_file_path(sandbox_dir, "src/main.py")
    assert norm_rel == (sandbox_dir / "src/main.py").resolve()

    # 2. Path with quotes and spaces
    norm_quoted = ToolCallNormalizer.normalize_file_path(sandbox_dir, " 'utils/helper.py' ")
    assert norm_quoted == (sandbox_dir / "utils/helper.py").resolve()

    # 3. Path traversal attempting escape via ../../etc/passwd
    norm_escape = ToolCallNormalizer.normalize_file_path(sandbox_dir, "../../etc/passwd")
    # Must be safely confined within sandbox_dir
    assert norm_escape.is_relative_to(sandbox_dir.resolve())
    assert "etc/passwd" in str(norm_escape)

    # 4. Absolute path outside sandbox
    norm_outside = ToolCallNormalizer.normalize_file_path(sandbox_dir, "/var/log/syslog")
    assert norm_outside.is_relative_to(sandbox_dir.resolve())


def test_tool_call_normalizer_param_sanitization(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    raw_params = {
        "TargetFile": "app/config.py",
        "Overwrite": "true",
        "StartLine": "12",
        "EndLine": "25",
        "CommandLine": "ls -la",
    }

    sanitized = ToolCallNormalizer.sanitize_tool_parameters("write_to_file", raw_params, sandbox_dir)
    assert sanitized["Overwrite"] is True
    assert sanitized["StartLine"] == 12
    assert sanitized["EndLine"] == 25
    assert Path(sanitized["TargetFile"]).is_relative_to(sandbox_dir.resolve())


def test_tool_call_diagnostic_detection():
    # 1. Invalid args
    diag_inv = ToolCallDiagnostic.diagnose_error(
        "write_to_file",
        "convert tool call for permissions: model output error: invalid tool call error (invalid_args)"
    )
    assert diag_inv["category"] == "INVALID_ARGS"
    assert diag_inv["auto_recoverable"] is True
    assert "recovering" in diag_inv["recommendation"].lower() or "reformulate" in diag_inv["recommendation"].lower()

    # 2. Permission denied
    diag_perm = ToolCallDiagnostic.diagnose_error("run_command", "Permission denied: /bin/chmod")
    assert diag_perm["category"] == "PERMISSIONS"

    # 3. Path not found
    diag_path = ToolCallDiagnostic.diagnose_error("view_file", "No such file or directory: app/secret.py")
    assert diag_path["category"] == "PATH_NOT_FOUND"

    # 4. Path traversal
    diag_trav = ToolCallDiagnostic.diagnose_error("write_to_file", "Attempted path traversal outside sandbox")
    assert diag_trav["category"] == "PATH_TRAVERSAL"

    # 5. Syntax / JSON
    diag_syn = ToolCallDiagnostic.diagnose_error("replace_file_content", "JSONDecodeError: Expecting value: line 1")
    assert diag_syn["category"] == "SYNTAX"
