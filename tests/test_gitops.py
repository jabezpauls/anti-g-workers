"""Tests for agw.gitops against real temporary git repositories."""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from agw import gitops
from agw.gitops import GitError
from agw.models import SNAPSHOT_BASE_LABEL
from conftest import run_git


def _head(repo: pathlib.Path) -> str:
    return run_git(["rev-parse", "HEAD"], repo).strip()


def _status(repo: pathlib.Path) -> str:
    return run_git(["status", "--porcelain=v1", "--untracked-files=all"], repo)


# ---- repo_info ---------------------------------------------------------------------------------------

def test_repo_info_root_and_subdir_agree(git_repo):
    (git_repo / "sub").mkdir()
    a = gitops.repo_info(str(git_repo))
    b = gitops.repo_info(str(git_repo / "sub"))
    assert a == b
    assert a.main_root == str(git_repo)
    assert a.common_dir == os.path.realpath(git_repo / ".git")
    assert a.branch == "main"
    assert len(a.head_sha) == 40 and set(a.head_sha) <= set("0123456789abcdef")


def test_repo_info_outside_repo_raises(tmp_path):
    with pytest.raises(GitError) as exc:
        gitops.repo_info(str(tmp_path))
    assert exc.value.returncode != 0
    assert exc.value.args_list


def test_repo_info_missing_cwd_raises_giterror(tmp_path):
    with pytest.raises(GitError):
        gitops.repo_info(str(tmp_path / "does-not-exist"))


def test_repo_info_no_commits(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    run_git(["init", "-q", "-b", "main"], repo)
    info = gitops.repo_info(str(repo))
    assert info.head_sha == ""
    assert info.branch == "main"


def test_repo_info_resolves_symlinked_path(git_repo, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(git_repo)
    info = gitops.repo_info(str(link))
    assert info.main_root == str(git_repo)
    assert info.common_dir == os.path.realpath(git_repo / ".git")


def test_repo_info_in_linked_worktree(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "a"
    gitops.add_worktree(repo, str(wt), repo.head_sha, "agw/r/a")
    info = gitops.repo_info(str(wt))
    assert info.main_root == os.path.realpath(wt)
    assert info.common_dir == repo.common_dir
    assert info.branch == "agw/r/a"


def test_scrubbed_git_dir_env_is_ignored(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonsense"))
    assert gitops.repo_info(str(git_repo)).main_root == str(git_repo)


# ---- resolve_base ------------------------------------------------------------------------------------

def test_resolve_base_head_branch_and_sha(git_repo):
    repo = gitops.repo_info(str(git_repo))
    head = _head(git_repo)
    assert gitops.resolve_base(repo, "HEAD", False) == (head, "main")
    assert gitops.resolve_base(repo, head, False) == (head, head)
    assert gitops.resolve_base(repo, "main", False) == (head, "main")


def test_resolve_base_detached_label(git_repo):
    head = _head(git_repo)
    run_git(["checkout", "-q", "--detach"], git_repo)
    repo = gitops.repo_info(str(git_repo))
    assert repo.branch == ""
    assert gitops.resolve_base(repo, "HEAD", False) == (head, "detached")


def test_resolve_base_unknown_or_option_like_ref_raises(git_repo):
    repo = gitops.repo_info(str(git_repo))
    with pytest.raises(GitError):
        gitops.resolve_base(repo, "no-such-ref", False)
    with pytest.raises(GitError):
        gitops.resolve_base(repo, "--all", False)


def test_snapshot_dirty_creates_commit_without_touching_tree(git_repo):
    repo = gitops.repo_info(str(git_repo))
    (git_repo / "app.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
    (git_repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    status_before = _status(git_repo)
    stash_list_before = run_git(["stash", "list"], git_repo)
    sha, label = gitops.resolve_base(repo, "HEAD", True)
    assert label == SNAPSHOT_BASE_LABEL
    assert sha != _head(git_repo)
    assert "return b + a" in run_git(["show", f"{sha}:app.py"], git_repo)
    assert _status(git_repo) == status_before
    assert run_git(["stash", "list"], git_repo) == stash_list_before
    assert run_git(["rev-parse", "HEAD"], git_repo).strip() == repo.head_sha


def test_snapshot_dirty_clean_tree_falls_back_to_head(git_repo):
    repo = gitops.repo_info(str(git_repo))
    assert gitops.resolve_base(repo, "HEAD", True) == (_head(git_repo), "main")


@pytest.fixture
def no_identity(git_repo, tmp_path, monkeypatch):
    """A repo whose git has no user identity at all (no repo, global or system config, no env)."""
    run_git(["config", "--unset", "user.name"], git_repo)
    run_git(["config", "--unset", "user.email"], git_repo)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "EMAIL", "NAME"):
        monkeypatch.delenv(var, raising=False)
    # Make sure the premise holds: a plain commit really does fail here.
    (git_repo / "probe.txt").write_text("p\n", encoding="utf-8")
    plain = subprocess.run(["git", "-c", "user.useConfigOnly=true", "add", "-A"], cwd=git_repo, capture_output=True)
    assert plain.returncode == 0
    probe = subprocess.run(["git", "-c", "user.useConfigOnly=true", "commit", "-q", "-m", "x"],
                           cwd=git_repo, capture_output=True)
    assert probe.returncode != 0
    run_git(["reset", "-q"], git_repo)
    (git_repo / "probe.txt").unlink()
    return git_repo


def test_snapshot_dirty_works_without_git_identity(no_identity, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    repo = gitops.repo_info(str(no_identity))
    (no_identity / "README.md").write_text("changed\n", encoding="utf-8")
    sha, label = gitops.resolve_base(repo, "HEAD", True)
    assert label == SNAPSHOT_BASE_LABEL and sha != repo.head_sha


def test_commit_all_works_without_git_identity(no_identity, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    (no_identity / "new.txt").write_text("n\n", encoding="utf-8")
    sha = gitops.commit_all(str(no_identity), "msg", set())
    assert sha == _head(no_identity)
    assert run_git(["log", "-1", "--format=%an <%ae>|%cn <%ce>"], no_identity).strip() == \
        "agw <agw@localhost>|agw <agw@localhost>"


def test_commit_identity_ignores_user_author_env(git_repo, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "else@example.com")
    (git_repo / "x.txt").write_text("x\n", encoding="utf-8")
    gitops.commit_all(str(git_repo), "msg", set())
    assert run_git(["log", "-1", "--format=%an"], git_repo).strip() == "agw"


# ---- snapshots ---------------------------------------------------------------------------------------

def test_snapshot_clean_tree_has_only_head(git_repo):
    snap = gitops.snapshot_tree(str(git_repo))
    assert snap == {"__HEAD__": _head(git_repo)}
    assert gitops.dirty_paths(snap) == []
    assert gitops.diff_snapshots(snap, gitops.snapshot_tree(str(git_repo))) == []


def test_snapshot_detects_second_edit_of_already_modified_file(git_repo):
    (git_repo / "app.py").write_text("v1\n", encoding="utf-8")
    before = gitops.snapshot_tree(str(git_repo))
    assert gitops.dirty_paths(before) == ["app.py"]
    (git_repo / "app.py").write_text("v2\n", encoding="utf-8")
    after = gitops.snapshot_tree(str(git_repo))
    assert gitops.diff_snapshots(before, after) == ["app.py"]


def test_snapshot_detects_untracked_deleted_and_commit(git_repo):
    before = gitops.snapshot_tree(str(git_repo))
    (git_repo / "new.txt").write_text("n\n", encoding="utf-8")
    (git_repo / "README.md").unlink()
    after = gitops.snapshot_tree(str(git_repo))
    assert gitops.diff_snapshots(before, after) == ["README.md", "new.txt"]
    assert gitops.dirty_paths(after) == ["README.md", "new.txt"]
    run_git(["add", "-A"], git_repo)
    run_git(["commit", "-q", "-m", "c"], git_repo)
    committed = gitops.snapshot_tree(str(git_repo))
    assert gitops.diff_snapshots(after, committed) == ["HEAD", "README.md", "new.txt"]
    assert gitops.diff_snapshots(before, committed) == ["HEAD"]


def test_snapshot_handles_odd_paths_symlinks_and_renames(git_repo):
    (git_repo / "sp ace").mkdir()
    (git_repo / "sp ace" / "ü file.txt").write_text("1\n", encoding="utf-8")
    (git_repo / "-dash.txt").write_text("1\n", encoding="utf-8")
    (git_repo / "link").symlink_to("app.py")
    run_git(["mv", "README.md", "RENAMED.md"], git_repo)
    before = gitops.snapshot_tree(str(git_repo))
    assert {"sp ace/ü file.txt", "-dash.txt", "link", "RENAMED.md"} <= set(before)
    assert "README.md" not in before                     # the original path of a rename is skipped
    (git_repo / "sp ace" / "ü file.txt").write_text("2\n", encoding="utf-8")
    (git_repo / "link").unlink()
    (git_repo / "link").symlink_to("tests")
    after = gitops.snapshot_tree(str(git_repo))
    assert gitops.diff_snapshots(before, after) == ["link", "sp ace/ü file.txt"]


def test_snapshot_filename_with_newline(git_repo):
    (git_repo / "a\nb.txt").write_text("1\n", encoding="utf-8")
    before = gitops.snapshot_tree(str(git_repo))
    assert "a\nb.txt" in before and not before["a\nb.txt"].endswith(":-")
    (git_repo / "a\nb.txt").write_text("2\n", encoding="utf-8")
    assert gitops.diff_snapshots(before, gitops.snapshot_tree(str(git_repo))) == ["a\nb.txt"]


def test_snapshot_empty_repo_without_commits(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    run_git(["init", "-q"], repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    snap = gitops.snapshot_tree(str(repo))
    assert snap["__HEAD__"] == "" and "f.txt" in snap


# ---- worktrees ---------------------------------------------------------------------------------------

def test_add_and_remove_worktree_with_branch(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "deep" / "er" / "wt1"
    gitops.add_worktree(repo, str(wt), repo.head_sha, "agw/r/t")
    assert (wt / "app.py").is_file()
    assert gitops.list_branches(repo) == ["agw/r/t"]
    assert os.path.realpath(wt) in run_git(["worktree", "list", "--porcelain"], git_repo)
    gitops.remove_worktree(repo, str(wt))
    assert not wt.exists()
    assert os.path.realpath(wt) not in run_git(["worktree", "list", "--porcelain"], git_repo)
    gitops.remove_worktree(repo, str(wt))                 # second call must not raise
    assert gitops.list_branches(repo) == ["agw/r/t"]       # branches survive cleanup


def test_add_worktree_detached_creates_no_branch(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "d"
    gitops.add_worktree(repo, str(wt), repo.head_sha, None)
    assert gitops.list_branches(repo) == []
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=wt).returncode != 0
    gitops.remove_worktree(repo, str(wt))


def test_remove_worktree_after_crash_leaves_dir_deleted_externally(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "gone"
    gitops.add_worktree(repo, str(wt), repo.head_sha, "agw/r/gone")
    import shutil
    shutil.rmtree(wt)                                      # simulate a half-cleaned crash
    gitops.remove_worktree(repo, str(wt))
    assert os.path.realpath(wt) not in run_git(["worktree", "list", "--porcelain"], git_repo)
    # the branch can be reused after pruning the stale registration
    gitops.add_worktree(repo, str(wt), repo.head_sha, "agw/r/gone2")


def test_remove_worktree_with_dirty_and_untracked_content(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "dirty"
    gitops.add_worktree(repo, str(wt), repo.head_sha, None)
    (wt / "app.py").write_text("dirty\n", encoding="utf-8")
    (wt / "junk").mkdir()
    (wt / "junk" / "x").write_text("x", encoding="utf-8")
    gitops.remove_worktree(repo, str(wt))
    assert not wt.exists()


def test_remove_worktree_refuses_to_delete_repo_or_ancestors(git_repo):
    repo = gitops.repo_info(str(git_repo))
    gitops.remove_worktree(repo, str(git_repo))
    assert (git_repo / "app.py").is_file()
    gitops.remove_worktree(repo, str(git_repo.parent))
    assert (git_repo / "app.py").is_file()
    gitops.remove_worktree(repo, "")
    assert (git_repo / "app.py").is_file()


def test_remove_worktree_symlink_does_not_delete_target(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("k", encoding="utf-8")
    link = tmp_path / "linkwt"
    link.symlink_to(target)
    gitops.remove_worktree(repo, str(link))
    assert (target / "keep.txt").is_file()


def test_add_worktree_existing_branch_raises(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    gitops.add_worktree(repo, str(tmp_path / "w1"), repo.head_sha, "agw/r/t")
    with pytest.raises(GitError):
        gitops.add_worktree(repo, str(tmp_path / "w2"), repo.head_sha, "agw/r/t")


def test_prune_worktrees(git_repo, tmp_path):
    import shutil
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "p"
    gitops.add_worktree(repo, str(wt), repo.head_sha, None)
    shutil.rmtree(wt)
    assert os.path.realpath(wt) in run_git(["worktree", "list", "--porcelain"], git_repo)
    gitops.prune_worktrees(repo)
    assert os.path.realpath(wt) not in run_git(["worktree", "list", "--porcelain"], git_repo)


# ---- worktree changes and commits --------------------------------------------------------------------

@pytest.fixture
def edit_wt(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    wt = tmp_path / "wt" / "edit"
    gitops.add_worktree(repo, str(wt), repo.head_sha, "agw/r/edit")
    return repo, wt


def test_worktree_changes_lists_modified_added_deleted_and_respects_ignore(edit_wt):
    _repo, wt = edit_wt
    assert gitops.worktree_changes(str(wt), set()) == []
    (wt / "app.py").write_text("changed\n", encoding="utf-8")
    (wt / "new dir").mkdir()
    (wt / "new dir" / "n.txt").write_text("n\n", encoding="utf-8")
    (wt / "README.md").unlink()
    assert gitops.worktree_changes(str(wt), set()) == ["README.md", "app.py", "new dir/n.txt"]
    assert gitops.worktree_changes(str(wt), {"app.py", "nonexistent"}) == ["README.md", "new dir/n.txt"]


def test_commit_all_commits_on_branch_as_agw_and_skips_ignored(edit_wt):
    repo, wt = edit_wt
    (wt / "app.py").write_text("changed\n", encoding="utf-8")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (wt / ".env.local").write_text("s", encoding="utf-8")
    ignore = {"node_modules/dep.js", ".env.local"}
    sha = gitops.commit_all(str(wt), "agw: task", ignore)
    assert sha and len(sha) == 40
    assert run_git(["rev-parse", "agw/r/edit"], repo.main_root).strip() == sha
    assert run_git(["log", "-1", "--format=%an|%ae|%s"], wt).strip() == "agw|agw@localhost|agw: task"
    assert run_git(["diff", "--name-only", repo.head_sha, sha], wt).split() == ["app.py"]
    assert gitops.worktree_changes(str(wt), set()) == [".env.local", "node_modules/dep.js"]


def test_commit_all_ignored_modified_tracked_file_not_committed(edit_wt):
    repo, wt = edit_wt
    (wt / "README.md").write_text("setup edited me\n", encoding="utf-8")
    (wt / "app.py").write_text("real\n", encoding="utf-8")
    gitops.commit_all(str(wt), "m", {"README.md"})
    assert run_git(["diff", "--name-only", repo.head_sha, "HEAD"], wt).split() == ["app.py"]


def test_commit_all_ignore_treats_glob_characters_literally(edit_wt):
    repo, wt = edit_wt
    (wt / "a[1].txt").write_text("1", encoding="utf-8")
    (wt / "a1.txt").write_text("2", encoding="utf-8")
    gitops.commit_all(str(wt), "m", {"a[1].txt"})
    assert run_git(["diff", "--name-only", repo.head_sha, "HEAD"], wt).split() == ["a1.txt"]


def test_commit_all_no_changes_returns_none(edit_wt):
    repo, wt = edit_wt
    assert gitops.commit_all(str(wt), "m", set()) is None
    (wt / "x").write_text("x", encoding="utf-8")
    assert gitops.commit_all(str(wt), "m", {"x"}) is None
    assert run_git(["rev-parse", "HEAD"], wt).strip() == repo.head_sha


def test_commit_all_skips_hooks_and_gpgsign(edit_wt, git_repo):
    repo, wt = edit_wt
    hooks = git_repo / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    run_git(["config", "commit.gpgsign", "true"], git_repo)
    run_git(["config", "gpg.program", "/nonexistent/gpg"], git_repo)
    (wt / "x.txt").write_text("x", encoding="utf-8")
    assert gitops.commit_all(str(wt), "m", set())


# ---- diffstat / patches ------------------------------------------------------------------------------

def _make_changes(repo, wt):
    (wt / "app.py").write_text("def add(a, b):\n    return a + b + 0\n# more\n", encoding="utf-8")
    (wt / "added.txt").write_text("1\n2\n3\n", encoding="utf-8")
    (wt / "README.md").unlink()
    return gitops.commit_all(str(wt), "task", set())


def test_diffstat_and_patch_apply_cleanly_to_fresh_base(edit_wt, tmp_path):
    repo, wt = edit_wt
    head = _make_changes(repo, wt)
    stat = gitops.diffstat(str(wt), repo.head_sha, head)
    assert sorted(stat.files) == [("A", "added.txt"), ("D", "README.md"), ("M", "app.py")]
    assert stat.insertions == 3 + 2
    assert stat.deletions == 1 + 1
    patch = tmp_path / "out" / "diff.patch"
    gitops.write_patch(str(wt), repo.head_sha, head, str(patch))
    fresh = tmp_path / "fresh"
    run_git(["clone", "-q", repo.main_root, str(fresh)], tmp_path)
    run_git(["apply", "--check", str(patch)], fresh)


def test_patch_ignores_user_diff_config(edit_wt, git_repo, tmp_path):
    repo, wt = edit_wt
    run_git(["config", "diff.noprefix", "true"], git_repo)
    run_git(["config", "diff.mnemonicPrefix", "true"], git_repo)
    run_git(["config", "diff.external", "/nonexistent/tool"], git_repo)
    head = _make_changes(repo, wt)
    patch = tmp_path / "d.patch"
    gitops.write_patch(str(wt), repo.head_sha, head, str(patch))
    assert patch.read_text(encoding="utf-8").startswith("diff --git a/")
    assert gitops.diffstat(str(wt), repo.head_sha, head).files


def test_binary_file_survives_patch_and_counts_zero_lines(edit_wt, tmp_path):
    repo, wt = edit_wt
    blob = bytes(range(256)) * 4
    (wt / "blob.bin").write_bytes(blob)
    head = gitops.commit_all(str(wt), "bin", set())
    stat = gitops.diffstat(str(wt), repo.head_sha, head)
    assert stat.files == [("A", "blob.bin")] and stat.insertions == 0 and stat.deletions == 0
    patch = tmp_path / "bin.patch"
    gitops.write_patch(str(wt), repo.head_sha, head, str(patch))
    status, _err = gitops.apply_patch(repo.main_root, str(patch))
    assert status == "applied"
    assert (pathlib.Path(repo.main_root) / "blob.bin").read_bytes() == blob


def test_diffstat_handles_unusual_names(edit_wt):
    repo, wt = edit_wt
    (wt / "sp ace.txt").write_text("a\n", encoding="utf-8")
    (wt / "tab\there.txt").write_text("a\n", encoding="utf-8")
    head = gitops.commit_all(str(wt), "t", set())
    stat = gitops.diffstat(str(wt), repo.head_sha, head)
    assert sorted(p for _s, p in stat.files) == ["sp ace.txt", "tab\there.txt"]
    assert stat.insertions == 2


def test_diffstat_empty_between_identical_commits(git_repo):
    repo = gitops.repo_info(str(git_repo))
    stat = gitops.diffstat(repo.main_root, repo.head_sha, repo.head_sha)
    assert stat.files == [] and stat.insertions == 0 and stat.deletions == 0


def test_write_patch_failure_raises(git_repo, tmp_path):
    repo = gitops.repo_info(str(git_repo))
    with pytest.raises(GitError):
        gitops.write_patch(repo.main_root, "deadbeef" * 5, repo.head_sha, str(tmp_path / "p"))


# ---- apply_patch -------------------------------------------------------------------------------------

def _patch_for(repo, wt, tmp_path, content: str) -> str:
    (wt / "app.py").write_text(content, encoding="utf-8")
    head = gitops.commit_all(str(wt), "t", set())
    patch = tmp_path / "t.patch"
    gitops.write_patch(str(wt), repo.head_sha, head, str(patch))
    return str(patch)


def test_apply_clean(edit_wt, tmp_path):
    repo, wt = edit_wt
    patch = _patch_for(repo, wt, tmp_path, "def add(a, b):\n    return a + b  # edited\n")
    root = pathlib.Path(repo.main_root)
    status, _ = gitops.apply_patch(repo.main_root, patch)
    assert status == "applied"
    assert "# edited" in (root / "app.py").read_text(encoding="utf-8")
    assert _status(root) == " M app.py\n"                  # unstaged, nothing committed
    assert _head(root) == repo.head_sha


def test_apply_conflict_on_same_line(edit_wt, tmp_path):
    repo, wt = edit_wt
    patch = _patch_for(repo, wt, tmp_path, "def add(a, b):\n    return a + b  # theirs\n")
    root = pathlib.Path(repo.main_root)
    (root / "app.py").write_text("def add(a, b):\n    return a + b  # mine\n", encoding="utf-8")
    status, err = gitops.apply_patch(repo.main_root, patch)
    assert status == "conflict"
    assert err.strip()
    assert _head(root) == repo.head_sha


def test_apply_non_overlapping_edit_in_same_file(edit_wt, tmp_path):
    repo, wt = edit_wt
    lines = [f"line{i}\n" for i in range(30)]
    root = pathlib.Path(repo.main_root)
    (wt / "app.py").write_text("".join(lines), encoding="utf-8")
    base2 = gitops.commit_all(str(wt), "seed", set())
    (root / "app.py").write_text("".join(lines), encoding="utf-8")
    run_git(["commit", "-q", "-am", "seed in main"], root)
    new_base = _head(root)
    # worker edits the top; the user edits the bottom of the working tree afterwards
    (wt / "app.py").write_text("".join(["TOP\n"] + lines[1:]), encoding="utf-8")
    head = gitops.commit_all(str(wt), "top", set())
    patch = tmp_path / "top.patch"
    gitops.write_patch(str(wt), base2, head, str(patch))
    (root / "app.py").write_text("".join(lines[:-1] + ["BOTTOM\n"]), encoding="utf-8")
    status, _ = gitops.apply_patch(str(root), str(patch))
    assert status in ("applied", "applied-3way")
    text = (root / "app.py").read_text(encoding="utf-8")
    assert text.startswith("TOP\n") and text.endswith("BOTTOM\n")
    assert _head(root) == new_base


def test_apply_empty_patch_is_conflict_not_crash(git_repo, tmp_path):
    empty = tmp_path / "empty.patch"
    empty.write_text("", encoding="utf-8")
    status, _err = gitops.apply_patch(str(git_repo), str(empty))
    assert status == "conflict"


# ---- branches ----------------------------------------------------------------------------------------

def test_list_and_delete_branches(git_repo):
    repo = gitops.repo_info(str(git_repo))
    assert gitops.list_branches(repo) == []
    for name in ("agw/r2/b", "agw/r1/a", "feature/x"):
        run_git(["branch", name], git_repo)
    assert gitops.list_branches(repo) == ["agw/r1/a", "agw/r2/b"]
    assert gitops.list_branches(repo, "agw/r1/") == ["agw/r1/a"]
    gitops.delete_branch(repo, "agw/r1/a")
    gitops.delete_branch(repo, "agw/r1/a")                 # already gone: no error
    gitops.delete_branch(repo, "does/not/exist")
    assert gitops.list_branches(repo) == ["agw/r2/b"]
    assert "feature/x" in run_git(["branch", "--list"], git_repo)


# ---- git() -------------------------------------------------------------------------------------------

def test_git_check_raises_with_details(git_repo):
    with pytest.raises(GitError) as exc:
        gitops.git(["rev-parse", "--verify", "nope^{commit}"], str(git_repo))
    assert exc.value.returncode != 0
    assert exc.value.args_list == ["rev-parse", "--verify", "nope^{commit}"]
    assert "nope" in str(exc.value)
    proc = gitops.git(["rev-parse", "--verify", "nope^{commit}"], str(git_repo), check=False)
    assert proc.returncode != 0


def test_git_input_and_env(git_repo):
    out = gitops.git(["hash-object", "--stdin"], str(git_repo), input="hello\n").stdout.strip()
    assert out == "ce013625030ba8dba906f756967f9e9ca394464a"
    cfg = gitops.git(["config", "--get", "agw.test"], str(git_repo),
                     env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "agw.test",
                          "GIT_CONFIG_VALUE_0": "yes"}).stdout.strip()
    assert cfg == "yes"


def test_git_lock_retry(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) < 3:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: Unable to create '.git/index.lock': File exists.")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(gitops.time, "sleep", sleeps.append)
    proc = gitops.git(["status"], str(tmp_path))
    assert proc.returncode == 0 and proc.stdout == "ok\n"
    assert len(calls) == 3
    assert sleeps == [0.2, 0.4]


def test_git_lock_retry_gives_up_after_five_retries(monkeypatch, tmp_path):
    calls: list[int] = []

    def fake_run(cmd, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 128, "", "index.lock exists")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(gitops.time, "sleep", lambda s: None)
    with pytest.raises(GitError):
        gitops.git(["status"], str(tmp_path))
    assert len(calls) == 6


def test_git_does_not_retry_other_failures(monkeypatch, tmp_path):
    calls: list[int] = []

    def fake_run(cmd, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GitError):
        gitops.git(["status"], str(tmp_path))
    assert len(calls) == 1


def test_git_sets_noninteractive_environment(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
    gitops.git(["status"], str(tmp_path), env={"X_EXTRA": "1"})
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and seen["LC_ALL"] == "C" and seen["X_EXTRA"] == "1"
    assert "GIT_WORK_TREE" not in seen


def test_concurrent_add_worktree(git_repo, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repo = gitops.repo_info(str(git_repo))

    def make(i: int) -> None:
        gitops.add_worktree(repo, str(tmp_path / "wt" / f"t{i}"), repo.head_sha, f"agw/r/t{i}")

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(make, range(6)))
    assert len(gitops.list_branches(repo)) == 6
    for i in range(6):
        gitops.remove_worktree(repo, str(tmp_path / "wt" / f"t{i}"))
    assert run_git(["worktree", "list", "--porcelain"], git_repo).count("worktree ") == 1
