"""``.sql`` files from the Airflow repository.

The git implementation never writes the access token into the clone URL, the
repository configuration or logs: the token is handed to git through an inline
credential helper that reads it from the subprocess environment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

log = logging.getLogger(__name__)

_TOKEN_ENV = "DN_GIT_CREDENTIAL_TOKEN"
_USER_ENV = "DN_GIT_CREDENTIAL_USER"


@dataclass(frozen=True)
class SqlFile:
    path: str
    content: str
    content_hash: str
    commit_sha: str | None = None


@dataclass(frozen=True)
class FileListing:
    files: list[SqlFile]
    head_sha: str | None = None
    removed_paths: list[str] = field(default_factory=list)
    # True when ``since_commit`` was honoured (only changed files are listed).
    incremental: bool = False


class SqlFileSource(Protocol):
    def sync(self) -> str | None:
        """Clone or update the working copy; returns the HEAD commit when known."""

    def list_files(self, *, since_commit: str | None = None) -> FileListing: ...


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect(root: Path, include_dirs: Sequence[str], extensions: Sequence[str]) -> list[Path]:
    suffixes = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    found: list[Path] = []
    for directory in include_dirs:
        base = root / directory
        if not base.is_dir():
            continue
        found.extend(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in suffixes)
    return sorted(found)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Local directory (fixtures / tests)
# ---------------------------------------------------------------------------


class LocalDirectorySqlFileSource:
    def __init__(
        self, root: Path, *, include_dirs: Sequence[str], extensions: Sequence[str] = (".sql",)
    ) -> None:
        self._root = root
        self._include_dirs = list(include_dirs)
        self._extensions = list(extensions)

    def sync(self) -> str | None:
        if not self._root.is_dir():
            msg = f"sql_files.local_path does not exist: {self._root}"
            raise FileNotFoundError(msg)
        return None

    def list_files(self, *, since_commit: str | None = None) -> FileListing:
        files = [
            SqlFile(
                path=path.relative_to(self._root).as_posix(),
                content=_read(path),
                content_hash=content_hash(_read(path)),
            )
            for path in _collect(self._root, self._include_dirs, self._extensions)
        ]
        return FileListing(files=files, head_sha=None, incremental=False)


# ---------------------------------------------------------------------------
# Git repository
# ---------------------------------------------------------------------------


class GitSqlFileSource:
    def __init__(
        self,
        repo_url: str,
        clone_dir: Path,
        *,
        branch: str = "main",
        include_dirs: Sequence[str] = ("src", "automation"),
        extensions: Sequence[str] = (".sql",),
        token: SecretStr | None = None,
        username: str = "oauth2",
        git_executable: str = "git",
    ) -> None:
        parts = urlsplit(repo_url)
        if parts.username or parts.password:
            msg = (
                "sql_files.repo_url must not embed credentials; "
                "put the token in DN_GITLAB_TOKEN instead"
            )
            raise ValueError(msg)
        self._repo_url = repo_url
        self._clone_dir = clone_dir
        self._branch = branch
        self._include_dirs = list(include_dirs)
        self._extensions = list(extensions)
        self._token = token
        self._username = username
        self._git = git_executable

    @property
    def display_url(self) -> str:
        parts = urlsplit(self._repo_url)
        return f"{parts.scheme}://{parts.hostname}{parts.path}" if parts.scheme else self._repo_url

    # -- git plumbing --------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        if self._token is not None:
            env[_TOKEN_ENV] = self._token.get_secret_value()
            env[_USER_ENV] = self._username
        return env

    def _credential_args(self) -> list[str]:
        if self._token is None:
            return []
        # The helper prints the credentials from the environment; the token never
        # appears on the command line, in .git/config or in logs.
        helper = f'!f() {{ echo "username=${_USER_ENV}"; echo "password=${_TOKEN_ENV}"; }}; f'
        return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]

    def _run(self, *args: str, cwd: Path | None = None) -> str:
        command = [self._git, *self._credential_args(), *args]
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=self._env(),
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            msg = f"git {args[0]} failed for {self.display_url}: {_scrub(stderr, self._token)}"
            raise RuntimeError(msg) from error
        return completed.stdout

    def sync(self) -> str | None:
        if (self._clone_dir / ".git").is_dir():
            log.info("updating %s (%s)", self.display_url, self._branch)
            self._run("fetch", "--quiet", "origin", self._branch, cwd=self._clone_dir)
            self._run(
                "checkout",
                "--quiet",
                "--force",
                "-B",
                self._branch,
                "FETCH_HEAD",
                cwd=self._clone_dir,
            )
        else:
            log.info("cloning %s (%s)", self.display_url, self._branch)
            self._clone_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                "clone",
                "--quiet",
                "--branch",
                self._branch,
                "--single-branch",
                self._repo_url,
                str(self._clone_dir),
            )
        return self._run("rev-parse", "HEAD", cwd=self._clone_dir).strip()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD", cwd=self._clone_dir).strip()

    def list_files(self, *, since_commit: str | None = None) -> FileListing:
        head = self.head()
        removed: list[str] = []
        changed: set[str] | None = None
        if since_commit and since_commit != head and self._commit_exists(since_commit):
            changed = set()
            output = self._run(
                "diff",
                "--name-status",
                "--no-renames",
                since_commit,
                head,
                "--",
                *self._include_dirs,
                cwd=self._clone_dir,
            )
            for line in output.splitlines():
                if not line.strip():
                    continue
                status, _, changed_path = line.partition("\t")
                if status.startswith("D"):
                    removed.append(changed_path)
                else:
                    changed.add(changed_path)
        elif since_commit == head:
            changed = set()

        last_commit = self._last_commit_by_path()
        files: list[SqlFile] = []
        for path in _collect(self._clone_dir, self._include_dirs, self._extensions):
            relative = path.relative_to(self._clone_dir).as_posix()
            if changed is not None and relative not in changed:
                continue
            text = _read(path)
            files.append(
                SqlFile(
                    path=relative,
                    content=text,
                    content_hash=content_hash(text),
                    commit_sha=last_commit.get(relative, head),
                )
            )
        suffixes = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in self._extensions}
        removed = [p for p in removed if Path(p).suffix.lower() in suffixes]
        return FileListing(
            files=files, head_sha=head, removed_paths=removed, incremental=changed is not None
        )

    def _commit_exists(self, sha: str) -> bool:
        try:
            self._run("cat-file", "-e", f"{sha}^{{commit}}", cwd=self._clone_dir)
        except RuntimeError:
            log.warning("previous commit %s not found; processing all files", sha[:12])
            return False
        return True

    def _last_commit_by_path(self) -> dict[str, str]:
        output = self._run(
            "log",
            "--format=commit %H",
            "--name-only",
            "--",
            *self._include_dirs,
            cwd=self._clone_dir,
        )
        result: dict[str, str] = {}
        current: str | None = None
        for line in output.splitlines():
            if line.startswith("commit "):
                current = line.split(" ", 1)[1].strip()
            elif line.strip() and current is not None:
                result.setdefault(line.strip(), current)
        return result


def _scrub(text: str, token: SecretStr | None) -> str:
    if token is None:
        return text
    return text.replace(token.get_secret_value(), "***")
