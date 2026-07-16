"""End-to-end tests for ``scripts/install-lookup.sh``.

These build a real (httpx-only) virtualenv and install the ``lookup`` launcher
into a temp bin dir, so they are marked ``slow`` (network + venv creation) and
excluded from the default fast suite. Run them explicitly with::

    pytest -m slow tests/integration/test_install_lookup.py

The fast, network-free control-flow tests live in
``tests/unit/test_install_lookup_cli.py``.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-lookup.sh"


@pytest.fixture(autouse=True)
def ensure_server_running():
    """Override ``tests/integration/conftest.py``'s autouse server fixture.

    These installer tests are fully self-contained — they build their own venv
    and shell out to the script — so they must not boot ``uvicorn main:app``,
    which that conftest's autouse fixture would otherwise start for every test
    in this directory (coupling the run to the whole FastAPI app importing and
    a 30s health probe). Redefining the fixture name here shadows it for this
    module without requesting ``local_server``.
    """
    yield


def _clean_env(tmp_path: Path) -> dict:
    """A hermetic environment for the installer.

    Starts from the real environment (so ``bash``/``pip`` still find PATH, a
    proxy, etc.) but strips anything that would perturb venv creation, pip, or
    import resolution — a leaked ``PYTHONPATH``/``PYTHONHOME``/``VIRTUAL_ENV``
    or ``PIP_*`` mirror flag could otherwise flip these tests green or red for
    reasons unrelated to the installer. HOME/BIN_DIR/LOOKUP_VENV are redirected
    under ``tmp_path`` so nothing touches the real system.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PIP_", "PYTHON", "VIRTUAL_ENV", "XDG_"))
    }
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["PYTHON"] = sys.executable
    env["BIN_DIR"] = str(tmp_path / "bin")
    env["LOOKUP_VENV"] = str(tmp_path / "venv")
    return env


def _run_installer(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the installer with all its outputs redirected under ``tmp_path``."""
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
    )


def _run_launcher(tmp_path: Path, launcher: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the installed launcher under the same hermetic environment."""
    return subprocess.run(
        [str(launcher), *args],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
    )


@pytest.mark.slow
def test_installer_creates_working_launcher(tmp_path: Path) -> None:
    result = _run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    # BIN_DIR (a fresh temp dir) is never on PATH, so the installer must warn
    # and print the exact export line — lock that user-facing guidance in.
    assert "not on your PATH" in result.stderr

    launcher = tmp_path / "bin" / "lookup"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)

    venv_python = tmp_path / "venv" / "bin" / "python"
    content = launcher.read_text()
    assert str(venv_python) in content
    assert "lookup.py" in content

    # The launcher runs end-to-end: --help exits 0 before any network call,
    # which proves the venv python, httpx, and scripts._common all resolve.
    help_result = _run_launcher(tmp_path, launcher, "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "usage" in help_result.stdout.lower()
    assert "/request endpoint" in help_result.stdout


@pytest.mark.slow
def test_installer_is_idempotent(tmp_path: Path) -> None:
    first = _run_installer(tmp_path)
    assert first.returncode == 0, first.stderr
    second = _run_installer(tmp_path)
    assert second.returncode == 0, second.stderr
    # The second run must take the reuse path, not rebuild the venv — otherwise
    # "idempotent" would pass even on a full, slow rebuild.
    assert "Reusing existing virtualenv" in second.stdout

    launcher = tmp_path / "bin" / "lookup"
    help_result = _run_launcher(tmp_path, launcher, "--help")
    assert help_result.returncode == 0, help_result.stderr


@pytest.mark.slow
def test_uninstall_removes_launcher_and_venv(tmp_path: Path) -> None:
    setup = _run_installer(tmp_path)
    assert setup.returncode == 0, setup.stderr
    launcher = tmp_path / "bin" / "lookup"
    venv = tmp_path / "venv"
    assert launcher.is_file()
    assert venv.is_dir()

    result = _run_installer(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not launcher.exists()
    assert not venv.exists()


@pytest.mark.slow
def test_installer_handles_metacharacters_in_paths(tmp_path: Path) -> None:
    """A venv path with shell metacharacters must not be re-evaluated at runtime.

    The generated /bin/sh launcher bakes in the venv-python and script paths; if
    they aren't quoted literally, a ``$`` (or backtick) in the path is expanded
    by /bin/sh on every ``lookup`` call — breaking the path or executing code.
    Installing under a dir containing a space and a ``$`` and then running the
    launcher exercises exactly that.
    """
    weird = tmp_path / "we ird$dir"
    weird.mkdir()
    env = _clean_env(tmp_path)
    env["BIN_DIR"] = str(weird / "bin")
    env["LOOKUP_VENV"] = str(weird / "venv")

    result = subprocess.run(["bash", str(INSTALL_SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    launcher = weird / "bin" / "lookup"
    assert launcher.is_file()
    help_result = subprocess.run([str(launcher), "--help"], env=env, capture_output=True, text=True)
    assert help_result.returncode == 0, help_result.stderr
    assert "/request endpoint" in help_result.stdout
