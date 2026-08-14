#!/usr/bin/env python3
"""Decide whether tonight's Groq NLP validation run is needed.

Used by `.github/workflows/nlp-nightly.yml`. The workflow is scheduled but
conditional: the real-Groq parser suite only runs when the NLP surface actually
changed since the last validated run, so an untouched week costs nothing and a
parser edit is validated against the live model within a day.

Two gates, in order:

1. **DST twin.** GitHub cron is UTC-only, so 03:00 America/New_York needs two
   entries (07:00 UTC in EDT, 08:00 UTC in EST) and exactly one of them must
   act on any given night. `is_tonights_cron` keys off the UTC offset in effect
   rather than the wall-clock hour, so GitHub's habitual scheduler lag cannot
   skip a night.
2. **Path diff.** `git diff <last-validated-sha> <head>` filtered through
   `WATCHED_PATHS`. When the baseline is unknown (first run, or the SHA is gone
   after a force-push) the gate fails open and runs.

Usage:
    python scripts/nlp_nightly_gate.py \\
        --event-name schedule --event-schedule "0 7 * * *" \\
        --base <sha> --head <sha>

Writes `should_run` and `reason` to $GITHUB_OUTPUT when it is set, and prints
the decision either way.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatch
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# 03:00 ET expressed in UTC, one entry per DST regime. These strings must match
# the `schedule.cron` entries in .github/workflows/nlp-nightly.yml verbatim --
# GitHub reports the firing entry in `github.event.schedule` as written there.
CRON_EDT = "0 7 * * *"  # UTC-4: 07:00 UTC == 03:00 EDT
CRON_EST = "0 8 * * *"  # UTC-5: 08:00 UTC == 03:00 EST

_EDT_OFFSET = timedelta(hours=-4)

# The NLP surface: the prompt/model/parse path, the Groq client wiring, the
# assertion corpus, and the suite itself -- plus this gate and its workflow, so
# a change that would break the trigger is caught by the run it triggers.
# Patterns are fnmatch-style; `*` also matches `/`, so `services/*` covers a
# whole subtree. Deliberately not watched: requirements*.txt / uv.lock. A groq
# SDK bump moves them but so does every unrelated dependency change; verify an
# SDK bump with a manual `workflow_dispatch` instead of running most nights.
WATCHED_PATHS: tuple[str, ...] = (
    "services/parser.py",
    "routers/parse.py",
    "core/dependencies.py",
    "core/groq_tracing.py",
    "config/settings.py",
    "tests/scenarios.py",
    "tests/integration/test_integration.py",
    # The suite's fixtures are load-bearing for the nightly specifically: the
    # workflow leans on TEST_ENV to keep the autouse local_server fixture from
    # booting uvicorn on the runner. A change there breaks this job and nothing
    # else, so it must be able to trigger its own validation.
    "tests/conftest.py",
    "tests/integration/conftest.py",
    ".github/workflows/nlp-nightly.yml",
    "scripts/nlp_nightly_gate.py",
)

# Cap how many filenames the reason string names, so a sweeping refactor does
# not produce an unreadable job summary.
_MAX_NAMED_FILES = 5


@dataclass(frozen=True)
class Decision:
    """Whether to run the suite, and the single-line why for the job log."""

    should_run: bool
    reason: str


def expected_cron(now_et: datetime) -> str:
    """Return the cron entry that means 03:00 ET under the offset now in effect."""
    return CRON_EDT if now_et.utcoffset() == _EDT_OFFSET else CRON_EST


def is_tonights_cron(event_schedule: str, now_et: datetime) -> bool:
    """True when `event_schedule` is the entry that stands for 03:00 ET tonight."""
    return event_schedule == expected_cron(now_et)


def watched_changes(paths: Iterable[str], patterns: Sequence[str] = WATCHED_PATHS) -> list[str]:
    """Return the subset of `paths` that touches the NLP surface, in input order."""
    return [path for path in paths if any(fnmatch(path, pattern) for pattern in patterns)]


def _summarize(files: Sequence[str]) -> str:
    """Render matched files as a single line, truncated past _MAX_NAMED_FILES."""
    named = ", ".join(files[:_MAX_NAMED_FILES])
    remainder = len(files) - _MAX_NAMED_FILES
    return f"{named} (+{remainder} more)" if remainder > 0 else named


def decide(
    *,
    event_name: str,
    event_schedule: str,
    changed_files: Sequence[str] | None,
    now_et: datetime,
) -> Decision:
    """Apply the DST-twin gate and then the path-diff gate."""
    if event_name != "schedule":
        return Decision(True, f"{event_name}: running unconditionally")

    if not is_tonights_cron(event_schedule, now_et):
        return Decision(
            False,
            f"cron '{event_schedule}' is not 03:00 ET right now "
            f"(tonight's entry is '{expected_cron(now_et)}')",
        )

    if changed_files is None:
        return Decision(True, "no usable baseline from a previously validated run; running")

    matched = watched_changes(changed_files)
    if not matched:
        return Decision(
            False,
            f"no changes to the NLP surface since the last validated run "
            f"({len(changed_files)} other file(s) changed)",
        )
    return Decision(True, f"NLP surface changed: {_summarize(matched)}")


def resolve_changed_files(
    base: str,
    head: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str] | None:
    """List files changed between `base` and `head`, or None if `base` is unusable.

    Args:
        base: SHA of the last validated run, or "" when there is none.
        head: SHA under test.
        run: subprocess.run seam, swapped out in tests.

    Returns:
        Changed paths, or None when no baseline can be resolved (caller runs).
    """
    if not base:
        return None

    exists = run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        return None

    diff = run(
        ["git", "diff", "--name-only", base, head],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        return None
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _write_github_output(decision: Decision) -> None:
    """Append the decision to $GITHUB_OUTPUT, if the workflow set one."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"should_run={'true' if decision.should_run else 'false'}\n")
        handle.write(f"reason={decision.reason}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve the diff, decide, and publish the decision."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default="workflow_dispatch", help="github.event_name")
    parser.add_argument("--event-schedule", default="", help="github.event.schedule")
    parser.add_argument(
        "--base", default="", help="SHA of the last commit validated against live Groq"
    )
    parser.add_argument("--head", default="HEAD", help="SHA under test")
    args = parser.parse_args(argv)

    changed_files = (
        resolve_changed_files(args.base, args.head) if args.event_name == "schedule" else None
    )
    decision = decide(
        event_name=args.event_name,
        event_schedule=args.event_schedule,
        changed_files=changed_files,
        now_et=datetime.now(EASTERN),
    )

    verdict = "RUN" if decision.should_run else "SKIP"
    print(f"{verdict}: {decision.reason}")
    _write_github_output(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
