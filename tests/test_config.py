"""Tests for agw.config: layered loading, validation and rendering."""
from __future__ import annotations

import os
import pathlib

import pytest

from agw import config as cfgmod
from agw.config import CONFIG_KEYS, ConfigError, default_worktree_dir, load_config, render_config, user_config_path
from agw.models import Config
from conftest import toml_dumps


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point AGW_CONFIG at a (non-existent) temp file so no real user config can be read."""
    user_file = tmp_path / "user" / "config.toml"
    user_file.parent.mkdir()
    monkeypatch.setenv("AGW_CONFIG", str(user_file))
    monkeypatch.delenv("AGW_AGY", raising=False)
    return user_file


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_defaults_without_any_file(isolated):
    cfg = load_config(None)
    d = Config()
    assert cfg.model == d.model
    assert cfg.max_parallel == d.max_parallel
    assert cfg.timeout_seconds == d.timeout_seconds
    assert cfg.sandbox is False
    assert cfg.retry_limit == d.retry_limit
    assert cfg.accept == []
    assert cfg.accept_timeout_seconds == d.accept_timeout_seconds
    assert cfg.setup == []
    assert cfg.agy_path == ["agy"]
    assert cfg.worktree_dir == default_worktree_dir()
    assert cfg.deny_write == [os.path.realpath(os.path.expanduser(p)) for p in d.deny_write]
    assert all(os.path.isabs(p) for p in cfg.deny_write)
    assert set(cfg.sources) == set(CONFIG_KEYS)
    assert set(cfg.sources.values()) == {"default"}


def test_user_then_project_precedence_and_sources(isolated, tmp_path):
    isolated.write_text(toml_dumps({"model": "user-model", "max_parallel": 5}), encoding="utf-8")
    root = _project(tmp_path)
    project_file = root / ".agw.toml"
    project_file.write_text(toml_dumps({"model": "project-model"}), encoding="utf-8")
    cfg = load_config(str(root))
    assert cfg.model == "project-model"
    assert cfg.max_parallel == 5
    assert cfg.sources["model"] == str(project_file)
    assert cfg.sources["max_parallel"] == str(isolated)
    assert cfg.sources["retry_limit"] == "default"


def test_project_file_ignored_without_main_root(isolated, tmp_path):
    root = _project(tmp_path)
    (root / ".agw.toml").write_text(toml_dumps({"model": "project-model"}), encoding="utf-8")
    assert load_config(None).model == Config().model


def test_lists_are_replaced_not_merged(isolated, tmp_path):
    isolated.write_text(toml_dumps({"accept": ["a"]}), encoding="utf-8")
    root = _project(tmp_path)
    (root / ".agw.toml").write_text(toml_dumps({"accept": ["b"]}), encoding="utf-8")
    assert load_config(str(root)).accept == ["b"]


def test_overrides_win_and_none_is_ignored(isolated, tmp_path):
    isolated.write_text(toml_dumps({"model": "user-model", "max_parallel": 4}), encoding="utf-8")
    cfg = load_config(None, {"model": "m2", "max_parallel": None})
    assert cfg.model == "m2"
    assert cfg.sources["model"] == "cli"
    assert cfg.max_parallel == 4
    assert cfg.sources["max_parallel"] == str(isolated)


def test_overrides_are_validated(isolated):
    with pytest.raises(ConfigError, match=r"cli: max_parallel: must be between 1 and 8"):
        load_config(None, {"max_parallel": 99})


def test_agw_agy_env_and_string_agy_path(isolated, monkeypatch):
    monkeypatch.setenv("AGW_AGY", "/x/agy")
    cfg = load_config(None)
    assert cfg.agy_path == ["/x/agy"]
    assert cfg.sources["agy_path"] == "env"
    monkeypatch.delenv("AGW_AGY")
    isolated.write_text('agy_path = "agy"\n', encoding="utf-8")
    cfg = load_config(None)
    assert cfg.agy_path == ["agy"]
    assert cfg.sources["agy_path"] == str(isolated)


def test_agw_agy_env_beats_files(isolated, monkeypatch):
    isolated.write_text('agy_path = ["python3", "/f.py"]\n', encoding="utf-8")
    monkeypatch.setenv("AGW_AGY", "/x/agy")
    assert load_config(None).agy_path == ["/x/agy"]


def test_agy_path_list_kept(isolated):
    isolated.write_text('agy_path = ["python3", "/x/fake_agy.py"]\n', encoding="utf-8")
    assert load_config(None).agy_path == ["python3", "/x/fake_agy.py"]


def test_worktree_dir_and_deny_write_are_normalized(isolated, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    isolated.write_text(toml_dumps({"worktree_dir": str(link), "deny_write": [str(link / "x"), "~/.foo"]}),
                        encoding="utf-8")
    cfg = load_config(None)
    assert cfg.worktree_dir == str(link)          # a symlink stays visible so run/doctor can reject it
    assert cfg.deny_write == [str(real / "x"), os.path.realpath(os.path.expanduser("~/.foo"))]


def test_relative_paths_resolve_against_the_config_file_directory(isolated, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".agw.toml").write_text(toml_dumps({"worktree_dir": "wts", "deny_write": ["secret", "../up"]}),
                                       encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(str(project))
    assert cfg.worktree_dir == str(project / "wts")
    assert cfg.deny_write == [str(project / "secret"), str(tmp_path / "up")]
    cli = load_config(str(project), {"worktree_dir": "from-cli"})
    assert cli.worktree_dir == str(elsewhere / "from-cli")


def test_worktree_dir_problem_checks(tmp_path):
    from agw.config import worktree_dir_problem
    fresh = tmp_path / "a" / "wts"
    assert worktree_dir_problem(str(fresh), create=True) == ""
    assert (os.stat(fresh).st_mode & 0o777) == 0o700
    assert worktree_dir_problem(str(tmp_path / "missing")) == ""
    link = tmp_path / "ln"
    link.symlink_to(fresh)
    assert "symlink" in worktree_dir_problem(str(link))
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    assert "chmod 700" in worktree_dir_problem(str(loose))
    f = tmp_path / "file"
    f.write_text("x", encoding="utf-8")
    assert "not a directory" in worktree_dir_problem(str(f))


def test_empty_worktree_dir_means_default(isolated):
    isolated.write_text('worktree_dir = ""\n', encoding="utf-8")
    cfg = load_config(None)
    assert cfg.worktree_dir == default_worktree_dir()
    assert cfg.sources["worktree_dir"] == str(isolated)


def test_default_worktree_dir_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(cfgmod.getpass, "getuser", lambda: "alice")
    assert default_worktree_dir() == os.path.realpath(str(tmp_path / "agw-alice"))

    def boom() -> str:
        raise KeyError("no user")

    monkeypatch.setattr(cfgmod.getpass, "getuser", boom)
    assert default_worktree_dir() == os.path.realpath(str(tmp_path / "agw-user"))


@pytest.mark.parametrize("content, key, problem", [
    ("bogus = 1\n", "bogus", "unknown key"),
    ("max_parallel = 0\n", "max_parallel", "must be between 1 and 8"),
    ("max_parallel = 9\n", "max_parallel", "must be between 1 and 8"),
    ("sandbox = 1\n", "sandbox", "must be a boolean"),
    ("timeout_seconds = true\n", "timeout_seconds", "must be an integer"),
    ("timeout_seconds = 59\n", "timeout_seconds", "must be between 60 and 7200"),
    ("accept = \"x\"\n", "accept", "must be a list of strings"),
    ("setup = [1]\n", "setup", "must be a list of strings"),
    ("agy_path = []\n", "agy_path", "must not be empty"),
    ("agy_path = \"\"\n", "agy_path", "must not be empty"),
    ("agy_path = 5\n", "agy_path", "must be a string or a list of strings"),
    ("agy_path = [1]\n", "agy_path", "must be a string or a list of strings"),
    ("retry_limit = 6\n", "retry_limit", "must be between 0 and 5"),
    ("accept_timeout_seconds = 9\n", "accept_timeout_seconds", "must be between 10 and 7200"),
    ("model = 3\n", "model", "must be a string"),
    ("model = \"\"\n", "model", "must not be empty"),
    ("worktree_dir = 3\n", "worktree_dir", "must be a string"),
    ("max_parallel = \"3\"\n", "max_parallel", "must be an integer"),
    ("max_parallel = 1.5\n", "max_parallel", "must be an integer"),
])
def test_validation_errors_user_file(isolated, content, key, problem):
    isolated.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None)
    assert str(exc.value) == f"{isolated}: {key}: {problem}"


def test_invalid_toml(isolated):
    isolated.write_text("model = = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None)
    message = str(exc.value)
    assert message.startswith(f"{isolated}: ")
    assert "invalid TOML:" in message


def test_invalid_utf8_is_invalid_toml(isolated):
    isolated.write_bytes(b"model = \"\xff\xfe\"\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(None)


def test_validation_error_in_project_file_names_that_file(isolated, tmp_path):
    root = _project(tmp_path)
    (root / ".agw.toml").write_text("sandbox = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(str(root))
    assert str(exc.value) == f"{root / '.agw.toml'}: sandbox: must be a boolean"


def test_unreadable_config_path_is_config_error(isolated):
    isolated.mkdir()          # a directory where a file is expected
    with pytest.raises(ConfigError, match=r"cannot read"):
        load_config(None)


def test_render_config(isolated, tmp_path):
    isolated.write_text(toml_dumps({"model": "m1", "sandbox": True, "accept": ["a b", "c"]}), encoding="utf-8")
    cfg = load_config(None, {"max_parallel": 2})
    text = render_config(cfg)
    assert text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 11
    assert [ln.split(" = ", 1)[0] for ln in lines] == list(CONFIG_KEYS)
    by_key = {ln.split(" = ", 1)[0]: ln for ln in lines}
    assert by_key["model"] == f'model = "m1"  # {isolated}'
    assert by_key["sandbox"] == f"sandbox = true  # {isolated}"
    assert by_key["max_parallel"] == "max_parallel = 2  # cli"
    assert by_key["accept"] == f'accept = ["a b", "c"]  # {isolated}'
    assert by_key["timeout_seconds"] == "timeout_seconds = 1200  # default"
    assert by_key["agy_path"] == 'agy_path = ["agy"]  # default'


def test_render_config_is_valid_toml(isolated):
    import tomllib
    text = render_config(load_config(None))
    stripped = "\n".join(ln.split("  # ")[0] for ln in text.splitlines())
    parsed = tomllib.loads(stripped)
    assert set(parsed) == set(CONFIG_KEYS)


def test_user_config_path_env_then_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("AGW_CONFIG", str(tmp_path / "c.toml"))
    assert user_config_path() == tmp_path / "c.toml"
    monkeypatch.delenv("AGW_CONFIG")
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert user_config_path() == tmp_path / "agw" / "config.toml"
        return
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert user_config_path() == tmp_path / "xdg" / "agw" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert user_config_path() == pathlib.Path.home() / ".config" / "agw" / "config.toml"
