"""Headless, deterministic tests for the cloudsync core.

Nothing here touches the network or a real cloud: the one subprocess seam
(``cloudsync.rclone._execute``) is monkeypatched, endpoints use the reserved
TEST-NET-1 block (192.0.2.0/24, RFC 5737 documentation addresses), and the
whole config tree is sandboxed inside a pytest ``tmp_path`` via ``$CLOUDSYNC_HOME``.
GUI and OS-specific bits are skipped on headless / win32 hosts.
"""

from __future__ import annotations

import os
import sys

import pytest

import cloudsync
from cloudsync import (
    BUCKET_KEY,
    CloudSyncError,
    build_remote_section,
    build_sync_args,
    friendly_test_error,
    human_size,
    parse_about,
    parse_config,
    parse_lsjson,
    remote_path,
    render_config,
    sanitize_endpoint,
    validate_remote_name,
)
from cloudsync import paths, rclone, syncengine

# Reserved documentation endpoints — never routable, never a real service.
MINIO_ENDPOINT = "https://192.0.2.10:9000"
OTHER_ENDPOINT = "https://192.0.2.20"


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, monkeypatch):
    """Point the whole config tree at a temp dir for every test."""
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    yield tmp_path


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def test_provider_catalogue_has_the_majors():
    keys = set(rclone.provider_keys())
    for expected in ("aws", "r2", "b2", "wasabi", "spaces", "minio", "other"):
        assert expected in keys
    aws = rclone.get_provider("aws")
    assert aws.needs_endpoint is False        # AWS is region-addressed
    assert rclone.get_provider("minio").needs_endpoint is True


def test_get_provider_unknown_raises():
    with pytest.raises(CloudSyncError):
        rclone.get_provider("dropbox")


# --------------------------------------------------------------------------- #
# Remote-name validation
# --------------------------------------------------------------------------- #
def test_validate_remote_name_ok():
    assert validate_remote_name("  work-s3 ") == "work-s3"
    assert validate_remote_name("R2.backups_1") == "R2.backups_1"


@pytest.mark.parametrize("bad", ["", "  ", "-leading", "has space", "no/slash"])
def test_validate_remote_name_rejects(bad):
    with pytest.raises(CloudSyncError):
        validate_remote_name(bad)


# --------------------------------------------------------------------------- #
# build_remote_section
# --------------------------------------------------------------------------- #
def test_build_remote_section_minio_fields():
    r = build_remote_section(
        "lab", "minio", "AKIAEXAMPLE", "obscured-secret",
        endpoint=MINIO_ENDPOINT, region="")
    assert r.name == "lab"
    assert r.params["type"] == "s3"
    assert r.params["provider"] == "Minio"
    assert r.params["access_key_id"] == "AKIAEXAMPLE"
    assert r.params["endpoint"] == MINIO_ENDPOINT
    assert r.provider_key == "minio"


def test_build_remote_section_aws_needs_no_endpoint_and_defaults_region():
    r = build_remote_section("aws1", "aws", "AK", "sekret")
    assert "endpoint" not in r.params
    assert r.params["region"] == "us-east-1"     # provider default applied
    assert r.provider_key == "aws"


def test_build_remote_section_endpoint_required_for_r2():
    with pytest.raises(CloudSyncError):
        build_remote_section("r2a", "r2", "AK", "sekret")   # no endpoint


def test_build_remote_section_requires_keys():
    with pytest.raises(CloudSyncError):
        build_remote_section("x", "aws", "", "sekret")
    with pytest.raises(CloudSyncError):
        build_remote_section("x", "aws", "AK", "")


def test_build_remote_section_stores_bucket_under_app_key():
    r = build_remote_section("r2a", "r2", "AK", "sekret",
                             endpoint="https://192.0.2.30", bucket="/mybkt/")
    assert r.params[BUCKET_KEY] == "mybkt"      # slashes trimmed
    assert r.bucket == "mybkt"
    assert r.as_dict()["bucket"] == "mybkt"
    # no bucket -> key entirely absent (round-trips as before)
    r2 = build_remote_section("r2b", "r2", "AK", "sekret",
                              endpoint="https://192.0.2.30")
    assert BUCKET_KEY not in r2.params
    assert r2.bucket == ""


def test_build_remote_section_sanitizes_endpoint_path():
    r = build_remote_section("r2a", "r2", "AK", "sekret",
                             endpoint="https://192.0.2.30/abcd/")
    assert r.params["endpoint"] == "https://192.0.2.30"


# --------------------------------------------------------------------------- #
# Endpoint sanitization (defect 2: a path in the endpoint breaks SigV4)
# --------------------------------------------------------------------------- #
def test_sanitize_endpoint_strips_path():
    clean, stripped = sanitize_endpoint(
        "https://acct.r2.cloudflarestorage.com/abcd/")
    assert clean == "https://acct.r2.cloudflarestorage.com"
    assert stripped == "abcd"


def test_sanitize_endpoint_trailing_slash_is_not_a_path():
    clean, stripped = sanitize_endpoint("https://192.0.2.30/")
    assert clean == "https://192.0.2.30"
    assert stripped == ""


def test_sanitize_endpoint_untouched_when_clean():
    clean, stripped = sanitize_endpoint("https://192.0.2.30:9000")
    assert (clean, stripped) == ("https://192.0.2.30:9000", "")


def test_sanitize_endpoint_deep_path_query_and_fragment():
    clean, stripped = sanitize_endpoint(
        "https://192.0.2.30/bkt/sub/?x=1#frag")
    assert clean == "https://192.0.2.30"
    assert stripped == "bkt/sub/?x=1#frag".strip("/")


def test_sanitize_endpoint_schemeless():
    clean, stripped = sanitize_endpoint("minio.example.com:9000/data/")
    assert clean == "minio.example.com:9000"
    assert stripped == "data"


def test_sanitize_endpoint_empty():
    assert sanitize_endpoint("") == ("", "")
    assert sanitize_endpoint("   ") == ("", "")


# --------------------------------------------------------------------------- #
# Connectivity-error mapping (defect 1: raw 403s are unactionable)
# --------------------------------------------------------------------------- #
def test_friendly_test_error_signature_mismatch():
    msg = friendly_test_error(
        "ERROR : : error listing: SignatureDoesNotMatch: status code 403")
    assert "Secret Access Key" in msg
    assert "SHA-256" in msg
    assert "no path" in msg


def test_friendly_test_error_access_denied_root_suggests_bucket_field():
    msg = friendly_test_error(
        "ERROR : AccessDenied: Access Denied, status code: 403")
    assert "Bucket field" in msg
    assert "scoped" in msg
    assert "403" not in msg          # no raw status dump


def test_friendly_test_error_access_denied_with_bucket():
    msg = friendly_test_error(
        "AccessDenied: Access Denied, status code: 403", bucket="mybkt")
    assert "mybkt" in msg
    assert "scoped" in msg


def test_friendly_test_error_passthrough():
    other = "connection refused: dial tcp 192.0.2.30:443"
    assert friendly_test_error(other) == other


# --------------------------------------------------------------------------- #
# Config INI round-trip
# --------------------------------------------------------------------------- #
def test_config_roundtrip_preserves_everything():
    r = build_remote_section("lab", "minio", "AK", "obscured",
                             endpoint=MINIO_ENDPOINT, region="us-east-1")
    text = render_config([r])
    again = parse_config(text)
    assert len(again) == 1
    assert again[0].name == "lab"
    assert again[0].params == r.params


def test_parse_config_empty_is_empty():
    assert parse_config("") == []
    assert parse_config("   \n") == []


def test_parse_config_preserves_unknown_keys():
    text = (
        "[legacy]\n"
        "type = s3\n"
        "provider = Other\n"
        "access_key_id = AK\n"
        "secret_access_key = obscured\n"
        f"endpoint = {OTHER_ENDPOINT}\n"
        "storage_class = GLACIER\n"     # key the model does not know about
    )
    remotes = parse_config(text)
    assert remotes[0].params["storage_class"] == "GLACIER"
    # round-trips back out unchanged
    assert "storage_class = GLACIER" in render_config(remotes)


def test_config_roundtrip_preserves_bucket_key_losslessly():
    r = build_remote_section("r2a", "r2", "AK", "obscured",
                             endpoint="https://192.0.2.30", bucket="mybkt")
    text = render_config([r])
    assert f"{BUCKET_KEY} = mybkt" in text
    again = parse_config(text)
    assert again[0].bucket == "mybkt"
    assert again[0].params == r.params
    # and a config written by hand round-trips too
    hand = f"[x]\ntype = s3\n{BUCKET_KEY} = other-bkt\n"
    assert parse_config(hand)[0].bucket == "other-bkt"
    assert f"{BUCKET_KEY} = other-bkt" in render_config(parse_config(hand))


def test_parse_config_percent_in_value_is_literal():
    text = "[r]\ntype = s3\nsecret_access_key = ab%cd\n"
    remotes = parse_config(text)
    assert remotes[0].params["secret_access_key"] == "ab%cd"


# --------------------------------------------------------------------------- #
# Command / path builders
# --------------------------------------------------------------------------- #
def test_remote_path():
    assert remote_path("work") == "work:"
    assert remote_path("work", "/bucket/dir/") == "work:bucket/dir/"
    with pytest.raises(CloudSyncError):
        remote_path("")


def test_build_sync_args_variants():
    assert build_sync_args("/a", "work:b") == ["sync", "/a", "work:b"]
    assert build_sync_args("work:b", "/a", operation="copy", dry_run=True) == \
        ["copy", "work:b", "/a", "--dry-run"]
    assert build_sync_args("/a", "work:b", operation="bisync")[0] == "bisync"


def test_build_sync_args_rejects_bad_input():
    with pytest.raises(CloudSyncError):
        build_sync_args("/a", "work:b", operation="delete")
    with pytest.raises(CloudSyncError):
        build_sync_args("", "work:b")


# --------------------------------------------------------------------------- #
# Output parsers
# --------------------------------------------------------------------------- #
def test_parse_lsjson_orders_dirs_first():
    text = """[
      {"Path":"z.txt","Name":"z.txt","Size":10,"IsDir":false},
      {"Path":"docs","Name":"docs","Size":-1,"IsDir":true},
      {"Path":"a.txt","Name":"a.txt","Size":2048,"IsDir":false}
    ]"""
    entries = parse_lsjson(text)
    assert [e.name for e in entries] == ["docs", "a.txt", "z.txt"]
    assert entries[0].is_dir is True
    assert entries[1].size == 2048


def test_parse_lsjson_bad_input_is_empty():
    assert parse_lsjson("") == []
    assert parse_lsjson("not json") == []
    assert parse_lsjson('{"not":"a list"}') == []


def test_parse_about():
    info = parse_about('{"total":1000,"used":400,"free":600,"objects":12}')
    assert info == {"total": 1000, "used": 400, "free": 600, "objects": 12}
    assert parse_about("garbage")["total"] is None


def test_human_size():
    assert human_size(0) == "0B"
    assert human_size(1023) == "1023B"
    assert human_size(1024) == "1.0KB"
    assert human_size(None) == "—"


# --------------------------------------------------------------------------- #
# A8: OS / filesystem path detection
# --------------------------------------------------------------------------- #
def test_platform_predicates_are_consistent():
    trues = [paths.is_windows(), paths.is_macos(), paths.is_linux()]
    assert trues.count(True) <= 1          # at most one platform is "current"
    assert paths.platform_label()


def test_config_dir_honours_home_override(tmp_path):
    assert str(paths.config_dir()) == str(tmp_path)
    assert str(paths.default_config_path()) == os.path.join(str(tmp_path), "rclone.conf")


def test_local_mounts_start_with_home_and_are_dicts():
    mounts = paths.local_mounts()
    assert mounts and mounts[0]["label"] == "Home"
    for m in mounts:
        assert set(m) == {"path", "label"}


def test_normalize_local_expands_user():
    got = paths.normalize_local("~")
    assert os.path.isabs(got)


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_windows_mounts_include_a_drive_letter():
    mounts = paths.local_mounts()
    assert any(m["label"].endswith("drive") for m in mounts)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX roots only")
def test_unix_mounts_include_root():
    assert any(m["path"] == "/" for m in paths.local_mounts())


# --------------------------------------------------------------------------- #
# Operations — the single subprocess seam is monkeypatched (no rclone, no net)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_rclone(monkeypatch):
    """Pretend rclone is installed and record every argv it would run."""
    monkeypatch.setattr(rclone, "_rclone_path", lambda: "/usr/bin/rclone")
    calls = []

    def fake_execute(argv, timeout=120):
        calls.append(argv)
        # obscure just echoes a deterministic token
        if argv[1:3] == ["--config", str(paths.default_config_path())] and \
           len(argv) >= 4 and argv[3] == "obscure":
            return 0, "OBSCURED(" + argv[4] + ")", ""
        return 0, "", ""

    monkeypatch.setattr(rclone, "_execute", fake_execute)
    return calls


def test_add_list_delete_remote_roundtrip_offline(monkeypatch):
    # Force rclone "not installed": secret is stored as typed, config still saved.
    monkeypatch.setattr(rclone, "_rclone_path", lambda: None)
    assert rclone.list_remotes() == []
    rclone.add_remote("lab", "minio", "AK", "plain-secret",
                      endpoint=MINIO_ENDPOINT)
    names = rclone.remote_names()
    assert names == ["lab"]
    r = rclone.get_remote("lab")
    assert r.params["secret_access_key"] == "plain-secret"   # not obscured
    assert r.endpoint == MINIO_ENDPOINT
    # duplicate without overwrite fails
    with pytest.raises(CloudSyncError):
        rclone.add_remote("lab", "minio", "AK", "x", endpoint=MINIO_ENDPOINT)
    rclone.delete_remote("lab")
    assert rclone.remote_names() == []
    with pytest.raises(CloudSyncError):
        rclone.delete_remote("lab")


def test_add_remote_stores_secret_verbatim(fake_rclone):
    # rclone's S3 backend signs with secret_access_key exactly as written, so
    # storing anything but the literal key (an `rclone obscure` blob, say)
    # makes every request fail with SignatureDoesNotMatch.
    rclone.add_remote("lab", "minio", "AK", "topsecret", endpoint=MINIO_ENDPOINT)
    r = rclone.get_remote("lab")
    assert r.params["secret_access_key"] == "topsecret"
    assert not any("obscure" in argv for argv in fake_rclone)
    # every rclone call was pinned to our dedicated --config file
    assert all(c[1] == "--config" for c in fake_rclone)


def test_add_remote_rejects_empty_secret(fake_rclone):
    with pytest.raises(CloudSyncError):
        rclone.add_remote("lab", "minio", "AK", "   ", endpoint=MINIO_ENDPOINT)


def test_obscured_secret_from_older_config_is_migrated(monkeypatch):
    """A config written by the obscuring versions is repaired on first read."""
    monkeypatch.setattr(rclone, "_rclone_path", lambda: None)
    rclone.add_remote("lab", "minio", "AK", "OBSCUREDBLOB", endpoint=MINIO_ENDPOINT)

    monkeypatch.setattr(rclone, "_rclone_path", lambda: "/usr/bin/rclone")
    monkeypatch.setattr(
        rclone, "_execute",
        lambda argv, timeout=120: (0, "real-secret", "") if "reveal" in argv
        else (0, "", ""))

    assert rclone.get_remote("lab").params["secret_access_key"] == "real-secret"
    # persisted, so the reveal happens once rather than on every read
    assert "real-secret" in paths.default_config_path().read_text()


def test_plain_secret_that_looks_base64_is_left_alone(monkeypatch):
    """A 64-hex key decodes as base64 but reveals to bytes, not a real key.

    Guards the migration against corrupting keys that merely *look* obscured.
    """
    monkeypatch.setattr(rclone, "_rclone_path", lambda: None)
    hex_secret = "a" * 64
    rclone.add_remote("r2", "minio", "AK", hex_secret, endpoint=MINIO_ENDPOINT)

    monkeypatch.setattr(rclone, "_rclone_path", lambda: "/usr/bin/rclone")
    # rclone "reveals" it to non-printable bytes; the helper must reject that
    monkeypatch.setattr(
        rclone, "_execute",
        lambda argv, timeout=120: (0, "\x01\x02 garbage\x7f", "") if "reveal" in argv
        else (0, "", ""))

    assert rclone.get_remote("r2").params["secret_access_key"] == hex_secret


def test_config_file_is_written_with_tight_perms(monkeypatch, tmp_path):
    monkeypatch.setattr(rclone, "_rclone_path", lambda: None)
    rclone.add_remote("lab", "minio", "AK", "s", endpoint=MINIO_ENDPOINT)
    cfg = paths.default_config_path()
    assert cfg.exists()
    if os.name == "posix":
        assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def test_list_path_builds_lsjson_argv(fake_rclone, monkeypatch):
    rclone.add_remote("lab", "minio", "AK", "s", endpoint=MINIO_ENDPOINT)
    monkeypatch.setattr(
        rclone, "_execute",
        lambda argv, timeout=120: (0, '[{"Name":"a","Path":"a","Size":1,"IsDir":false}]', ""))
    entries = rclone.list_path("lab", "bucket")
    assert entries[0].name == "a"


def test_sync_builds_dry_run_argv(fake_rclone):
    fake_rclone.clear()
    rclone.sync("/local", "lab:bucket", operation="sync", dry_run=True)
    argv = fake_rclone[-1]
    assert "sync" in argv and "--dry-run" in argv and "--stats" in argv
    assert argv[0] == "/usr/bin/rclone" and argv[1] == "--config"


def test_operations_without_rclone_raise(monkeypatch):
    monkeypatch.setattr(rclone, "_rclone_path", lambda: None)
    with pytest.raises(CloudSyncError):
        rclone.sync("/a", "lab:b")
    with pytest.raises(CloudSyncError):
        rclone.rclone_version()


def test_test_remote_unknown_raises():
    with pytest.raises(CloudSyncError):
        rclone.test_remote("nope")


# --------------------------------------------------------------------------- #
# Bucket-aware test / browse / about (bucket-scoped credentials, e.g. R2)
# --------------------------------------------------------------------------- #
def test_add_remote_persists_bucket(fake_rclone):
    rclone.add_remote("r2a", "r2", "AK", "s",
                      endpoint=OTHER_ENDPOINT, bucket="mybkt")
    assert rclone.get_remote("r2a").bucket == "mybkt"


def test_test_remote_without_bucket_lists_root(fake_rclone):
    rclone.add_remote("lab", "minio", "AK", "s", endpoint=MINIO_ENDPOINT)
    fake_rclone.clear()
    rclone.test_remote("lab")
    assert fake_rclone[-1][-2:] == ["lsd", "lab:"]


def test_test_remote_with_bucket_lists_the_bucket(fake_rclone):
    rclone.add_remote("r2a", "r2", "AK", "s",
                      endpoint=OTHER_ENDPOINT, bucket="mybkt")
    fake_rclone.clear()
    rclone.test_remote("r2a")
    assert fake_rclone[-1][-2:] == ["lsd", "r2a:mybkt"]


def test_test_remote_bucket_falls_back_to_stat(fake_rclone, monkeypatch):
    rclone.add_remote("r2a", "r2", "AK", "s",
                      endpoint=OTHER_ENDPOINT, bucket="mybkt")
    calls = []

    def picky(argv, timeout=120):
        calls.append(argv)
        if "lsd" in argv:
            return 1, "", "ERROR : some gateways refuse lsd on a bucket"
        return 0, "{}", ""

    monkeypatch.setattr(rclone, "_execute", picky)
    rclone.test_remote("r2a")                    # must not raise
    assert any("--stat" in c for c in calls)
    assert calls[-1][-1] == "r2a:mybkt"


def test_test_remote_maps_access_denied_to_bucket_hint(fake_rclone, monkeypatch):
    rclone.add_remote("r2a", "r2", "AK", "s", endpoint=OTHER_ENDPOINT)
    monkeypatch.setattr(
        rclone, "_execute",
        lambda argv, timeout=120:
            (1, "", "ERROR : : error listing: AccessDenied: Access Denied, "
                    "status code: 403"))
    with pytest.raises(CloudSyncError) as ei:
        rclone.test_remote("r2a")
    assert "Bucket field" in str(ei.value)


def test_test_remote_maps_signature_mismatch(fake_rclone, monkeypatch):
    rclone.add_remote("r2a", "r2", "AK", "s",
                      endpoint=OTHER_ENDPOINT, bucket="mybkt")
    monkeypatch.setattr(
        rclone, "_execute",
        lambda argv, timeout=120:
            (1, "", "ERROR : SignatureDoesNotMatch: status code: 403"))
    with pytest.raises(CloudSyncError) as ei:
        rclone.test_remote("r2a")
    assert "Secret Access Key" in str(ei.value)


def test_about_uses_bucket_when_set(fake_rclone):
    rclone.add_remote("r2a", "r2", "AK", "s",
                      endpoint=OTHER_ENDPOINT, bucket="mybkt")
    fake_rclone.clear()
    rclone.about("r2a")
    assert "r2a:mybkt" in fake_rclone[-1]


# --------------------------------------------------------------------------- #
# GUI: headless-safe entry point (catches ImportError, returns 0)
# --------------------------------------------------------------------------- #
def test_gui_module_imports_without_display():
    from cloudsync import gui
    assert callable(gui.main)


def test_gui_main_degrades_on_missing_ctk(monkeypatch):
    from cloudsync import gui

    def boom():
        raise ImportError("no customtkinter here")

    monkeypatch.setattr(gui, "build_app", boom)
    assert gui.main() == 0        # must swallow ImportError and return 0


def test_public_api_surface():
    for name in ("add_remote", "sync", "list_path", "rclone_available",
                 "PROVIDERS", "paths", "sanitize_endpoint",
                 "friendly_test_error", "BUCKET_KEY"):
        assert hasattr(cloudsync, name)


# --------------------------------------------------------------------------- #
# The freedesktop trash on a secondary volume must never be uploaded.
#
# IGNORE_DIRS listed ".Trash", the spelling used only at the top of the user's
# own filesystem.  Every other mounted volume names it ".Trash-<uid>", so the
# trash on a removable or secondary disk was synced like ordinary data.
# Reported from the field as an error naming
# ".Trash-1000/files/efi-backup-2026-08-14/RESTORE.md" -- a file the user never
# chose to sync, which the desktop deleted mid-transfer, leaving rclone to
# report the transfer corrupted.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    ".Trash", ".Trash-1000", ".Trash-0", ".trash-1000", ".TRASH-1000",
])
def test_uid_suffixed_trash_is_ignored(name):
    assert syncengine.should_ignore_dir(name)
    assert syncengine.should_ignore(name)


@pytest.mark.parametrize("path", [
    ".Trash-1000/files/efi-backup-2026-08-14/RESTORE.md",
    ".Trash-1000/info/thing.trashinfo",
    "sub/.Trash-1000/files/deep/x.bin",
])
def test_contents_of_trash_are_ignored(path):
    assert syncengine.should_ignore(path)


@pytest.mark.parametrize("path", [
    "Trash-notes.md", "my.Trash-file.txt", "Documents/trash-report.md",
    ".Trashy/keep.md", "trash.md",
])
def test_ordinary_names_are_not_swept_up(path):
    """The prefix rule must not swallow real user files."""
    assert not syncengine.should_ignore(path)
