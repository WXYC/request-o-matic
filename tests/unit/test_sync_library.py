"""Tests for the sync-library.sh script."""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary git repository with the sync script structure."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )

    # Create initial commit so we have a branch
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )

    # Create library.db file and commit it
    library_db = repo_dir / "library.db"
    library_db.write_text("initial content")
    subprocess.run(["git", "add", "library.db"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add library.db"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )

    # Create venv directory structure
    venv_bin = repo_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)

    # Create scripts directory
    scripts_dir = repo_dir / "scripts"
    scripts_dir.mkdir()

    return repo_dir


@pytest.fixture
def log_file(tmp_path):
    """Create a temporary log file path."""
    return tmp_path / "etl.log"


def create_mock_etl(repo_dir: Path, should_succeed: bool = True, modify_db: bool = False):
    """Create a mock ETL script that optionally modifies library.db."""
    etl_script = repo_dir / "scripts" / "export_to_sqlite.py"

    if should_succeed:
        if modify_db:
            # Script that modifies library.db
            etl_script.write_text(f"""#!/usr/bin/env python3
import sys
from pathlib import Path

db_path = Path("{repo_dir}/library.db")
current = db_path.read_text()
db_path.write_text(current + "\\nmodified")
print("ETL completed successfully")
sys.exit(0)
""")
        else:
            # Script that succeeds but doesn't modify anything
            etl_script.write_text("""#!/usr/bin/env python3
import sys
print("ETL completed successfully - no changes")
sys.exit(0)
""")
    else:
        # Script that fails
        etl_script.write_text("""#!/usr/bin/env python3
import sys
print("ETL failed: database connection error", file=sys.stderr)
sys.exit(1)
""")

    etl_script.chmod(etl_script.stat().st_mode | stat.S_IEXEC)


def create_mock_python(repo_dir: Path):
    """Create a mock python executable in venv/bin that runs the real Python."""
    python_path = repo_dir / "venv" / "bin" / "python"
    # Create a shell script that calls the real Python
    python_path.write_text(f"""#!/bin/bash
exec python3 "$@"
""")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC)


def create_sync_script(repo_dir: Path, log_file: Path, slack_marker_file: Path | None = None):
    """Create the sync-library.sh script configured for the test environment.

    Args:
        slack_marker_file: If provided, writes Slack notification payload here instead of curl.
    """
    sync_script = repo_dir / "scripts" / "sync-library.sh"

    # Use marker file to capture what would be sent to Slack (avoids real HTTP calls)
    if slack_marker_file:
        slack_notify = f'echo "$message" >> "{slack_marker_file}"'
    else:
        slack_notify = "true  # No Slack configured"

    # Create a modified version that doesn't push (no remote in test)
    sync_script.write_text(f"""#!/bin/bash
set -e

LOG_FILE="{log_file}"
REPO_DIR="{repo_dir}"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}}

notify_error() {{
    local message="$1"
    log "ERROR: $message"
    {slack_notify}
}}

cd "$REPO_DIR"

log "Starting library sync"

# Run ETL, capturing output for error reporting
ETL_OUTPUT=$(mktemp)
if ! venv/bin/python scripts/export_to_sqlite.py >> "$ETL_OUTPUT" 2>&1; then
    # Extract the final error line (last non-empty line not starting with whitespace)
    ERROR_DETAILS=$(grep -v '^[[:space:]]' "$ETL_OUTPUT" | grep -v '^$' | tail -1 | sed 's/"/\\\"/g')
    cat "$ETL_OUTPUT" >> "$LOG_FILE"
    rm -f "$ETL_OUTPUT"
    notify_error "ETL script failed: $ERROR_DETAILS"
    exit 1
fi
cat "$ETL_OUTPUT" >> "$LOG_FILE"
rm -f "$ETL_OUTPUT"

# Check if database changed
if git diff --quiet library.db; then
    log "No changes to library.db"
    exit 0
fi

# Commit (skip push for tests - no remote)
log "Changes detected, committing..."
git add library.db
git commit -m "Update library catalog $(date +%Y-%m-%d)"

log "Committed successfully"
""")
    sync_script.chmod(sync_script.stat().st_mode | stat.S_IEXEC)
    return sync_script


class TestSyncLibraryScript:
    """Tests for sync-library.sh behavior."""

    def test_etl_failure_exits_with_error(self, temp_repo, log_file):
        """When ETL fails, the script should exit with error and not commit."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=False)
        sync_script = create_sync_script(temp_repo, log_file)

        # Get initial commit count
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        initial_commits = int(result.stdout.strip())

        # Run sync script - should fail
        result = subprocess.run(
            [str(sync_script)],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1, "Script should exit with error code 1"

        # Verify no new commits
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        final_commits = int(result.stdout.strip())
        assert final_commits == initial_commits, "No commits should be made when ETL fails"

        # Verify error logged
        log_content = log_file.read_text()
        assert "ERROR: ETL script failed" in log_content

    def test_no_changes_skips_commit(self, temp_repo, log_file):
        """When library.db is unchanged, no commit should be made."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=False)
        sync_script = create_sync_script(temp_repo, log_file)

        # Get initial commit count
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        initial_commits = int(result.stdout.strip())

        # Run sync script
        result = subprocess.run(
            [str(sync_script)],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Script should succeed. stderr: {result.stderr}"

        # Verify no new commits
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        final_commits = int(result.stdout.strip())
        assert final_commits == initial_commits, "No commits should be made when no changes"

        # Verify logged correctly
        log_content = log_file.read_text()
        assert "No changes to library.db" in log_content

    def test_changes_detected_creates_commit(self, temp_repo, log_file):
        """When library.db changes, a commit should be created."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=True)
        sync_script = create_sync_script(temp_repo, log_file)

        # Get initial commit count
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        initial_commits = int(result.stdout.strip())

        # Run sync script
        result = subprocess.run(
            [str(sync_script)],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Script should succeed. stderr: {result.stderr}"

        # Verify new commit was made
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        final_commits = int(result.stdout.strip())
        assert final_commits == initial_commits + 1, "One new commit should be made"

        # Verify commit message format
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        commit_message = result.stdout.strip()
        assert commit_message.startswith(
            "Update library catalog"
        ), f"Commit message should start with 'Update library catalog', got: {commit_message}"

        # Verify logged correctly
        log_content = log_file.read_text()
        assert "Changes detected, committing..." in log_content
        assert "Committed successfully" in log_content

    def test_logging_includes_timestamps(self, temp_repo, log_file):
        """Log entries should include timestamps."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=False)
        sync_script = create_sync_script(temp_repo, log_file)

        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        log_content = log_file.read_text()
        # Check for timestamp format: YYYY-MM-DD HH:MM:SS
        import re

        timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
        assert re.search(
            timestamp_pattern, log_content
        ), f"Log should contain timestamps. Log content: {log_content}"

    def test_script_logs_start_message(self, temp_repo, log_file):
        """Script should log when it starts."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=False)
        sync_script = create_sync_script(temp_repo, log_file)

        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        log_content = log_file.read_text()
        assert "Starting library sync" in log_content

    def test_only_library_db_is_committed(self, temp_repo, log_file):
        """Only library.db should be committed, not other changed files."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=True)
        sync_script = create_sync_script(temp_repo, log_file)

        # Create another modified file
        other_file = temp_repo / "other.txt"
        other_file.write_text("some content")

        # Run sync script
        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        # Verify other.txt is still untracked
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        assert "other.txt" in result.stdout, "other.txt should still be untracked"

        # Verify only library.db was in the last commit
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        committed_files = result.stdout.strip().split("\n")
        assert committed_files == [
            "library.db"
        ], f"Only library.db should be committed, got: {committed_files}"

    def test_error_includes_details_in_notification(self, temp_repo, log_file, tmp_path):
        """Error notifications should include the actual error message."""
        create_mock_python(temp_repo)

        # Create ETL that fails with a specific error message
        etl_script = temp_repo / "scripts" / "export_to_sqlite.py"
        etl_script.write_text("""#!/usr/bin/env python3
import sys
print("Connecting to database...", file=sys.stderr)
print("ERROR: Connection refused - MySQL is not running", file=sys.stderr)
sys.exit(1)
""")
        etl_script.chmod(etl_script.stat().st_mode | stat.S_IEXEC)

        slack_marker = tmp_path / "slack_notification.txt"
        sync_script = create_sync_script(temp_repo, log_file, slack_marker_file=slack_marker)

        # Run sync script - should fail
        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        # Verify notification includes error details
        notification = slack_marker.read_text()
        assert (
            "MySQL is not running" in notification
        ), f"Notification should include error details, got: {notification}"

    def test_error_extracts_final_exception_line(self, temp_repo, log_file, tmp_path):
        """Error notification should extract the final exception line, not full traceback."""
        create_mock_python(temp_repo)

        # Create ETL that fails with a Python traceback (like the real MySQL error)
        etl_script = temp_repo / "scripts" / "export_to_sqlite.py"
        etl_script.write_text('''#!/usr/bin/env python3
import sys
# Simulate a Python traceback
print("""Traceback (most recent call last):
  File "script.py", line 10, in <module>
    connect()
  File "script.py", line 5, in connect
    raise ConnectionError("Failed")
ConnectionRefusedError: [Errno 61] Connection refused""", file=sys.stderr)
sys.exit(1)
''')
        etl_script.chmod(etl_script.stat().st_mode | stat.S_IEXEC)

        slack_marker = tmp_path / "slack_notification.txt"
        sync_script = create_sync_script(temp_repo, log_file, slack_marker_file=slack_marker)

        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        notification = slack_marker.read_text()
        # Should contain the final error line
        assert (
            "ConnectionRefusedError" in notification
        ), f"Should include exception type, got: {notification}"
        # Should NOT contain traceback noise
        assert (
            "Traceback" not in notification
        ), f"Should not include traceback header, got: {notification}"
        assert (
            'File "script.py"' not in notification
        ), f"Should not include file references, got: {notification}"

    def test_slack_notification_sent_on_etl_failure(self, temp_repo, log_file, tmp_path):
        """Slack notification should be sent when ETL fails."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=False)

        slack_marker = tmp_path / "slack_notification.txt"
        sync_script = create_sync_script(temp_repo, log_file, slack_marker_file=slack_marker)

        # Run sync script - should fail
        subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)

        # Verify notification was sent
        assert slack_marker.exists(), "Slack notification should be sent on failure"
        notification = slack_marker.read_text()
        assert "ETL script failed" in notification

    def test_no_slack_notification_on_success(self, temp_repo, log_file, tmp_path):
        """No Slack notification should be sent when sync succeeds."""
        create_mock_python(temp_repo)
        create_mock_etl(temp_repo, should_succeed=True, modify_db=True)

        slack_marker = tmp_path / "slack_notification.txt"
        sync_script = create_sync_script(temp_repo, log_file, slack_marker_file=slack_marker)

        # Run sync script - should succeed
        result = subprocess.run([str(sync_script)], cwd=temp_repo, capture_output=True)
        assert result.returncode == 0

        # Verify no notification was sent
        assert not slack_marker.exists(), "No Slack notification should be sent on success"
