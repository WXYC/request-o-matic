"""Fast, network-free tests for ``scripts/install-lookup.sh`` control flow.

These exercise the installer's argument dispatch and guard clauses *without*
building a venv or hitting the network, so they run in the default fast suite
(CI's ``pytest tests/unit/``). The expensive real-venv end-to-end coverage lives
in ``tests/integration/test_install_lookup.py`` (``slow``-marked).

Every invocation redirects HOME/BIN_DIR/LOOKUP_VENV under ``tmp_path`` so no
test touches the real ``~/.local`` tree.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-lookup.sh"


def _run(tmp_path: Path, *args: str, python: str = "python3") -> subprocess.CompletedProcess:
    """Run the installer with filesystem side effects confined to ``tmp_path``."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["BIN_DIR"] = str(tmp_path / "bin")
    env["LOOKUP_VENV"] = str(tmp_path / "venv")
    env["PYTHON"] = python
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_help_exits_zero_and_prints_usage(tmp_path: Path) -> None:
    result = _run(tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    # Regression guard: usage() must not leak shell source. The old sed-based
    # extraction over-read its own header and spilled `set -euo pipefail` (and
    # blank lines) into --help output.
    assert "set -euo pipefail" not in result.stdout


def test_h_alias_matches_help(tmp_path: Path) -> None:
    assert _run(tmp_path, "-h").stdout == _run(tmp_path, "--help").stdout


def test_unknown_argument_exits_two(tmp_path: Path) -> None:
    result = _run(tmp_path, "--bogus")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr
    # An unknown arg should still show usage to orient the user.
    assert "Usage:" in result.stdout


def test_uninstall_with_nothing_installed_is_a_noop(tmp_path: Path) -> None:
    result = _run(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert "Nothing to uninstall" in result.stdout


def test_missing_python_interpreter_is_rejected(tmp_path: Path) -> None:
    # Points PYTHON at a path that does not exist: install() must fail fast,
    # before creating any venv, rather than trip over it later.
    result = _run(tmp_path, python=str(tmp_path / "no-such-python"))
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert not (tmp_path / "venv").exists()


def test_old_python_is_rejected(tmp_path: Path) -> None:
    # A Python that reports < 3.10 must be rejected before any venv is built.
    # The stub mimics just enough of the interpreter: the version-probe `-c`
    # exits non-zero (i.e. version_info < (3, 10)) and `-V` prints an old label.
    stub = tmp_path / "old-python"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  -V) echo 'Python 3.9.18' ;;\n"
        "  -c) exit 1 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)

    result = _run(tmp_path, python=str(stub))
    assert result.returncode == 1
    assert "3.10" in result.stderr
    assert not (tmp_path / "venv").exists()
