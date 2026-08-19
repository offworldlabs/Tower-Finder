"""deploy/classify-migration-gap.py, exercised as the CLI rollback.sh calls.

Run as a subprocess against a scratch git repository rather than imported. The
script's contract is its printed report and its exit status, and it reads the
repository from its working directory, so a subprocess is the only honest way
to point it at a tree built for a test.
"""

import subprocess
import sys
from pathlib import Path

from tests.migration_helpers import BACKEND

SCRIPT = BACKEND.parent / "deploy" / "classify-migration-gap.py"
VERSIONS = "backend/migrations/versions"


def _classify(target_ref: str, *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), target_ref],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def _repo(tmp_path: Path) -> Path:
    """A git repository with one commit and no revisions yet.

    The first commit deliberately predates migrations/versions/ entirely, which
    is the state production's rollback tag sits at (see resync_env_to_tree in
    deploy/rollback.sh) and which one of the tests below rolls back to.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("scratch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pre-alembic")
    return repo


def _add_revision(repo: Path, filename: str, safety: str | None) -> None:
    versions = repo / VERSIONS
    versions.mkdir(parents=True, exist_ok=True)
    body = f'revision = "{filename.split("_")[0]}"\n'
    if safety is not None:
        body += f'rollback_safety = "{safety}"\n'
    (versions / filename).write_text(body)


def _commit(repo: Path, message: str, tag: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    if tag is not None:
        _git(repo, "tag", tag)


def test_no_gap_when_the_restored_tree_ships_every_revision(tmp_path):
    repo = _repo(tmp_path)
    _add_revision(repo, "0001_baseline.py", "additive")
    _add_revision(repo, "0002_nodes.py", "additive")
    _commit(repo, "revisions", tag="good")

    result = _classify("good", cwd=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ships every revision this one does" in result.stdout, result.stdout
    # An empty gap means the two trees agree, which is not the same as the
    # database being where the restored tree expects it. A second rollback run
    # compares the restored tag with itself and lands here with the database
    # still ahead from the first, so the report must not read as an all-clear
    # for the database itself.
    assert "does not show up here" in result.stdout, result.stdout


def test_an_additive_gap_is_safe_to_serve(tmp_path):
    repo = _repo(tmp_path)
    _add_revision(repo, "0001_baseline.py", "additive")
    _commit(repo, "baseline", tag="good")
    _add_revision(repo, "0002_nodes.py", "additive")
    _commit(repo, "nodes")

    result = _classify("good", cwd=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "It stays at 0002." in result.stdout, result.stdout
    assert "1 revision ahead" in result.stdout, result.stdout
    assert "0002_nodes.py" in result.stdout, result.stdout
    assert "safe to serve" in result.stdout, result.stdout
    assert "MANUAL ACTION" not in result.stdout, result.stdout


def test_a_destructive_gap_names_the_downgrade_target(tmp_path):
    repo = _repo(tmp_path)
    _add_revision(repo, "0001_baseline.py", "additive")
    _commit(repo, "baseline", tag="good")
    _add_revision(repo, "0002_drop_legacy.py", "destructive")
    _add_revision(repo, "0003_nodes.py", "additive")
    _commit(repo, "drop and add")

    result = _classify("good", cwd=repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MANUAL ACTION REQUIRED" in result.stdout, result.stdout
    assert "2 revisions ahead" in result.stdout, result.stdout
    assert "alembic downgrade 0001" in result.stdout, result.stdout


def test_an_undeclared_revision_is_graded_destructive(tmp_path):
    repo = _repo(tmp_path)
    _add_revision(repo, "0001_baseline.py", "additive")
    _commit(repo, "baseline", tag="good")
    _add_revision(repo, "0002_forgot.py", None)
    _commit(repo, "forgot to declare")

    result = _classify("good", cwd=repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "undeclared" in result.stdout, result.stdout


def test_restoring_a_pre_alembic_tree_puts_the_whole_history_in_the_gap(tmp_path):
    """Production is checked out at a commit predating the versions directory,
    so its first rollback tag points at a tree with no revisions at all. The gap
    is then every revision, and no target can be named: `base` is the only one
    git could offer and it drops the baseline tables the restored code creates
    for itself, so offering it would talk an operator mid-incident into
    destroying every account, invite, owner and claim code on the droplet.
    """
    repo = _repo(tmp_path)
    _git(repo, "tag", "pre-alembic")
    _add_revision(repo, "0001_baseline.py", "additive")
    _add_revision(repo, "0002_drop_legacy.py", "destructive")
    _commit(repo, "revisions")

    result = _classify("pre-alembic", cwd=repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "2 revisions ahead" in result.stdout, result.stdout
    assert "predates the migration history" in result.stdout, result.stdout
    assert "no safe automatic target" in result.stdout, result.stdout
    assert "downgrade base" not in result.stdout, result.stdout


def test_an_unknown_ref_reports_cleanly_rather_than_raising(tmp_path):
    repo = _repo(tmp_path)
    _add_revision(repo, "0001_baseline.py", "additive")
    _commit(repo, "baseline")

    result = _classify("deploy-does-not-exist", cwd=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert "deploy-does-not-exist" in result.stderr, result.stderr
    # rollback.sh prints this stderr in place of the report, so it has to carry
    # the header too: an operator scanning the log for the Database block reads
    # its absence as the check never having run.
    assert "── Database: MANUAL ACTION REQUIRED" in result.stderr, result.stderr
