"""Backend-owned repository ingestion; never execute code from a repository."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import Project


DEFAULT_GIT_HOSTS = frozenset({"github.com", "gitlab.com", "gitee.com"})


class GitRepositorySource:
    """Public HTTPS Git only, with isolated configuration and bounded commands."""

    def __init__(self, *, allowed_hosts: set[str] | frozenset[str] = DEFAULT_GIT_HOSTS,
                 timeout: float = 120) -> None:
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        if timeout <= 0:
            raise ValueError("Git timeout must be positive.")
        self.timeout = timeout

    def validate(self, repo_url: str, branch: str | None) -> None:
        try:
            url = urlsplit(repo_url)
            valid = (
                url.scheme == "https" and url.hostname and url.port in (None, 443)
                and url.username is None and url.password is None
                and not url.query and not url.fragment
                and re.fullmatch(r"/[A-Za-z0-9._~/-]+", url.path)
                and url.path.strip("/")
                and all(part not in {".", ".."} for part in url.path.split("/"))
                and not any(char.isspace() or ord(char) < 32 for char in repo_url)
                and "\\" not in repo_url
            )
        except ValueError:
            valid = False
        if not valid:
            raise BackendError("invalid_repository", "Use a public HTTPS repository URL without credentials, query or fragment.")
        if url.hostname.lower() not in self.allowed_hosts:
            raise BackendError("repository_forbidden", "Repository host is not allowed by the server.")
        if branch is not None and (
            not branch or branch.startswith(("-", "/")) or branch.endswith(("/", "."))
            or branch == "@" or ".." in branch or "@{" in branch or "//" in branch
            or any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in branch)
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        ):
            raise BackendError("invalid_repository", "Repository branch is invalid.")

    def clone(self, repo_url: str, branch: str | None, destination: Path) -> None:
        self.validate(repo_url, branch)
        # The empty directory disables templates and hooks; no user/system Git
        # settings, URL rewrites, credential helpers or smudge filters are inherited.
        with TemporaryDirectory(prefix="git-config-", dir=destination.parent) as temporary:
            control = Path(temporary)
            empty = control / "empty"
            empty.mkdir()
            args = ["clone", "--no-checkout", "--depth=1", "--single-branch", "--no-tags",
                    f"--template={empty}"]
            if branch is not None:
                args += ["--branch", branch]
            args += ["--", repo_url, str(destination)]
            self._run(args, cwd=destination.parent, control=control)
            tree = self._run(["ls-tree", "-rz", "HEAD"], cwd=destination, control=control)
            self._validate_tree(tree)
            self._run(["checkout", "--force", "HEAD"], cwd=destination, control=control)

    def resolve_head(self, repository: Path) -> str:
        with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
            control = Path(temporary)
            (control / "empty").mkdir()
            output = self._run(
                ["rev-parse", "--verify", "HEAD^{commit}"],
                cwd=repository,
                control=control,
            )
        try:
            commit_sha = output.decode("ascii").strip().lower()
        except UnicodeDecodeError as exc:
            raise BackendError("repository_failed", "Repository HEAD is invalid.") from exc
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha) is None:
            raise BackendError("repository_failed", "Repository HEAD is invalid.")
        return commit_sha

    @staticmethod
    def _validate_tree(tree: bytes) -> None:
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            # Use Git metadata, not OS symlink support (Windows may materialize
            # symlinks as ordinary files). Submodules are also unsupported.
            if mode not in {b"100644", b"100755"}:
                raise BackendError("unsafe_repository", "Repository symlinks and submodules are not supported.")
            parts = name.replace(b"\\", b"/").split(b"/")
            if (any(p in {b"", b".", b".."} or b":" in p for p in parts)
                    or parts[0].lower().rstrip(b" .") == b".pclens"):
                raise BackendError("unsafe_repository", "Repository contains unsafe paths or reserved .pclens artifacts.")

    def _run(self, args: list[str], *, cwd: Path, control: Path) -> bytes:
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC"}}
        env.update({
            "HOME": str(control), "USERPROFILE": str(control),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_ALLOW_PROTOCOL": "https", "GIT_LFS_SKIP_SMUDGE": "1",
        })
        command = [
            "git", "-c", "credential.helper=", "-c", f"core.hooksPath={control / 'empty'}",
            "-c", f"core.attributesFile={os.devnull}", "-c", "core.fsmonitor=false",
            "-c", "core.protectNTFS=true", "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
            "-c", "submodule.recurse=false", *args,
        ]
        try:
            result = subprocess.run(command, cwd=cwd, env=env, shell=False,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=self.timeout, check=True)
        except FileNotFoundError as exc:
            raise BackendError("git_unavailable", "Git is not installed or available on the server.") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("repository_timeout", "Repository preparation timed out. Retry indexing later.") from exc
        except subprocess.CalledProcessError as exc:
            raise BackendError("repository_failed", "Repository preparation failed. Check the public URL, branch and server connectivity.") from exc
        return result.stdout


class RepositoryWorkspace:
    """UUID paths and preparation, owned by the application, separate from Core.

    The root must be writable only by the service/operator. This is static
    ingestion, not a sandbox against another process mutating files concurrently.
    """

    def __init__(self, root: Path, source: GitRepositorySource | None = None) -> None:
        self.root = root.resolve()
        self.source = source if source is not None else GitRepositorySource()

    def validate_source(self, repo_url: str, branch: str | None) -> None:
        self.source.validate(repo_url, branch)

    def path_for(self, project_id: UUID) -> Path:
        return self.root / str(project_id) / "repo"

    def persistent_path_for(self, project_id: UUID) -> str:
        return str(self.path_for(project_id).resolve())

    def _project_path(self, project: Project) -> Path:
        expected = self.path_for(project.id).resolve()
        if (project.workspace_path is None
                or Path(project.workspace_path).resolve() != expected
                or not expected.is_relative_to(self.root)):
            raise BackendError("workspace_unavailable", "Project workspace is unavailable. Run indexing again.")
        return expected

    @staticmethod
    def _check_entry(path: Path, root: Path) -> None:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not path.resolve().is_relative_to(root)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
            raise BackendError("unsafe_repository", "Workspace contains a link, special file or a path outside the project.")

    def _check_parents(self, project_id: UUID) -> Path:
        repo = self.path_for(project_id)
        self._check_entry(self.root, self.root)
        self._check_entry(repo.parent, self.root)
        return repo

    def _check_tree(self, repo: Path) -> None:
        self._check_entry(repo, repo)
        pending = [repo]
        while pending:
            directory = pending.pop()
            for child in directory.iterdir():
                self._check_entry(child, repo)
                if child.is_dir() and child != repo / ".git":
                    pending.append(child)

    def prepare(self, project: Project) -> Path:
        self._project_path(project)
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_entry(self.root, self.root)
        repo = self.path_for(project.id)
        repo.parent.mkdir(exist_ok=True)
        self._check_parents(project.id)
        if repo.exists() or repo.is_symlink():
            return self.require_ready(project)
        # TemporaryDirectory cleans only the newly created, owned staging path.
        # No existing repository is deleted on failure or on application restart.
        with TemporaryDirectory(prefix="prepare-", dir=repo.parent) as temporary:
            staging = Path(temporary) / "repo"
            self.source.clone(project.repo_url, project.branch, staging)
            self._check_tree(staging)
            staging.rename(repo)
        return repo

    def require_ready(self, project: Project) -> Path:
        self._project_path(project)
        repo = self._check_parents(project.id)
        if not repo.exists() and not repo.is_symlink():
            raise BackendError("workspace_unavailable", "Project workspace is unavailable. Run indexing again.")
        self._check_tree(repo)
        if not (repo / ".git").is_dir():
            raise BackendError("workspace_unavailable", "Project workspace is not a Git repository. Run indexing again.")
        return repo

    def resolve_head(self, project: Project) -> str:
        return self.source.resolve_head(self.require_ready(project))
