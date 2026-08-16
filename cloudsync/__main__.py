r"""Command-line interface: ``python -m cloudsync <command> ...``.

Offline-first and safe by default.  Configuring remotes (``add``, ``remove``,
``remotes``, ``providers``, ``mounts``, ``status``) never touches the network.
The commands that do reach a cloud (``test``, ``ls``, ``about``, ``sync``) run
only when you invoke them, use the endpoint and keys *you* configured, and go
through rclone.  ``sync`` defaults to a ``--dry-run`` preview unless you pass
``--go``.  A :class:`CloudSyncError` becomes a clean non-zero exit, never a
traceback.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from . import (
    CloudSyncError,
    PROVIDERS,
    about,
    add_remote,
    delete_remote,
    human_size,
    list_path,
    list_remotes,
    paths,
    rclone_available,
    rclone_version,
    remote_path,
    sync,
    test_remote,
)
from .rclone import NO_RCLONE_MSG, get_provider, sanitize_endpoint


# --- helpers ----------------------------------------------------------------


def _resolve_secret(a):
    """Get the secret from --secret, $CLOUDSYNC_SECRET, or an interactive prompt."""
    if a.secret:
        return a.secret
    env = os.environ.get("CLOUDSYNC_SECRET")
    if env:
        return env
    try:
        return getpass.getpass("Secret access key: ")
    except (EOFError, KeyboardInterrupt):
        raise CloudSyncError("No secret access key provided.")


# --- command handlers -------------------------------------------------------


def cmd_providers(a):
    if a.json:
        print(json.dumps([{
            "key": p.key, "label": p.label, "rclone_provider": p.rclone_provider,
            "needs_endpoint": p.needs_endpoint, "default_region": p.default_region,
            "endpoint_hint": p.endpoint_hint} for p in PROVIDERS], indent=2))
        return
    print(f"{'KEY':<10} {'PROVIDER':<24} ENDPOINT")
    print("-" * 60)
    for p in PROVIDERS:
        ep = p.endpoint_hint if p.needs_endpoint else "(region-addressed)"
        print(f"{p.key:<10} {p.label:<24} {ep}")


def cmd_remotes(a):
    remotes = list_remotes()
    if a.json:
        print(json.dumps([r.as_dict() for r in remotes], indent=2))
        return
    if not remotes:
        print("No remotes configured yet. Add one with 'cloudsync add'.")
        return
    print(f"{'NAME':<20} {'PROVIDER':<22} {'REGION':<12} ENDPOINT")
    print("-" * 78)
    for r in remotes:
        ep = r.endpoint or "—"
        if r.bucket:
            ep += f"  (bucket: {r.bucket})"
        print(f"{r.name:<20} {r.provider_label():<22} "
              f"{(r.region or '—'):<12} {ep}")
    print(f"\n{len(remotes)} remote(s). Config: {paths.default_config_path()}")


def cmd_add(a):
    prov = get_provider(a.provider)  # validates early with a clear message
    secret = _resolve_secret(a)
    clean_ep, stripped = sanitize_endpoint(a.endpoint or "")
    if stripped:
        print(f"note: removed '/{stripped}' from the endpoint (kept "
              f"{clean_ep}) — bucket names go in --bucket, not the endpoint.")
    remote = add_remote(a.name, prov.key, a.access_key, secret,
                        endpoint=a.endpoint, region=a.region,
                        bucket=a.bucket, overwrite=a.force)
    where = "obscured and saved" if rclone_available() else "saved (rclone not installed)"
    print(f"Remote {remote.name!r} ({remote.provider_label()}) {where}.")
    if not rclone_available():
        print(NO_RCLONE_MSG)


def cmd_remove(a):
    delete_remote(a.name)
    print(f"Removed remote {a.name!r}.")


def cmd_test(a):
    out = test_remote(a.name)
    print(f"OK — {a.name} is reachable.")
    if out.strip():
        print(out.rstrip())


def cmd_ls(a):
    path = a.path
    if not path:
        # A remote with a configured bucket lists from that bucket, never the
        # account root (bucket-scoped credentials cannot list the root).
        from .rclone import get_remote
        path = get_remote(a.name).bucket
    entries = list_path(a.name, path)
    if a.json:
        print(json.dumps([e.as_dict() for e in entries], indent=2))
        return
    if not entries:
        print(f"(empty) {remote_path(a.name, path)}")
        return
    for e in entries:
        kind = "d" if e.is_dir else "-"
        size = "" if e.is_dir else human_size(e.size)
        print(f"{kind} {size:>10}  {e.name}")


def cmd_about(a):
    info = about(a.name)
    if a.json:
        print(json.dumps(info, indent=2))
        return
    print(f"Storage for {a.name}:")
    print(f"  Total   : {human_size(info['total'])}")
    print(f"  Used    : {human_size(info['used'])}")
    print(f"  Free    : {human_size(info['free'])}")
    if info.get("objects") is not None:
        print(f"  Objects : {info['objects']}")


def cmd_sync(a):
    op = a.op
    dry = not a.go
    label = "DRY RUN (no changes)" if dry else f"{op.upper()} (live)"
    print(f"{label}: {a.source}  ->  {a.dest}")
    out = sync(a.source, a.dest, operation=op, dry_run=dry)
    if out.strip():
        print(out.rstrip())
    if dry:
        print("\nThis was a preview. Re-run with --go to apply the changes.")


def cmd_mounts(a):
    mounts = paths.local_mounts()
    if a.json:
        print(json.dumps(mounts, indent=2))
        return
    print(f"Local roots on {paths.platform_label()}:")
    for m in mounts:
        print(f"  {m['label']:<28} {m['path']}")


def cmd_status(a):
    avail = rclone_available()
    info = {
        "platform": paths.platform_label(),
        "config_path": str(paths.default_config_path()),
        "rclone_available": avail,
        "remotes": len(list_remotes()),
    }
    if avail:
        try:
            info["rclone_version"] = rclone_version()
        except CloudSyncError:
            info["rclone_version"] = None
    if a.json:
        print(json.dumps(info, indent=2))
        return
    print(f"Platform     : {info['platform']}")
    print(f"Config file  : {info['config_path']}")
    print(f"Remotes      : {info['remotes']}")
    if avail:
        print(f"rclone       : {info.get('rclone_version') or 'installed'}")
    else:
        print("rclone       : NOT installed")
        print("\n" + NO_RCLONE_MSG)


# --- parser -----------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="cloudsync",
        description="Configure S3 / major-cloud remotes and sync folders. "
        "Offline-first: you configure every remote; nothing connects on its own.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help, handler):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(func=handler)
        return sp

    s = add("providers", "List the supported cloud providers", cmd_providers)
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("remotes", "List your configured remotes (secrets redacted)", cmd_remotes)
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("add", "Add or update a remote (offline; secret is obscured)", cmd_add)
    s.add_argument("name", help="a name for this remote, e.g. 'work-s3'")
    s.add_argument("--provider", required=True,
                   help="provider key (see 'cloudsync providers')")
    s.add_argument("--access-key", required=True, metavar="ID",
                   help="access key ID")
    s.add_argument("--secret", metavar="KEY",
                   help="secret access key (else $CLOUDSYNC_SECRET or a prompt)")
    s.add_argument("--endpoint", metavar="URL",
                   help="endpoint URL (required for non-AWS providers); "
                        "host only — any path is stripped")
    s.add_argument("--region", metavar="R", help="region (optional)")
    s.add_argument("--bucket", metavar="B",
                   help="bucket name — required if your credentials are "
                        "scoped to one bucket (e.g. a Cloudflare R2 API "
                        "token limited to a single bucket); test/ls then "
                        "start inside it")
    s.add_argument("-f", "--force", action="store_true",
                   help="overwrite an existing remote of the same name")

    s = add("remove", "Delete a configured remote", cmd_remove)
    s.add_argument("name")

    s = add("test", "Check that a remote is reachable (network)", cmd_test)
    s.add_argument("name")

    s = add("ls", "List one level of a remote (network)", cmd_ls)
    s.add_argument("name")
    s.add_argument("path", nargs="?", default="", help="sub-path, e.g. bucket/folder")
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("about", "Show a remote's storage usage (network)", cmd_about)
    s.add_argument("name")
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("sync", "Sync SOURCE to DEST (dry-run unless --go)", cmd_sync)
    s.add_argument("source", help="local path or remote:path")
    s.add_argument("dest", help="local path or remote:path")
    s.add_argument("--op", choices=("sync", "copy", "bisync"), default="sync",
                   help="sync (mirror), copy (add only), or bisync (both ways)")
    s.add_argument("--go", action="store_true",
                   help="actually apply changes (default is a safe dry run)")

    s = add("mounts", "List local drives / mount points on this machine", cmd_mounts)
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("status", "Show rclone availability, platform and config path", cmd_status)
    s.add_argument("--json", action="store_true", help="emit JSON")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CloudSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
