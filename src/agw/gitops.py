"""Every git invocation agw needs: repo discovery, snapshots, worktrees, commits, diffs and patch apply."""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import threading
import time

from agw.models import CACHE_EXCLUDES, SNAPSHOT_BASE_LABEL, DiffStat

HEAD_KEY = "__HEAD__"
_LOCK_RETRIES = 5
# Only git's own "another process holds the lock" message is worth retrying; a file name such as yarn.lock in an
# error message is not.
_LOCK_MESSAGE = re.compile(r"Unable to create '[^']*\.lock'")
_WORKTREE_LOCK = threading.Lock()
# Repo-selecting variables inherited from a parent git process (for example when agw is started from a git
# hook) would silently redirect every command to the wrong repository. agw always selects the repo by cwd.
_SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")
# Identity for commits agw creates itself. Env vars beat user config, so the result never depends on (or fails
# for lack of) the user's git identity.
_AGW_IDENTITY = {
    "GIT_AUTHOR_NAME": "agw", "GIT_AUTHOR_EMAIL": "agw@localhost",
    "GIT_COMMITTER_NAME": "agw", "GIT_COMMITTER_EMAIL": "agw@localhost",
}
_AGW_IDENTITY_ARGS = ["-c", "user.name=agw", "-c", "user.email=agw@localhost", "-c", "commit.gpgsign=false"]
# Fixed diff rendering, so a user's diff.* settings can never change what `git apply` later has to parse.
_DIFF_OPTS = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
              "--src-prefix=a/", "--dst-prefix=b/"]


class GitError(Exception):
    """A git command failed."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(self.args_list)} failed with exit {returncode}: {stderr.strip()}")


@dataclasses.dataclass
class RepoInfo:
    main_root: str     # realpath of the working tree top level
    common_dir: str    # realpath of the common git dir (shared by all linked worktrees)
    head_sha: str      # "" when there are no commits
    branch: str        # "" when detached


def _environment(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    if extra:
        env.update(extra)
    return env


def _run(args: list[str], cwd: str, input_data: str | bytes | None, env: dict[str, str] | None,
         text: bool) -> subprocess.CompletedProcess:
    """Run git once, retrying while another process holds a git lock file."""
    kwargs: dict = {"cwd": cwd, "capture_output": True, "env": _environment(env)}
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    if input_data is not None:
        kwargs["input"] = input_data
    proc: subprocess.CompletedProcess | None = None
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            proc = subprocess.run(["git", *args], **kwargs)
        except OSError as exc:      # git or cwd missing
            empty: str | bytes = "" if text else b""
            return subprocess.CompletedProcess(["git", *args], 127, empty, str(exc) if text else str(exc).encode())
        if proc.returncode == 0:
            return proc
        stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
        if (args[:1] == ["apply"] or not _LOCK_MESSAGE.search(stderr)) or attempt == _LOCK_RETRIES:
            return proc
        time.sleep(0.2 * 2 ** attempt)
    assert proc is not None
    return proc


def _stderr_text(proc: subprocess.CompletedProcess) -> str:
    return proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")


def git(args: list[str], cwd: str, *, check: bool = True, input: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` in cwd. Retries on lock contention; raises GitError on failure when check is true."""
    proc = _run(args, cwd, input, env, text=True)
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr or "")
    return proc


def repo_info(cwd: str) -> RepoInfo:
    """Describe the repository containing cwd. Raises GitError when cwd is not inside a work tree."""
    root = git(["rev-parse", "--show-toplevel"], cwd).stdout.strip()
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd).stdout.strip()
    head = git(["rev-parse", "--verify", "-q", "HEAD"], cwd, check=False)
    head_sha = head.stdout.strip() if head.returncode == 0 else ""
    branch = git(["symbolic-ref", "-q", "--short", "HEAD"], cwd, check=False)
    return RepoInfo(
        main_root=os.path.realpath(root),
        common_dir=os.path.realpath(common),
        head_sha=head_sha,
        branch=branch.stdout.strip() if branch.returncode == 0 else "",
    )


def resolve_base(repo: RepoInfo, ref: str, snapshot_dirty: bool) -> tuple[str, str]:
    """Resolve the worktree base to (commit sha, label). Optionally snapshots uncommitted tracked changes."""
    if snapshot_dirty:
        # `git stash create` writes a commit object without touching the work tree, index or refs. It needs
        # a committer identity, which the user may not have configured.
        proc = git([*_AGW_IDENTITY_ARGS, "stash", "create"], repo.main_root, check=False, env=_AGW_IDENTITY)
        if proc.returncode != 0:
            raise GitError(["stash", "create"], proc.returncode, proc.stderr or "")
        sha = proc.stdout.strip()
        if sha:
            return sha, SNAPSHOT_BASE_LABEL
    if ref.startswith("-"):
        raise GitError(["rev-parse", "--verify", ref], 128, f"invalid ref {ref!r}")
    sha = git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo.main_root).stdout.strip()
    if ref == "HEAD":
        return sha, repo.branch or "detached"
    return sha, ref


def _status_entries(cwd: str) -> list[tuple[str, str]]:
    """(XY, path) for each entry of `git status --porcelain=v1 -z --untracked-files=all`."""
    out = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd).stdout
    fields = out.split("\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if len(field) < 4:         # trailing empty field
            continue
        xy, path = field[:2], field[3:]
        if "R" in xy or "C" in xy:
            i += 1                 # skip the original path that follows
        entries.append((xy, path))
    return entries


def _hash_files(root: str, paths: list[str]) -> dict[str, str]:
    """Content hash of each path in one `git hash-object --stdin-paths` call (fallback: one call each)."""
    hashes: dict[str, str] = {}
    batchable = [p for p in paths if "\n" not in p and "\r" not in p]
    if batchable:
        proc = git(["hash-object", "--stdin-paths"], root, check=False, input="\n".join(batchable) + "\n")
        lines = proc.stdout.splitlines()
        if proc.returncode == 0 and len(lines) == len(batchable):
            hashes.update(zip(batchable, lines))
    for path in paths:
        if path in hashes:
            continue
        proc = git(["hash-object", "--", path], root, check=False)
        hashes[path] = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "-"
    return hashes


def snapshot_tree(root: str) -> dict[str, str]:
    """Fingerprint a work tree: HEAD plus status code and content hash of every dirty path."""
    head = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False)
    snap: dict[str, str] = {HEAD_KEY: head.stdout.strip() if head.returncode == 0 else ""}
    entries = _status_entries(root)
    regular: list[str] = []
    links: dict[str, str] = {}
    for _xy, path in entries:
        full = os.path.join(root, path)
        if os.path.islink(full):
            try:
                links[path] = "link:" + os.readlink(full)
            except OSError:
                links[path] = "-"
        elif os.path.isfile(full):
            regular.append(path)
    hashes = _hash_files(root, regular) if regular else {}
    for xy, path in entries:
        snap[path] = f"{xy}:{links.get(path) or hashes.get(path, '-')}"
    return snap


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Sorted paths that differ between two snapshots (`HEAD` stands for the moved HEAD)."""
    changed = [("HEAD" if k == HEAD_KEY else k) for k in before.keys() | after.keys()
               if before.get(k) != after.get(k)]
    return sorted(changed)


def dirty_paths(snapshot: dict[str, str]) -> list[str]:
    """Sorted dirty paths of a snapshot (HEAD excluded)."""
    return sorted(k for k in snapshot if k != HEAD_KEY)


def add_worktree(repo: RepoInfo, path: str, base_sha: str, branch: str | None) -> None:
    """Create a worktree at path from base_sha: on a new branch, or detached when branch is None."""
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if branch:
        args = ["worktree", "add", "-q", "-b", branch, path, base_sha]
    else:
        args = ["worktree", "add", "-q", "--detach", path, base_sha]
    with _WORKTREE_LOCK:
        git(args, repo.main_root)


def _safe_to_delete(repo: RepoInfo, path: str) -> bool:
    """False for paths that must never be removed recursively: empty, the repo itself, or its ancestors."""
    if not path:
        return False
    real = os.path.realpath(path)
    for protected in (repo.main_root, repo.common_dir):
        if protected and (real == protected or protected.startswith(real.rstrip(os.sep) + os.sep)):
            return False
    return real != os.path.dirname(real)     # filesystem root


def _is_real_dir(path: str) -> bool:
    """True for an existing directory that is not a symlink (checked with lstat, never following links)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _under(path: str, root: str) -> bool:
    """True if realpath(path) is strictly below realpath(root)."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real != base and real.startswith(base.rstrip(os.sep) + os.sep)


def remove_worktree(repo: RepoInfo, path: str, root: str | None = None) -> None:
    """Remove a worktree directory and its git registration. Safe to call repeatedly or on a missing path.

    Only a real directory (not a symlink) is ever removed recursively, and when `root` is given only one whose
    realpath lies below it. A symlink is merely unlinked; anything else is left alone.
    """
    if path and os.path.islink(path):
        try:
            os.unlink(path)
        except OSError:
            pass
        prune_worktrees(repo)
        return
    if path and _is_real_dir(path) and _safe_to_delete(repo, path) and (root is None or _under(path, root)):
        git(["worktree", "remove", "--force", path], repo.main_root, check=False)
        shutil.rmtree(path, ignore_errors=True)
    prune_worktrees(repo)


def is_cache_path(path: str) -> bool:
    """True for build/test cache artifacts (CACHE_EXCLUDES) that are never part of a worker's deliverable."""
    parts = path.replace("\\", "/").split("/")
    for pattern in CACHE_EXCLUDES:
        if pattern.endswith("/"):
            if pattern[:-1] in parts[:-1]:
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return True
    return False


def _cache_pathspecs() -> list[str]:
    specs = []
    for pattern in CACHE_EXCLUDES:
        specs.append(f":(exclude,glob)**/{pattern}**" if pattern.endswith("/") else f":(exclude,glob)**/{pattern}")
    return specs


def checkout_paths(repo: RepoInfo) -> list[str]:
    """Realpaths of every checkout of this repository (`git worktree list --porcelain`), sorted."""
    proc = git(["worktree", "list", "--porcelain"], repo.main_root, check=False)
    found = {os.path.realpath(line[len("worktree "):]) for line in proc.stdout.splitlines()
             if line.startswith("worktree ")}
    found.add(repo.main_root)
    return sorted(found)


def worktree_changes(wt: str, ignore: set[str]) -> list[str]:
    """Sorted paths with uncommitted changes in wt (including untracked), minus ignore and cache artifacts."""
    return sorted({path for _xy, path in _status_entries(wt) if not is_cache_path(path)} - set(ignore))


def commit_all(wt: str, message: str, ignore: set[str], ref: str | None = None) -> str | None:
    """Commit every change in wt except the ignored paths and cache artifacts, moving only `ref`.

    `ref` is the full ref name (refs/heads/agw/<run>/<task>); by default the branch HEAD is on. The commit is made
    with plumbing (write-tree, commit-tree, update-ref) so HEAD, and whatever branch the worker may have switched
    to, never moves. Returns the new commit sha, or None when the tree is unchanged from the ref's tip.
    """
    if ref is None:
        sym = git(["symbolic-ref", "-q", "HEAD"], wt, check=False)
        ref = sym.stdout.strip() if sym.returncode == 0 and sym.stdout.strip() else "HEAD"
    git(["add", "-A", "--", ".", *_cache_pathspecs()], wt)
    if ignore:
        staged = git(["diff", "--cached", "--name-only", "-z"], wt).stdout.split("\0")
        to_unstage = sorted(p for p in staged if p in ignore)
        if to_unstage:
            git(["reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul"], wt,
                input="\0".join(to_unstage) + "\0", env={"GIT_LITERAL_PATHSPECS": "1"})
    tree = git(["write-tree"], wt).stdout.strip()
    tip_proc = git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], wt, check=False)
    tip = tip_proc.stdout.strip() if tip_proc.returncode == 0 else ""
    if tip:
        tip_tree = git(["rev-parse", f"{tip}^{{tree}}"], wt).stdout.strip()
        if tip_tree == tree:
            return None
    args = ["commit-tree", tree, "-m", message]
    if tip:
        args[2:2] = ["-p", tip]
    new = git(args, wt, env=_AGW_IDENTITY).stdout.strip()
    git(["update-ref", ref, new, tip or "0" * 40], wt)
    return new


def diffstat(repo_dir: str, base: str, head: str) -> DiffStat:
    """Changed files (status letter, path) and total inserted/deleted lines between two commits."""
    names = git(["diff", *_DIFF_OPTS, "-z", "--name-status", base, head], repo_dir).stdout.split("\0")
    files: list[tuple[str, str]] = []
    for i in range(0, len(names) - 1, 2):
        files.append((names[i], names[i + 1]))
    numstat = git(["diff", *_DIFF_OPTS, "-z", "--numstat", base, head], repo_dir).stdout
    insertions = deletions = 0
    for record in numstat.split("\0"):
        parts = record.split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return DiffStat(files=files, insertions=insertions, deletions=deletions)


def write_patch(repo_dir: str, base: str, head: str, out_path: str) -> None:
    """Write `git diff --binary base head` to out_path as raw bytes."""
    args = ["diff", *_DIFF_OPTS, "--binary", base, head]
    proc = _run(args, repo_dir, None, None, text=False)
    if proc.returncode != 0:
        raise GitError(args, proc.returncode, _stderr_text(proc))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(proc.stdout)


def apply_patch(root: str, patch_path: str) -> tuple[str, str]:
    """Apply a patch to the work tree at root without committing.

    Returns ("applied" | "applied-3way" | "conflict", stderr text). A 3-way application that conflicts leaves
    conflict markers in the affected files, as `git apply --3way` does.
    """
    patch = os.path.realpath(patch_path)
    check = git(["apply", "--check", "--binary", patch], root, check=False)
    if check.returncode == 0:
        done = git(["apply", "--binary", patch], root, check=False)
        if done.returncode == 0:
            return "applied", done.stderr
    three = git(["apply", "--3way", "--binary", patch], root, check=False)
    if three.returncode == 0:
        return "applied-3way", three.stderr
    return "conflict", three.stderr


def list_branches(repo: RepoInfo, prefix: str = "agw/") -> list[str]:
    """Sorted local branch names under prefix."""
    out = git(["for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"], repo.main_root).stdout
    return sorted(line for line in out.splitlines() if line)


def delete_branch(repo: RepoInfo, branch: str) -> None:
    """Force-delete a local branch; a missing branch is not an error."""
    git(["branch", "-D", "--", branch], repo.main_root, check=False)


def prune_worktrees(repo: RepoInfo) -> None:
    """Drop git's records of worktrees whose directories are gone."""
    git(["worktree", "prune"], repo.main_root, check=False)
