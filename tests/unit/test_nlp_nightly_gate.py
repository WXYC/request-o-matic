"""Unit tests for scripts/nlp_nightly_gate.py (the nightly Groq NLP check's gate)."""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.nlp_nightly_gate import (
    CRON_EDT,
    CRON_EST,
    EASTERN,
    KEYWORD_WATCHED,
    WATCHED_PATHS,
    decide,
    expected_cron,
    hunk_mentions,
    is_tonights_cron,
    main,
    resolve_changed_files,
    resolve_keyword_hits,
    watched_changes,
)

# A January instant (EST, UTC-5) and a July one (EDT, UTC-4). 03:00 ET lands on
# a different UTC hour in each regime, which is the whole reason two cron
# entries exist.
WINTER_3AM_ET = datetime(2026, 1, 15, 3, 0, tzinfo=EASTERN)
SUMMER_3AM_ET = datetime(2026, 7, 15, 3, 0, tzinfo=EASTERN)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nlp-nightly.yml"


class FakeRun:
    """Stand-in for subprocess.run that replays canned results per git subcommand."""

    def __init__(
        self, *, commit_exists: bool = True, diff_output: str = "", diff_returncode: int = 0
    ):
        self.commit_exists = commit_exists
        self.diff_output = diff_output
        self.diff_returncode = diff_returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if "cat-file" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if self.commit_exists else 128, "", "")
        return subprocess.CompletedProcess(cmd, self.diff_returncode, self.diff_output, "")


class TestExpectedCron:
    """03:00 ET is a different UTC hour in each DST regime."""

    @pytest.mark.parametrize(
        ("now_et", "cron"),
        [(WINTER_3AM_ET, CRON_EST), (SUMMER_3AM_ET, CRON_EDT)],
        ids=["est", "edt"],
    )
    def test_regime_selects_its_cron(self, now_et, cron):
        assert expected_cron(now_et) == cron

    def test_the_two_crons_differ(self):
        """A copy-paste slip that collapsed both entries would double-fire or never fire."""
        assert CRON_EDT != CRON_EST

    @pytest.mark.parametrize(
        ("cron", "sample_day"),
        [(CRON_EDT, (2026, 7, 15)), (CRON_EST, (2026, 1, 15))],
        ids=["edt", "est"],
    )
    def test_each_cron_actually_lands_at_3am_et(self, cron, sample_day):
        """Derived from the clock, not asserted against the same constants.

        Every other test here compares `expected_cron` output to CRON_EDT /
        CRON_EST, so transposing the two literals keeps all of them green while
        making *both* entries decoys -- the nightly would gate itself off every
        night, forever, with every job passing. Converting each cron's UTC hour
        into Eastern is the only check that pins them to reality. ("EST is the
        earlier UTC hour" is the intuitive reading, and it is backwards.)
        """
        minute, hour = cron.split()[0], cron.split()[1]
        assert minute == "0", f"cron '{cron}' does not fire on the hour"
        year, month, day = sample_day
        fires_at = datetime(year, month, day, int(hour), tzinfo=UTC)
        assert fires_at.astimezone(EASTERN).hour == 3, (
            f"cron '{cron}' fires at {fires_at.astimezone(EASTERN):%H:%M %Z} on "
            f"{sample_day}, not 03:00 ET"
        )

    @pytest.mark.parametrize(
        ("schedule", "now_et", "expected"),
        [
            (CRON_EST, WINTER_3AM_ET, True),
            (CRON_EDT, WINTER_3AM_ET, False),
            (CRON_EDT, SUMMER_3AM_ET, True),
            (CRON_EST, SUMMER_3AM_ET, False),
        ],
        ids=["est-match", "est-decoy", "edt-match", "edt-decoy"],
    )
    def test_only_the_regimes_own_cron_is_tonights(self, schedule, now_et, expected):
        assert is_tonights_cron(schedule, now_et) is expected

    def test_late_runner_still_counts(self):
        """GitHub's scheduler routinely lags. A run that fires 90 minutes late is
        still tonight's run: the gate keys off the UTC offset in effect, not off
        the wall-clock hour, so the delay cannot silently skip a whole day."""
        delayed = datetime(2026, 1, 15, 4, 30, tzinfo=EASTERN)
        assert is_tonights_cron(CRON_EST, delayed) is True

    def test_unknown_schedule_string_is_not_tonights(self):
        assert is_tonights_cron("17 4 * * *", WINTER_3AM_ET) is False


class TestWatchedChanges:
    def test_parser_is_watched(self):
        assert watched_changes(["services/parser.py"]) == ["services/parser.py"]

    def test_unrelated_file_is_ignored(self):
        assert watched_changes(["README.md", "docs/deployment.md"]) == []

    def test_returns_only_the_watched_subset_in_input_order(self):
        changed = ["README.md", "tests/scenarios.py", "services/slack.py", "services/parser.py"]
        assert watched_changes(changed) == ["tests/scenarios.py", "services/parser.py"]

    def test_glob_patterns_are_supported(self):
        assert watched_changes(["core/groq_tracing.py"], patterns=("core/groq_*.py",)) == [
            "core/groq_tracing.py"
        ]

    def test_prompt_and_corpus_are_both_watched(self):
        """The two files that most directly define NLP behaviour: the prompt +
        model in services/parser.py and the assertion corpus in tests/scenarios.py."""
        assert "services/parser.py" in WATCHED_PATHS
        assert "tests/scenarios.py" in WATCHED_PATHS

    def test_the_gate_watches_itself(self):
        """Editing the gate or the workflow must trigger a run, so a broken change
        to either is caught the same night rather than by never firing again."""
        assert "scripts/nlp_nightly_gate.py" in WATCHED_PATHS
        assert ".github/workflows/nlp-nightly.yml" in WATCHED_PATHS


class TestKeywordWatchedFiles:
    """Shared files trigger only when their own diff moves something Groq-shaped.

    core/dependencies.py also builds the LML, PostHog, and ban clients; watching
    it whole spent the shared TPM bucket on ban-roster and LML work about once a
    fortnight, which is the resource #118 says to conserve.
    """

    GROQ_DIFF = """\
diff --git a/core/dependencies.py b/core/dependencies.py
--- a/core/dependencies.py
+++ b/core/dependencies.py
@@ -40,1 +40,1 @@
-    return Groq(api_key=settings.groq_api_key)
+    return AsyncGroq(api_key=settings.groq_api_key)
"""

    BAN_ROSTER_DIFF = """\
diff --git a/core/dependencies.py b/core/dependencies.py
--- a/core/dependencies.py
+++ b/core/dependencies.py
@@ -120,0 +121,2 @@
+def get_moderator_client() -> ModeratorClient:
+    return ModeratorClient(base_url=settings.backend_url)
"""

    def test_a_groq_hunk_counts(self):
        assert hunk_mentions(self.GROQ_DIFF, "groq") is True

    def test_an_unrelated_hunk_does_not(self):
        assert hunk_mentions(self.BAN_ROSTER_DIFF, "groq") is False

    def test_file_headers_do_not_match_themselves(self):
        """`+++ b/core/groq_tracing.py` is not evidence that a Groq line moved."""
        header_only = "--- a/core/groq_tracing.py\n+++ b/core/groq_tracing.py\n"
        assert hunk_mentions(header_only, "groq") is False

    def test_context_lines_do_not_count(self):
        """An unchanged mention means the keyword was already there and stayed."""
        assert hunk_mentions("@@ -1 +1 @@\n     client = AsyncGroq()\n", "groq") is False

    def test_keyword_is_matched_case_insensitively(self):
        assert hunk_mentions("+from groq import AsyncGroq\n", "GROQ") is True

    def test_only_keyword_watched_files_are_diffed(self):
        """An ordinary changed file must not cost a git call."""
        run = FakeRun(diff_output=self.GROQ_DIFF)
        assert resolve_keyword_hits(["README.md", "services/slack.py"], "base", "head", run) == []
        assert run.calls == []

    def test_a_groq_touching_shared_file_is_a_hit(self):
        run = FakeRun(diff_output=self.GROQ_DIFF)
        hits = resolve_keyword_hits(["core/dependencies.py"], "base", "head", run)
        assert hits == ["core/dependencies.py"]

    def test_an_unrelated_shared_file_change_is_not(self):
        run = FakeRun(diff_output=self.BAN_ROSTER_DIFF)
        assert resolve_keyword_hits(["core/dependencies.py"], "base", "head", run) == []

    def test_a_failed_diff_counts_as_a_hit(self):
        """Fail open, consistent with the unknown-baseline rule: a diff we could
        not read must not be reported as "no Groq change"."""
        run = FakeRun(diff_returncode=128)
        assert resolve_keyword_hits(["config/settings.py"], "base", "head", run) == [
            "config/settings.py"
        ]

    def test_keyword_hits_make_the_gate_run(self):
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=["core/dependencies.py", "README.md"],
            now_et=WINTER_3AM_ET,
            keyword_hits=["core/dependencies.py"],
        )
        assert decision.should_run is True
        assert "core/dependencies.py" in decision.reason

    def test_shared_file_without_a_hit_does_not_run(self):
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=["core/dependencies.py", "README.md"],
            now_et=WINTER_3AM_ET,
            keyword_hits=[],
        )
        assert decision.should_run is False

    def test_the_two_watch_lists_are_disjoint(self):
        """Overlap would list a file twice in the reason and make the keyword
        scoping a no-op for it -- the unconditional match would win."""
        assert not set(WATCHED_PATHS) & set(KEYWORD_WATCHED)

    def test_the_parse_route_is_not_watched(self):
        """It looks like NLP surface and is not: TestParserIntegration calls
        parse_request() directly and never routes through /parse, so a live run
        cannot validate a change here. tests/unit/test_parse_router.py covers it.
        """
        assert "routers/parse.py" not in WATCHED_PATHS
        assert "routers/parse.py" not in KEYWORD_WATCHED


class TestStaysInSyncWithTheRepo:
    """Both halves of the gate depend on strings that live somewhere else.

    Neither kind of drift announces itself: a cron retimed in the YAML alone
    makes *both* entries decoys, so every night gates itself off and every job
    is green while the workflow is dead. A watched file renamed elsewhere leaves
    a pattern that matches nothing, so the surface it was guarding stops
    triggering runs. These tests are the only thing that would notice.
    """

    def test_cron_entries_match_the_gates_constants(self):
        # Scraped rather than YAML-parsed on purpose: pyyaml reaches this repo
        # only as a transitive dependency of datamodel-code-generator, and the
        # default unit suite should not break the day that changes.
        crons = set(re.findall(r'^\s*-\s*cron:\s*["\']([^"\']+)["\']', WORKFLOW.read_text(), re.M))
        assert crons == {CRON_EDT, CRON_EST}, (
            f"workflow crons {sorted(crons)} do not match the gate's constants "
            f"{sorted({CRON_EDT, CRON_EST})}; every night would gate itself off silently"
        )

    def test_every_watched_path_still_exists(self):
        literal = [p for p in (*WATCHED_PATHS, *KEYWORD_WATCHED) if "*" not in p]
        missing = [pattern for pattern in literal if not (REPO_ROOT / pattern).exists()]
        assert missing == [], f"watched paths that no longer exist (renamed or deleted): {missing}"


class TestDecide:
    def test_manual_dispatch_always_runs(self):
        decision = decide(
            event_name="workflow_dispatch",
            event_schedule="",
            changed_files=[],
            now_et=WINTER_3AM_ET,
        )
        assert decision.should_run is True
        assert "workflow_dispatch" in decision.reason

    def test_wrong_dst_twin_is_skipped(self):
        """Both cron entries fire every day; the one that is not 03:00 ET must
        no-op, or the suite runs twice nightly against a shared Groq TPM bucket."""
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EDT,
            changed_files=["services/parser.py"],
            now_et=WINTER_3AM_ET,
        )
        assert decision.should_run is False
        assert "03:00" in decision.reason

    def test_no_baseline_runs(self):
        """First run ever, or a baseline SHA that vanished under a force-push:
        run, rather than skip on the strength of a diff we could not compute."""
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=None,
            now_et=WINTER_3AM_ET,
        )
        assert decision.should_run is True
        assert "baseline" in decision.reason

    def test_untouched_nlp_surface_is_skipped(self):
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=["README.md", "services/slack.py"],
            now_et=WINTER_3AM_ET,
        )
        assert decision.should_run is False
        assert "no changes" in decision.reason.lower()

    def test_changed_nlp_surface_runs_and_names_the_files(self):
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=["README.md", "services/parser.py"],
            now_et=WINTER_3AM_ET,
        )
        assert decision.should_run is True
        assert "services/parser.py" in decision.reason
        assert "README.md" not in decision.reason

    def test_reason_stays_single_line(self):
        """The reason is written to $GITHUB_OUTPUT as key=value; a newline there
        would truncate the value and could inject a second output key."""
        decision = decide(
            event_name="schedule",
            event_schedule=CRON_EST,
            changed_files=["services/parser.py", "routers/parse.py", "tests/scenarios.py"],
            now_et=WINTER_3AM_ET,
        )
        assert "\n" not in decision.reason


class TestResolveChangedFiles:
    def test_empty_base_means_no_baseline(self):
        run = FakeRun()
        assert resolve_changed_files("", "head-sha", run=run) is None
        assert run.calls == [], "an empty baseline should not shell out to git at all"

    def test_missing_commit_means_no_baseline(self):
        """The recorded SHA is gone (force-push, or history shallower than the
        fetch). Treat it as unknown rather than diffing against nothing."""
        assert (
            resolve_changed_files("dead-sha", "head-sha", run=FakeRun(commit_exists=False)) is None
        )

    def test_failed_diff_means_no_baseline(self):
        """A diff that errors out (unreadable pack, bad revision) must not read as
        "nothing changed" -- that would skip the run on the strength of a failure."""
        assert resolve_changed_files("base", "head", run=FakeRun(diff_returncode=128)) is None

    def test_diff_names_are_returned(self):
        run = FakeRun(diff_output="services/parser.py\nREADME.md\n")
        assert resolve_changed_files("base", "head", run=run) == ["services/parser.py", "README.md"]

    def test_blank_lines_are_dropped(self):
        run = FakeRun(diff_output="services/parser.py\n\n")
        assert resolve_changed_files("base", "head", run=run) == ["services/parser.py"]

    def test_diff_is_two_dot_between_the_two_shas(self):
        """`git diff A B`, not `A...B`: we want everything that landed on the
        branch since the last green run, not just one side of a merge base."""
        run = FakeRun(diff_output="")
        resolve_changed_files("base-sha", "head-sha", run=run)
        diff_cmd = next(cmd for cmd in run.calls if "diff" in cmd)
        assert "base-sha" in diff_cmd
        assert "head-sha" in diff_cmd
        assert not any("..." in part for part in diff_cmd)


class TestMain:
    def test_writes_github_output(self, tmp_path, monkeypatch, capsys):
        output = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        exit_code = main(
            [
                "--event-name",
                "workflow_dispatch",
                "--base",
                "",
                "--head",
                "head-sha",
            ]
        )
        assert exit_code == 0
        written = output.read_text()
        assert "should_run=true" in written
        assert "reason=" in written
        assert "workflow_dispatch" in capsys.readouterr().out

    def test_skip_writes_false(self, tmp_path, monkeypatch):
        output = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setattr(
            "scripts.nlp_nightly_gate.resolve_changed_files", lambda *a, **k: ["README.md"]
        )
        exit_code = main(
            [
                "--event-name",
                "schedule",
                "--event-schedule",
                expected_cron(datetime.now(ZoneInfo("America/New_York"))),
                "--base",
                "base-sha",
                "--head",
                "head-sha",
            ]
        )
        assert exit_code == 0
        assert "should_run=false" in output.read_text()

    def test_runs_without_github_output_set(self, monkeypatch, capsys):
        """Locally (`python scripts/nlp_nightly_gate.py --event-name workflow_dispatch`)
        there is no $GITHUB_OUTPUT; the script should still print its decision."""
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        assert main(["--event-name", "workflow_dispatch"]) == 0
        assert "run" in capsys.readouterr().out.lower()
