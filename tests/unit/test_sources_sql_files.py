from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from data_navigator.sources.sql_files import (
    GitSqlFileSource,
    LocalDirectorySqlFileSource,
    content_hash,
)

FIXTURE_REPO = Path("tests/fixtures/repo")
EXPECTED_FILES = [
    "automation/cards/card_spend_report.sql",
    "automation/ops/broken.sql",
    "src/retail/adhoc_check.sql",
    "src/retail/customer_daily_balance.sql",
    "src/retail/customer_monthly_summary.sql",
    "src/risk/customer_risk_score.sql",
    "src/risk/daily_exposure.sql",
]


def test_local_source_lists_only_included_dirs_and_extensions() -> None:
    source = LocalDirectorySqlFileSource(FIXTURE_REPO, include_dirs=["src", "automation"])
    assert source.sync() is None
    listing = source.list_files()
    assert [f.path for f in listing.files] == EXPECTED_FILES
    assert not listing.incremental and listing.head_sha is None
    first = listing.files[0]
    assert first.content_hash == content_hash(first.content)
    assert first.commit_sha is None
    assert "docs/notes.sql" not in [f.path for f in listing.files]


def test_local_source_missing_root() -> None:
    with pytest.raises(FileNotFoundError):
        LocalDirectorySqlFileSource(Path("does/not/exist"), include_dirs=["src"]).sync()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    shutil.copytree(FIXTURE_REPO, repo)
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def test_git_source_clones_without_persisting_the_token(origin: Path, tmp_path: Path) -> None:
    token = SecretStr("glpat-SUPER-SECRET")
    clone_dir = tmp_path / "clone"
    source = GitSqlFileSource(
        str(origin), clone_dir, include_dirs=["src", "automation"], token=token
    )
    head = source.sync()
    assert head and len(head) == 40
    config = (clone_dir / ".git" / "config").read_text()
    assert "SUPER-SECRET" not in config
    assert "credential" not in config
    listing = source.list_files()
    assert [f.path for f in listing.files] == EXPECTED_FILES
    assert listing.head_sha == head
    assert all(f.commit_sha == head for f in listing.files)
    assert not listing.incremental

    # Second sync takes the fetch/checkout path and keeps HEAD identical.
    assert source.sync() == head


def test_git_source_incremental_listing(origin: Path, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    source = GitSqlFileSource(str(origin), clone_dir, include_dirs=["src", "automation"])
    old_head = source.sync()
    assert old_head is not None

    (origin / "src/retail/adhoc_check.sql").write_text("SELECT 1 FROM cos.ref.branch;\n")
    (origin / "src/risk/daily_exposure.sql").unlink()
    (origin / "docs/notes.sql").write_text("-- changed outside include dirs\n")
    (origin / "automation/ops/new_job.sql").write_text("SELECT 2 FROM cos.ref.product;\n")
    (origin / "src/retail/notes.txt").write_text("not sql\n")
    git("add", "-A", cwd=origin)
    git("commit", "-q", "-m", "change", cwd=origin)

    new_head = source.sync()
    assert new_head != old_head
    listing = source.list_files(since_commit=old_head)
    assert listing.incremental
    assert sorted(f.path for f in listing.files) == [
        "automation/ops/new_job.sql",
        "src/retail/adhoc_check.sql",
    ]
    assert listing.removed_paths == ["src/risk/daily_exposure.sql"]
    assert all(f.commit_sha == new_head for f in listing.files)

    unchanged = source.list_files(since_commit=new_head)
    assert unchanged.incremental and unchanged.files == [] and unchanged.removed_paths == []

    full = source.list_files(since_commit="0" * 40)  # unknown commit -> full listing
    assert not full.incremental and len(full.files) == len(EXPECTED_FILES)
    untouched = next(f for f in full.files if f.path == "src/retail/customer_daily_balance.sql")
    assert untouched.commit_sha == old_head


def test_git_source_rejects_credentials_in_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DN_GITLAB_TOKEN"):
        GitSqlFileSource("https://oauth2:tok@gitlab.example.com/x/y.git", tmp_path)
    source = GitSqlFileSource("https://gitlab.example.com/data/airflow.git", tmp_path / "c")
    assert source.display_url == "https://gitlab.example.com/data/airflow.git"


def test_git_source_errors_never_leak_the_token(origin: Path, tmp_path: Path) -> None:
    source = GitSqlFileSource(
        str(origin), tmp_path / "clone", branch="does-not-exist", token=SecretStr("glpat-LEAK")
    )
    with pytest.raises(RuntimeError) as info:
        source.sync()
    assert "glpat-LEAK" not in str(info.value)
    assert "git clone failed" in str(info.value)
