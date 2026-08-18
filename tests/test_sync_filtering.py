r"""Exclusions and conflict decisions.

These cover a real field failure: syncing a folder that already existed in the
cloud produced 606 "conflicted copy" files in one pass, including copies of
.git refs, which silently corrupts repositories.
"""

from __future__ import annotations

import os

import pytest

from cloudsync import guiconfig, syncengine as se


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# Junk and transient files
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "notes.txt", "report.pdf", "src/main.py", "a/b/c/deep.md", "Makefile",
])
def test_real_files_are_synced(name):
    assert se.should_ignore(name) is False


@pytest.mark.parametrize("name", [
    "x.tmp", "x.temp", "x.part", "x.partial", "x.swp", "x.swo",
    "x.crdownload", "build.log", "notes.bak", "mod.pyc", "app.lock",
])
def test_transient_and_build_output_is_excluded(name):
    assert se.should_ignore(name) is True


@pytest.mark.parametrize("name", [
    "~$report.docx", ".~lock.odt", ".#emacs",
])
def test_editor_lock_files_are_excluded(name):
    assert se.should_ignore(name) is True


@pytest.mark.parametrize("name", [
    ".DS_Store", "Thumbs.db", "desktop.ini", ".directory",
])
def test_os_metadata_files_are_excluded(name):
    assert se.should_ignore(name) is True


def test_matching_is_case_insensitive():
    assert se.should_ignore("BUILD.LOG") is True
    assert se.should_ignore("thumbs.DB") is True


# --------------------------------------------------------------------------- #
# Directories -- the .git case is the damaging one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    ".git/HEAD",
    "repo/.git/refs/heads/main",
    "repo/.git/hooks/pre-receive.sample",
    "__pycache__/mod.pyc",
    "project/node_modules/pkg/index.js",
    ".venv/lib/python3/site.py",
])
def test_excluded_directories_exclude_everything_under_them(path):
    """A 'conflicted copy' of HEAD or a branch ref corrupts the repository."""
    assert se.should_ignore(path) is True


def test_a_file_merely_named_like_a_repo_is_still_synced():
    assert se.should_ignore("notes-about-git.txt") is False
    assert se.should_ignore("gitignore-guide.md") is False


def test_should_ignore_dir_matches_case_insensitively():
    assert se.should_ignore_dir(".GIT") is True
    assert se.should_ignore_dir("src") is False


# --------------------------------------------------------------------------- #
# The walker prunes rather than filters
# --------------------------------------------------------------------------- #
def test_walker_skips_excluded_trees(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.txt").write_text("a")
    (tmp_path / ".git" / "refs").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "i.js").write_text("x")
    (tmp_path / "b.log").write_text("noise")
    (tmp_path / "c.md").write_text("keep me")

    found = {rel for _full, rel in se.iter_local_files(str(tmp_path))}
    assert found == {"keep/a.txt", "c.md"}


def test_walker_handles_an_empty_tree(tmp_path):
    assert list(se.iter_local_files(str(tmp_path))) == []


# --------------------------------------------------------------------------- #
# User-supplied patterns
# --------------------------------------------------------------------------- #
def test_user_patterns_extend_the_defaults():
    guiconfig.set_ignore_patterns(["*.iso", "draft-*"])
    assert se.should_ignore("ubuntu.iso") is True
    assert se.should_ignore("draft-notes.txt") is True
    assert se.should_ignore("final-notes.txt") is False


def test_user_patterns_can_match_a_path():
    guiconfig.set_ignore_patterns(["build/*"])
    assert se.should_ignore("build/output.bin") is True
    assert se.should_ignore("src/output.bin") is False


def test_bad_pattern_entries_are_ignored_not_fatal():
    guiconfig.set_ignore_patterns(["", "   "])
    assert se.should_ignore("notes.txt") is False


def test_patterns_persist():
    guiconfig.set_ignore_patterns(["*.iso"])
    assert guiconfig.get_ignore_patterns() == ["*.iso"]


# --------------------------------------------------------------------------- #
# Empty / defensive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["", ".", "/"])
def test_empty_paths_are_never_synced(name):
    assert se.should_ignore(name) is True
