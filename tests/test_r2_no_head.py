r"""R2 needs ``no_head`` or every first upload of a file reports an error.

R2 returns an ``x-amz-version-id`` on PUT even for an unversioned bucket;
rclone then reads the object back with ``HEAD ...?versionId=``, R2 answers
501, and rclone reports the copy as failed. The object is already stored, so
the retry finds it present and "succeeds" -- which is why the data was intact
while the UI still counted an error for every new file.
"""

from __future__ import annotations

import pytest

from cloudsync import rclone
from cloudsync.errors import CloudSyncError


R2 = "https://59ec8a54b24c76f6e3e0441baf21f72b.r2.cloudflarestorage.com"


@pytest.mark.parametrize("endpoint,expected", [
    (R2, True),
    ("https://ACCOUNT.R2.CloudflareStorage.com", True),
    ("ACCOUNT.r2.cloudflarestorage.com", True),
    ("https://account.r2.cloudflarestorage.com/bucket", True),
    ("https://s3.us-east-1.amazonaws.com", False),
    ("https://s3.wasabisys.com", False),
    ("", False),
    # near-misses that must not be treated as R2
    ("https://r2.cloudflarestorage.com.evil.example", False),
    # R2 accounts are <account>.r2.cloudflarestorage.com -- a sibling
    # subdomain of cloudflarestorage.com is not R2
    ("https://notr2.cloudflarestorage.com", False),
])
def test_r2_endpoints_are_recognised(endpoint, expected):
    assert rclone.is_r2_endpoint(endpoint) is expected


def test_a_new_r2_remote_gets_no_head():
    r = rclone.build_remote_section(
        "R2", "r2", "keyid", "secret", endpoint=R2, bucket="b")
    assert r.params[rclone.NO_HEAD_KEY] == "true"


def test_an_aws_remote_does_not():
    """no_head skips the upload read-back, so it is not a blanket default."""
    r = rclone.build_remote_section("S3", "aws", "keyid", "secret")
    assert rclone.NO_HEAD_KEY not in r.params


def test_an_r2_endpoint_wins_over_a_wrong_provider():
    """Hand-written and imported configs commonly say provider = AWS."""
    r = rclone.build_remote_section(
        "R2", "other", "keyid", "secret", endpoint=R2, bucket="b")
    assert r.params[rclone.NO_HEAD_KEY] == "true"


def test_an_existing_r2_config_is_migrated(tmp_path, monkeypatch):
    cfg = tmp_path / "rclone.conf"
    cfg.write_text(
        "[R2SV]\ntype = s3\nprovider = AWS\naccess_key_id = k\n"
        f"secret_access_key = s\nendpoint = {R2}\nregion = us-east-1\n")
    monkeypatch.setattr(rclone.paths, "default_config_path", lambda: cfg)
    monkeypatch.setattr(rclone.paths, "ensure_config_dir", lambda: None)

    remotes = rclone.list_remotes()
    assert remotes[0].params[rclone.NO_HEAD_KEY] == "true"
    # and it is persisted, so the fix survives a restart
    assert "no_head = true" in cfg.read_text()


def test_migration_leaves_a_non_r2_remote_alone(tmp_path, monkeypatch):
    cfg = tmp_path / "rclone.conf"
    original = ("[AWS]\ntype = s3\nprovider = AWS\naccess_key_id = k\n"
                "secret_access_key = s\nendpoint = https://s3.amazonaws.com\n")
    cfg.write_text(original)
    monkeypatch.setattr(rclone.paths, "default_config_path", lambda: cfg)
    monkeypatch.setattr(rclone.paths, "ensure_config_dir", lambda: None)

    assert rclone.NO_HEAD_KEY not in rclone.list_remotes()[0].params
    assert "no_head" not in cfg.read_text()


def test_migration_does_not_rewrite_an_already_fixed_config(tmp_path, monkeypatch):
    cfg = tmp_path / "rclone.conf"
    cfg.write_text(
        "[R2SV]\ntype = s3\nprovider = Cloudflare\naccess_key_id = k\n"
        f"secret_access_key = s\nendpoint = {R2}\nno_head = true\n")
    monkeypatch.setattr(rclone.paths, "default_config_path", lambda: cfg)
    monkeypatch.setattr(rclone.paths, "ensure_config_dir", lambda: None)
    before = cfg.stat().st_mtime_ns
    rclone.list_remotes()
    assert cfg.stat().st_mtime_ns == before, "config rewritten needlessly"


def test_a_read_only_config_still_yields_the_fix(tmp_path, monkeypatch):
    """The session must work even when the fix cannot be persisted."""
    cfg = tmp_path / "rclone.conf"
    cfg.write_text(
        "[R2SV]\ntype = s3\nprovider = AWS\naccess_key_id = k\n"
        f"secret_access_key = s\nendpoint = {R2}\n")
    monkeypatch.setattr(rclone.paths, "default_config_path", lambda: cfg)
    monkeypatch.setattr(rclone.paths, "ensure_config_dir", lambda: None)

    def boom(_remotes):
        raise CloudSyncError("read-only")
    monkeypatch.setattr(rclone, "save_remotes", boom)
    assert rclone.list_remotes()[0].params[rclone.NO_HEAD_KEY] == "true"
