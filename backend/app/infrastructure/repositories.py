"""Backend-owned repository ingestion; never execute code from a repository."""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import Project


DEFAULT_GIT_HOSTS = frozenset({"github.com", "gitlab.com", "gitee.com"})
GIT_LOGGER = logging.getLogger("uvicorn.error")
GIT_NETWORK_ENVIRONMENT = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO",
})
# CURL_CA_BUNDLE is a curl-command variable, not Git's documented TLS control;
# Git uses GIT_SSL_CAINFO (or the deliberately isolated http.sslCAInfo config).
GIT_STDERR_LIMIT = 2048


@dataclass(frozen=True)
class RepositoryRevision:
    branch: str
    commit_sha: str


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

    def clone(
        self, repo_url: str, branch: str | None, destination: Path, *,
        project_id: UUID | None = None,
    ) -> RepositoryRevision:
        self.validate(repo_url, branch)
        remote_host = urlsplit(repo_url).hostname
        # The empty directory disables templates and hooks; no user/system Git
        # settings, URL rewrites, credential helpers or smudge filters are inherited.
        with TemporaryDirectory(prefix="git-config-", dir=destination.parent) as temporary:
            control = Path(temporary)
            empty = control / "empty"
            empty.mkdir()
            resolved_branch = self._resolve_remote_branch(
                repo_url, branch, control=control, project_id=project_id,
                remote_host=remote_host,
            )
            args = ["clone", "--no-checkout", "--single-branch", "--no-tags",
                    f"--template={empty}", "--branch", resolved_branch]
            args += ["--", repo_url, str(destination)]
            self._run(
                args, cwd=destination.parent, control=control,
                stage="clone_repository", project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            )
            commit_sha = self._resolve_ref(
                destination, f"refs/remotes/origin/{resolved_branch}", control=control,
                stage="resolve_remote_commit", project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            )
            self._validate_commit_tree(
                destination, commit_sha, control=control, project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            )
        return RepositoryRevision(resolved_branch, commit_sha)

    def update(
        self, repo_url: str, branch: str | None, repository: Path, *,
        project_id: UUID | None = None,
    ) -> RepositoryRevision:
        self.validate(repo_url, branch)
        remote_host = urlsplit(repo_url).hostname
        with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
            control = Path(temporary)
            (control / "empty").mkdir()
            resolved_branch = self._resolve_remote_branch(
                repo_url, branch, control=control, project_id=project_id,
                remote_host=remote_host,
            )
            shallow = self._run(
                ["rev-parse", "--is-shallow-repository"], cwd=repository, control=control,
                stage="detect_shallow", project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            ).decode("ascii").strip() == "true"
            refspec = (
                f"+refs/heads/{resolved_branch}:refs/remotes/origin/{resolved_branch}"
            )
            args = ["fetch", "--no-tags", "--force"]
            if shallow:
                args.append("--unshallow")
            args += ["--", repo_url, refspec]
            self._run(
                args, cwd=repository, control=control,
                stage="unshallow_repository" if shallow else "fetch_remote_branch",
                project_id=project_id, remote_host=remote_host,
                branch=resolved_branch,
            )
            commit_sha = self._resolve_ref(
                repository, f"refs/remotes/origin/{resolved_branch}", control=control,
                stage="resolve_remote_commit", project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            )
            self._validate_commit_tree(
                repository, commit_sha, control=control, project_id=project_id,
                remote_host=remote_host, branch=resolved_branch,
            )
        return RepositoryRevision(resolved_branch, commit_sha)

    def checkout_exact(
        self, repository: Path, commit_sha: str, *, project_id: UUID | None = None,
    ) -> None:
        self._validate_commit_sha(commit_sha)
        with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
            control = Path(temporary)
            (control / "empty").mkdir()
            self._checkout(
                repository, commit_sha, control=control, project_id=project_id,
            )

    def protect_snapshot(
        self, repository: Path, snapshot_id: UUID, commit_sha: str, *,
        project_id: UUID | None = None,
    ) -> None:
        self._validate_commit_sha(commit_sha)
        with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
            control = Path(temporary)
            (control / "empty").mkdir()
            self._run(
                ["update-ref", f"refs/pycode/snapshots/{snapshot_id}", commit_sha],
                cwd=repository, control=control, stage="protect_snapshot_ref",
                project_id=project_id,
            )

    def commit_available(
        self, repository: Path, commit_sha: str, *, project_id: UUID | None = None,
    ) -> bool:
        self._validate_commit_sha(commit_sha)
        try:
            with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
                control = Path(temporary)
                (control / "empty").mkdir()
                self._resolve_ref(
                    repository, commit_sha, control=control,
                    stage="resolve_snapshot_commit", project_id=project_id,
                )
        except BackendError as exc:
            if exc.code == "repository_commit_unavailable":
                return False
            raise
        return True

    def resolve_head(
        self, repository: Path, *, project_id: UUID | None = None,
    ) -> str:
        with TemporaryDirectory(prefix="git-config-", dir=repository.parent) as temporary:
            control = Path(temporary)
            (control / "empty").mkdir()
            return self._resolve_ref(
                repository, "HEAD", control=control, stage="verify_head",
                project_id=project_id,
            )

    def _resolve_remote_branch(
        self, repo_url: str, branch: str | None, *, control: Path,
        project_id: UUID | None, remote_host: str | None,
    ) -> str:
        if branch is not None:
            self._run(
                ["ls-remote", "--exit-code", "--heads", "--", repo_url,
                 f"refs/heads/{branch}"],
                cwd=control, control=control,
                failure_code="repository_branch_missing",
                failure_message="The requested repository branch does not exist.",
                mapped_returncodes={2},
                stage="resolve_remote_branch", project_id=project_id,
                remote_host=remote_host, branch=branch,
            )
            return branch
        output = self._run(
            ["ls-remote", "--symref", "--exit-code", "--", repo_url, "HEAD"],
            cwd=control, control=control,
            failure_code="repository_branch_missing",
            failure_message="The repository default branch is unavailable.",
            mapped_returncodes={2},
            stage="resolve_remote_branch", project_id=project_id,
            remote_host=remote_host, branch=None,
        )
        prefix = b"ref: refs/heads/"
        for line in output.splitlines():
            if line.startswith(prefix) and line.endswith(b"\tHEAD"):
                try:
                    resolved = line[len(prefix):-len(b"\tHEAD")].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BackendError(
                        "repository_branch_missing",
                        "The repository default branch is unavailable.",
                    ) from exc
                self.validate(repo_url, resolved)
                return resolved
        raise BackendError(
            "repository_branch_missing", "The repository default branch is unavailable.",
        )

    def _resolve_ref(
        self, repository: Path, ref: str, *, control: Path, stage: str,
        project_id: UUID | None = None, remote_host: str | None = None,
        branch: str | None = None,
    ) -> str:
        output = self._run(
            ["rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=repository, control=control,
            failure_code="repository_commit_unavailable",
            failure_message="The requested repository commit is unavailable.",
            stage=stage, project_id=project_id, remote_host=remote_host,
            branch=branch,
        )
        try:
            commit_sha = output.decode("ascii").strip().lower()
        except UnicodeDecodeError as exc:
            raise BackendError(
                "repository_commit_unavailable",
                "The requested repository commit is unavailable.",
            ) from exc
        self._validate_commit_sha(commit_sha)
        return commit_sha

    @staticmethod
    def _validate_commit_sha(commit_sha: str) -> None:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha) is None:
            raise BackendError(
                "repository_commit_unavailable",
                "The requested repository commit is unavailable.",
            )

    def _checkout(
        self, repository: Path, commit_sha: str, *, control: Path,
        project_id: UUID | None,
    ) -> None:
        self._validate_commit_sha(commit_sha)
        self._validate_commit_tree(
            repository, commit_sha, control=control, project_id=project_id,
        )
        self._run(
            ["checkout", "--detach", "--force", commit_sha],
            cwd=repository, control=control, stage="checkout_commit",
            project_id=project_id,
        )
        # The directory is service-owned. Artifact history lives outside repo;
        # removing untracked/ignored files makes the analyzed tree exact.
        self._run(
            ["clean", "-ffdx"], cwd=repository, control=control,
            stage="clean_workspace", project_id=project_id,
        )
        if self._resolve_ref(
            repository, "HEAD", control=control, stage="verify_head",
            project_id=project_id,
        ) != commit_sha:
            raise BackendError(
                "repository_changed", "Repository HEAD changed during preparation.",
            )

    def _validate_commit_tree(
        self, repository: Path, commit_sha: str, *, control: Path,
        project_id: UUID | None = None, remote_host: str | None = None,
        branch: str | None = None,
    ) -> None:
        tree = self._run(
            ["ls-tree", "-rz", commit_sha], cwd=repository, control=control,
            stage="validate_commit_tree", project_id=project_id,
            remote_host=remote_host, branch=branch,
        )
        self._validate_tree(tree)

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

    def _run(
        self, args: list[str], *, cwd: Path, control: Path,
        stage: str,
        failure_code: str = "repository_failed",
        failure_message: str = (
            "Repository preparation failed. Check the public URL, branch and server connectivity."
        ),
        mapped_returncodes: set[int] | None = None,
        project_id: UUID | None = None,
        remote_host: str | None = None,
        branch: str | None = None,
    ) -> bytes:
        env = {
            key: value for key, value in os.environ.items()
            if (key.upper() in {
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
            } or key in GIT_NETWORK_ENVIRONMENT)
        }
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
            self._log_failure(
                stage, args[0], project_id, remote_host, branch, None, b"",
            )
            raise BackendError("git_unavailable", "Git is not installed or available on the server.") from exc
        except subprocess.TimeoutExpired as exc:
            self._log_failure(
                stage, args[0], project_id, remote_host, branch, None,
                exc.stderr,
            )
            raise BackendError("repository_timeout", "Repository preparation timed out. Retry indexing later.") from exc
        except subprocess.CalledProcessError as exc:
            self._log_failure(
                stage, args[0], project_id, remote_host, branch,
                exc.returncode, exc.stderr,
            )
            if mapped_returncodes is not None and exc.returncode not in mapped_returncodes:
                raise BackendError(
                    "repository_failed",
                    "Repository preparation failed. Check the public URL, branch and server connectivity.",
                ) from exc
            raise BackendError(failure_code, failure_message) from exc
        return result.stdout

    def _log_failure(
        self, stage: str, operation: str, project_id: UUID | None,
        remote_host: str | None, branch: str | None, returncode: int | None,
        stderr: bytes | str | None,
    ) -> None:
        GIT_LOGGER.error(
            "Git operation failed: project_id=%s stage=%s operation=%s "
            "remote_host=%s branch=%s returncode=%s timeout=%s stderr=%s",
            project_id, stage, operation, remote_host, branch, returncode,
            self.timeout, _redact_git_stderr(stderr),
        )


def _redact_git_stderr(stderr: bytes | str | None) -> str:
    if isinstance(stderr, bytes):
        value = stderr.decode("utf-8", "replace")
    else:
        value = stderr or ""
    value = re.sub(
        r"(?i)\b(?:proxy-)?authorization\s*:\s*[^\r\n]+",
        "authorization=<redacted>", value,
    )
    value = re.sub(r"(?i)\bbearer\s+[^\s]+", "Bearer <redacted>", value)
    value = re.sub(
        r"(?i)\b(?:https?|socks5h?)://[^\s'\"]+", "<redacted-url>", value,
    )
    value = re.sub(
        r"(?i)\b(authorization|proxy-authorization|password|passwd|token|access_token)"
        r"\s*[:=]\s*[^\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "<redacted-token>", value,
    )
    value = " ".join(value.splitlines()).strip() or "<empty>"
    if len(value) > GIT_STDERR_LIMIT:
        value = value[:GIT_STDERR_LIMIT] + "...<truncated>"
    return value


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
            revision = self.source.clone(
                project.repo_url, project.branch, staging, project_id=project.id,
            )
            if revision is not None:
                self.source.checkout_exact(
                    staging, revision.commit_sha, project_id=project.id,
                )
            self._check_tree(staging)
            staging.rename(repo)
        return repo

    def synchronize(self, project: Project) -> tuple[Path, RepositoryRevision]:
        """Clone or fetch and resolve a revision; checkout remains explicit."""
        self._project_path(project)
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_entry(self.root, self.root)
        repo = self.path_for(project.id)
        repo.parent.mkdir(exist_ok=True)
        self._check_parents(project.id)
        if repo.exists() or repo.is_symlink():
            ready = self.require_ready(project)
            revision = self.source.update(
                project.repo_url, project.branch, ready, project_id=project.id,
            )
            self._check_tree(ready)
            return ready, revision
        with TemporaryDirectory(prefix="prepare-", dir=repo.parent) as temporary:
            staging = Path(temporary) / "repo"
            revision = self.source.clone(
                project.repo_url, project.branch, staging, project_id=project.id,
            )
            if revision is None:  # Backward-compatible test source boundary.
                revision = RepositoryRevision(
                    project.branch or "default",
                    self.source.resolve_head(staging, project_id=project.id),
                )
            self._check_tree(staging)
            staging.rename(repo)
        return repo, revision

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
        return self.source.resolve_head(
            self.require_ready(project), project_id=project.id,
        )

    def checkout_exact(self, project: Project, commit_sha: str) -> Path:
        repo = self.require_ready(project)
        self.source.checkout_exact(repo, commit_sha, project_id=project.id)
        self._check_tree(repo)
        return repo

    def protect_snapshot(
        self, project: Project, snapshot_id: UUID, commit_sha: str,
    ) -> None:
        self.source.protect_snapshot(
            self.require_ready(project), snapshot_id, commit_sha,
            project_id=project.id,
        )

    def commit_available(self, project: Project, commit_sha: str) -> bool:
        return self.source.commit_available(
            self.require_ready(project), commit_sha, project_id=project.id,
        )

    def assert_head(self, project: Project, commit_sha: str) -> None:
        if self.resolve_head(project) != commit_sha:
            raise BackendError(
                "repository_changed", "Repository HEAD changed during analysis.",
            )
