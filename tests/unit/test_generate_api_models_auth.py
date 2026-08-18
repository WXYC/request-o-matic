"""Unit tests for the authenticated download path of ``scripts/generate_api_models.sh`` (#268).

The Codegen Freshness CI job fetches ``api.yaml`` from raw.githubusercontent.com,
which rate-limits anonymous requests per source IP -- and Actions runners share
IP pools, so the job is exposed to intermittent 429s. WXYC/library-metadata-lookup#1205
documented three such failures in a single day on the job this one mirrors, each
needing a manual ``gh run rerun --failed``; whether a given run passes is IP-pool
luck. This repo's copy of the script had the same anonymous shape, so the fix was
ported before the shared pool has a bad day here too.

Three safety properties are pinned:

- the token travels on curl's **stdin** (``-H @-``), never argv (visible in
  ``ps``) and never stdout/stderr;
- a failed authenticated attempt retries once anonymously -- GitHub 404s (not
  401s) raw requests carrying a stale token, so without the fallback an expired
  ambient token would turn previously-working local runs into hard 404s;
- the token is dropped from the environment at the top of the script, before
  the source-resolution branch, so no child process inherits it on EITHER arm
  -- the sibling-checkout arm never downloads, so a scrub placed inside the
  download branch misses the default local invocation entirely.

See the script's header comment for the canonical rationale; this module pins
the properties, it does not restate the reasoning.

Each test runs the real script via subprocess, hermetically: a stub ``curl``
first on ``PATH`` records each invocation's argv, stdin, and token-bearing
environment (so no network), and the script is copied into a throwaway repo
under ``tmp_path`` with no sibling ``wxyc-shared`` beside it, which is what
forces the download branch. Stub ``datamodel-codegen`` and ``ruff`` stand in for
the codegen stages that follow, which are out of scope here.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_api_models.sh"

_TOKEN = "ghs_rom268_fake_token_for_tests"
_URL = "https://raw.githubusercontent.com/WXYC/wxyc-shared/main/api.yaml"

# The org-pinned curl idiom for this exact download (mirrors wxyc-shared's
# generate-python-models.sh and LML's script): --retry also covers 429/5xx with
# Retry-After, which directly serves this ticket's goal.
_CURL_COMMON = ["-sSfL", "--max-time", "30", "--retry", "3", "--retry-max-time", "60"]


def _install_curl_stub(tmp_path: Path) -> None:
    """Install a fake ``curl`` in ``tmp_path/bin`` that records every invocation.

    Call *n* writes ``curl_calls/<n>.argv`` (one argv element per line -- a
    header value like ``Authorization: Bearer x`` is a single element, spaces
    and all), ``<n>.stdin`` (whatever was piped in), and ``<n>.env`` (any
    ``GITHUB_TOKEN``/``GH_TOKEN`` lines in the child environment). Appending a
    new numbered record per call, rather than truncating one file, is what
    makes the auth-then-anonymous-fallback retry assertable as two distinct
    calls.

    Failure knob: if ``curl_fail_remaining`` exists, calls up to and including
    the number it contains exit 22 (curl's HTTP-error code) after recording --
    write ``1`` to fail only the first call, ``2`` to fail both. Compared
    against the call ordinal the stub already computes, so the marker file is
    read-only state rather than a decrementing counter.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'calls_dir="{tmp_path / "curl_calls"}"\n'
        f'fail_marker="{tmp_path / "curl_fail_remaining"}"\n'
        'mkdir -p "$calls_dir"\n'
        "n=$(find \"$calls_dir\" -name '*.argv' | wc -l | tr -d ' ')\n"
        "n=$((n + 1))\n"
        ': > "$calls_dir/$n.argv"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$calls_dir/$n.argv"; done\n'
        'cat > "$calls_dir/$n.stdin"\n'
        "env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"$calls_dir/$n.env\" || true\n"
        '[ -e "$fail_marker" ] && [ "$n" -le "$(cat "$fail_marker")" ] && exit 22\n'
        "exit 0\n"
    )
    stub.chmod(0o755)


def _install_env_recording_stub(tmp_path: Path, name: str) -> None:
    """Install a stub ``name`` on ``PATH`` that records its own environment.

    Used for the codegen/ruff stages, which the curl stub can't stand in for:
    the sibling-checkout arm never runs curl at all, so the token-scrub
    property has to be observed at the tools that actually run after it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"{tmp_path}/{name}.env\" || true\n"
        "exit 0\n"
    )
    stub.chmod(0o755)


def _hermetic_env() -> dict[str, str]:
    """``os.environ`` minus everything that would let the ambient shell decide
    which arm of the script runs.

    ``GITHUB_TOKEN``/``GH_TOKEN`` because CI exports one and each test controls
    exactly which the script sees. ``GIT_DIR``/``GIT_WORK_TREE`` because git
    exports them into hooks and ``rebase --exec`` subprocesses, and either one
    overrides the throwaway repo entirely -- ``git rev-parse --git-common-dir``
    then resolves the sibling path against SOMEONE ELSE'S checkout, silently
    routing every test down the no-download arm. On this machine that is not
    hypothetical: ../wxyc-shared/api.yaml exists, so running this suite from a
    pre-push hook took 7 of the 10 tests red for a reason unrelated to the
    change under test.
    """
    env = dict(os.environ)
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(name, None)
    return env


def _copy_script(tmp_path: Path, *, with_sibling: bool) -> Path:
    """Lay out a throwaway repo around a copy of the script; return the copy.

    The script resolves the sibling wxyc-shared checkout from its own location
    via ``git rev-parse --git-common-dir``, so which arm runs is decided by the
    copy's surroundings -- a real repo with no ``../wxyc-shared/api.yaml``
    beside it is what forces the download branch that this suite is about.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_hermetic_env())
    script_copy = repo / "scripts" / "generate_api_models.sh"
    shutil.copy(_SCRIPT, script_copy)
    if with_sibling:
        sibling = tmp_path / "wxyc-shared"
        sibling.mkdir()
        (sibling / "api.yaml").write_text("openapi: 3.0.0\n")
    return script_copy


class _CurlCall(TypedDict):
    argv: list[str]
    stdin: str
    env: str


def _calls(tmp_path: Path) -> list[_CurlCall]:
    """Read back the stub's per-invocation records, in call order."""
    calls_dir = tmp_path / "curl_calls"
    if not calls_dir.is_dir():
        return []
    records: list[_CurlCall] = []
    for argv_file in sorted(calls_dir.glob("*.argv"), key=lambda p: int(p.stem)):
        records.append(
            _CurlCall(
                argv=argv_file.read_text().splitlines(),
                stdin=(calls_dir / f"{argv_file.stem}.stdin").read_text(),
                env=(calls_dir / f"{argv_file.stem}.env").read_text(),
            )
        )
    return records


def _run(
    tmp_path: Path,
    env_extra: dict[str, str],
    *,
    with_sibling: bool = False,
) -> subprocess.CompletedProcess:
    """Run the real script with stubbed tooling and a deterministic token env."""
    # Every stub this suite needs, installed here rather than at each call
    # site: no test wants the real thing, and installing curl unconditionally
    # is also the network guard that makes "no calls recorded" meaningful.
    _install_curl_stub(tmp_path)
    _install_env_recording_stub(tmp_path, "datamodel-codegen")
    _install_env_recording_stub(tmp_path, "ruff")
    script = _copy_script(tmp_path, with_sibling=with_sibling)
    env = _hermetic_env()
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_argv(argv: list[str], *, authenticated: bool) -> None:
    """The full curl invocation, asserted as one expression.

    The anonymous form is the pre-fix command plus the org-pinned
    --max-time/--retry options; the authenticated form is that command plus
    ``-H @-``, which reads the header from stdin so the token itself never
    rides argv. Both end in ``-o`` paired with a download target.
    """
    *head, o_flag, out_path = argv
    assert head == [*_CURL_COMMON, *(["-H", "@-"] if authenticated else []), _URL]
    assert o_flag == "-o"
    assert out_path


@pytest.mark.parametrize("token_var", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_token_attaches_authorization_header_without_leaking(tmp_path, token_var):
    """With a token set, curl receives the Bearer header on stdin, the log gets
    a stderr advisory -- and the token value appears nowhere: not argv, not
    stdout, not stderr."""

    result = _run(tmp_path, {token_var: _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(calls[0]["argv"], authenticated=True)
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert calls[0]["env"] == "", "no child process may inherit the token"
    assert not any(_TOKEN in arg for arg in calls[0]["argv"])
    # Acceptance: the authenticated path is observable in the log...
    assert "Authenticated download" in result.stderr
    # ...and the token is never echoed.
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_gh_token_takes_precedence_over_github_token(tmp_path):
    """When both variables are set, GH_TOKEN wins -- the same precedence gh
    itself documents (``gh help environment``)."""
    other = "gho_rom268_secondary_token"

    result = _run(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": other})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert other not in calls[0]["stdin"]
    assert not any(other in arg for arg in calls[0]["argv"])


def test_no_token_sends_no_authorization_header(tmp_path):
    """Unset-token local runs behave exactly as before: one anonymous download,
    no Authorization header anywhere, no authenticated-path note.

    The anonymous arm still says so on stderr. Without that advisory, dropping
    ``GITHUB_TOKEN:`` from the workflow step would silently revert CI to the
    unauthenticated path this fix exists to leave, presenting as the original
    intermittent 429 rather than as the configuration regression it is.
    """

    result = _run(tmp_path, {})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(calls[0]["argv"], authenticated=False)
    assert "Authorization" not in calls[0]["stdin"]
    assert not any("Authorization" in arg for arg in calls[0]["argv"])
    assert "Authenticated download" not in result.stdout + result.stderr
    assert "downloading anonymously" in result.stderr


def test_failed_authenticated_download_falls_back_to_anonymous(tmp_path):
    """A stale ambient token must not break previously-working runs: GitHub
    404s (not 401s) raw requests with any invalid Authorization header, so the
    script retries once anonymously and says so on stderr."""
    (tmp_path / "curl_fail_remaining").write_text("1")

    result = _run(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 2
    _assert_argv(calls[0]["argv"], authenticated=True)
    _assert_argv(calls[1]["argv"], authenticated=False)
    assert "retrying anonymously" in result.stderr
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_failure_of_both_attempts_still_fails(tmp_path):
    """The anonymous retry is a floor, not a mask: when the anonymous attempt
    fails too, the script fails -- after exactly one fallback, no retry loop --
    rather than regenerating models from a truncated spec."""
    (tmp_path / "curl_fail_remaining").write_text("2")

    result = _run(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode != 0
    assert len(_calls(tmp_path)) == 2


def test_token_is_dropped_on_the_sibling_checkout_path_too(tmp_path):
    """The scrub is a property of the whole script, not of the download arm.

    The default local invocation -- an ambient GH_TOKEN plus a sibling
    wxyc-shared checkout -- takes the arm that never downloads. If the
    capture/unset lives inside the download branch, that arm hands the token to
    datamodel-codegen, ruff, and their whole dependency tree.
    """

    result = _run(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": _TOKEN}, with_sibling=True)

    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [], "the sibling arm must not download at all"
    for tool in ("datamodel-codegen", "ruff"):
        assert (tmp_path / f"{tool}.env").read_text() == "", tool


def test_ci_job_delivers_a_token_to_the_script():
    """The script's authenticated arm is dead code unless CI exports a token.

    Scoped to the step that runs the script, and accepting either name a
    whole-file substring match is wrong in both directions: it stays green when
    the ``env:`` block migrates to a neighbouring step -- the silent revert to
    anonymous that #268 exists to prevent -- and it goes red on a rename to
    ``GH_TOKEN``, which the script accepts and which this repo already uses in
    nlp-nightly.yml.

    Scraped rather than YAML-parsed because pyyaml reaches this repo only as a
    transitive dependency of datamodel-code-generator; a parse here would rest
    on someone else's dependency tree.
    """
    lines = (
        (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml")
        .read_text()
        .splitlines()
    )
    runs = [
        i
        for i, line in enumerate(lines)
        if "generate_api_models.sh" in line and line.lstrip().startswith("run:")
    ]
    assert len(runs) == 1, f"expected exactly one step running the script, found {len(runs)}"

    indent = len(lines[runs[0]]) - len(lines[runs[0]].lstrip())
    step = []
    for line in lines[runs[0] + 1 :]:
        if line.strip().startswith("- ") and len(line) - len(line.lstrip()) < indent:
            break
        step.append(line)

    pattern = r"^\s*(GH_TOKEN|GITHUB_TOKEN):\s*\$\{\{\s*github\.token\s*\}\}\s*$"
    assert re.search(pattern, "\n".join(step), re.M), (
        "the codegen step no longer receives a token; CI is back on the "
        "shared anonymous rate budget (#268)"
    )
