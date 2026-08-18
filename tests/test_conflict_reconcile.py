r"""Automatic tidying of conflicted copies.

Most "conflicted copy" files are the same bytes under a second name. Removing
those automatically is safe; anything that genuinely differs must be left for
the user, because only they can say which version they want.
"""

from __future__ import annotations

import os

from cloudsync.syncengine import conflicted_name, reconcile_conflicts

TAG = " (conflicted copy 2026-08-18)"


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_identical_copy_is_removed(tmp_path):
    write(tmp_path / "notes.txt", "same")
    write(tmp_path / f"notes{TAG}.txt", "same")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed, kept) == (0, 1, [])
    assert (tmp_path / "notes.txt").exists()
    assert not (tmp_path / f"notes{TAG}.txt").exists()


def test_a_copy_whose_original_vanished_is_restored(tmp_path):
    """This is how a private key went missing -- the copy IS the file."""
    write(tmp_path / f"secret{TAG}.key", "KEY DATA")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed) == (1, 0)
    assert (tmp_path / "secret.key").read_text() == "KEY DATA"


def test_a_genuine_conflict_is_left_alone(tmp_path):
    """Differing content is the user's call, never ours."""
    write(tmp_path / "doc.md", "mine")
    write(tmp_path / f"doc{TAG}.md", "theirs")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed) == (0, 0)
    assert len(kept) == 1
    assert (tmp_path / "doc.md").read_text() == "mine"
    assert (tmp_path / f"doc{TAG}.md").read_text() == "theirs"


def test_same_size_but_different_content_is_kept(tmp_path):
    """Size alone is not identity -- the bytes must match."""
    write(tmp_path / "a.txt", "AAAA")
    write(tmp_path / f"a{TAG}.txt", "BBBB")
    _r, removed, kept = reconcile_conflicts(str(tmp_path))
    assert removed == 0 and len(kept) == 1


def test_doubly_tagged_names_resolve_to_the_original(tmp_path):
    """Successive resolutions stack the tag; all of it must come off."""
    name = f"report{TAG}{TAG}.pdf"
    write(tmp_path / name, "data")
    restored, _removed, _kept = reconcile_conflicts(str(tmp_path))
    assert restored == 1
    assert (tmp_path / "report.pdf").exists()


def test_it_recurses(tmp_path):
    sub = tmp_path / "deep" / "deeper"
    sub.mkdir(parents=True)
    write(sub / "x.txt", "same")
    write(sub / f"x{TAG}.txt", "same")
    _r, removed, _k = reconcile_conflicts(str(tmp_path))
    assert removed == 1


def test_ordinary_files_are_untouched(tmp_path):
    write(tmp_path / "keep.txt", "content")
    write(tmp_path / "notes-about-conflicts.txt", "content")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed, kept) == (0, 0, [])
    assert (tmp_path / "keep.txt").exists()
    assert (tmp_path / "notes-about-conflicts.txt").exists()


def test_removal_can_be_switched_off(tmp_path):
    write(tmp_path / "a.txt", "same")
    write(tmp_path / f"a{TAG}.txt", "same")
    _r, removed, kept = reconcile_conflicts(str(tmp_path), remove_identical=False)
    assert removed == 0 and len(kept) == 1


def test_the_counter_form_is_tidied_too(tmp_path):
    """A second conflict on the same day gets a counter -- ``... 2016-01-01 2``.

    Those are the copies that actually pile up: the first conflict of the day
    takes the plain name, every one after it is numbered. Missing them leaves
    exactly the accumulation the tidying exists to clear.
    """
    original = write(tmp_path / "notes.txt", "same")
    # Built by the generator itself, so the shape can never drift from it.
    write(conflicted_name(str(original)), "same")
    write(conflicted_name(str(original)), "same")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed, kept) == (0, 2, [])
    assert (tmp_path / "notes.txt").read_text() == "same"


def test_a_numbered_copy_whose_original_is_gone_is_restored(tmp_path):
    original = tmp_path / "key.pem"
    write(original, "PRIVATE KEY")
    # The counter only appears once the plain name is taken, so the first copy
    # has to exist on disk before the second one is asked for.
    first = write(conflicted_name(str(original)), "PRIVATE KEY")
    numbered = write(conflicted_name(str(original)), "PRIVATE KEY")
    assert " 2)" in os.path.basename(numbered), numbered
    os.remove(original)
    os.remove(first)
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed) == (1, 0)
    assert original.read_text() == "PRIVATE KEY"


def test_excluded_directories_are_never_touched(tmp_path):
    """Tidying must respect the same boundary the sync itself respects.

    iter_local_files prunes these deliberately -- walking into .git "risks
    acting on files that must never move". Renaming and deleting is exactly
    that, so the tidy pass must prune them too.
    """
    for excluded in (".git", "node_modules", ".venv"):
        d = tmp_path / excluded
        d.mkdir()
        write(d / "thing.txt", "same")
        write(d / f"thing{TAG}.txt", "same")
    restored, removed, kept = reconcile_conflicts(str(tmp_path))
    assert (restored, removed, kept) == (0, 0, [])
    for excluded in (".git", "node_modules", ".venv"):
        assert (tmp_path / excluded / f"thing{TAG}.txt").exists()
