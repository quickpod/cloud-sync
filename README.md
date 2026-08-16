# Cloud Sync

A fast, **offline**, **100% open-source** S3 & cloud folder sync utility for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/cloud-sync).

> **100% AI-built and open source.** Apache-2.0.

## What it does

A friendly, OneDrive-style config front-end for S3 and every major S3-compatible cloud (AWS S3, Cloudflare R2, Backblaze B2, Wasabi, DigitalOcean Spaces, MinIO and any custom endpoint). You enter each remote's endpoint, bucket and access keys yourself; the app connects to nothing on its own. Browse buckets, preview changes with a dry run, and sync a local folder up, down or both ways. Backed by rclone when installed, and it degrades gracefully with clear guidance when it is not. Runs locally, no telemetry, no dial-home.

## Install

Download **`CloudSync-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/cloud-sync) or the [GitHub release](https://github.com/quickpod/cloud-sync/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
pip install -r requirements.txt
python cloud_sync_app.py          # GUI
python -m cloudsync --help    # CLI
```

The engine is cross-platform (it auto-detects your OS and uses the right local
paths — drive letters on Windows, `$HOME`/`/mnt`/`/media` on Linux,
`/Volumes` on macOS), so it also runs from source on Linux and macOS. The
signed installer targets Windows.

## Features

- **Synced folders (Dropbox-style continuous sync)** — pick a local folder and
  a remote: **realtime mode** (the default) watches for changes and syncs them
  immediately (via `watchdog`, with a polling fallback), or switch to a
  **schedule** (every N minutes, or daily at HH:MM). A live activity feed shows
  per-file state (✓ synced / ↻ syncing / ⚠ error), an overall status chip
  lives in the header, and one click pauses/resumes everything.
- **Conflicts never lose data** — newer file wins the canonical name; the
  other version is kept as a Dropbox-style `name (conflicted copy YYYY-MM-DD)`
  file, locally and in the cloud.
- **Remotes (OneDrive-style)** — add a cloud in a simple form: pick a provider,
  paste your **endpoint, access key ID and secret**, and save. Supported out of
  the box: **Amazon S3, Cloudflare R2, Backblaze B2, Wasabi, DigitalOcean
  Spaces, MinIO / self-hosted, and any other S3-compatible endpoint.** Secrets
  are stored obscured (via rclone) in a dedicated, `0600` config file — never in
  your global rclone config.
- **Browse** — walk a remote's buckets and folders, with sizes. Runs only when
  you ask it to.
- **Sync** — choose a local folder, a remote path and a direction (**Upload ↑**,
  **Download ↓**, or **Both ⇅** via rclone bisync), then **preview with a dry
  run** before applying. Mirror (`sync`) or add-only (`copy`) modes.
- **Status** — shows rclone availability and version, your platform, and where
  the config lives.
- **rclone-backed** — uses the open-source [rclone](https://rclone.org) engine
  when present, and **degrades gracefully** with clear install guidance when it
  is not (you can still add and edit remotes fully offline).

### Privacy — offline-first, no dial-home

Cloud Sync **connects to nothing on its own**. There is no telemetry and no
auto-update. *You* configure every remote, and the only outbound traffic is the
transfer you explicitly start (Test, Browse, Sync) to the endpoint **you**
entered. Credentials never leave your machine except to reach your own cloud.

## CLI examples

Everything the GUI does is available headless via `python -m cloudsync`.
Configuring remotes is fully offline; only `test`/`ls`/`about`/`sync` reach the
network, and `sync` is a **dry run** unless you pass `--go`:

```sh
python -m cloudsync providers                      # list supported clouds
python -m cloudsync add work-s3 --provider aws \
    --access-key AKIA... --region eu-west-1        # secret via prompt or $CLOUDSYNC_SECRET
python -m cloudsync add lab --provider minio \
    --access-key AK --endpoint https://minio.example.com
python -m cloudsync remotes                        # list configured remotes (secrets redacted)

python -m cloudsync test work-s3                   # connectivity check (network)
python -m cloudsync ls work-s3 my-bucket           # list one level (network)
python -m cloudsync about work-s3                  # storage usage (network)

python -m cloudsync sync ~/Documents work-s3:my-bucket/docs        # DRY RUN preview
python -m cloudsync sync ~/Documents work-s3:my-bucket/docs --go   # apply
python -m cloudsync sync work-s3:my-bucket/docs ~/Documents --op copy --go

python -m cloudsync mounts                         # local drives / mount points
python -m cloudsync status                         # rclone + platform + config path
```

Most commands accept `--json`, and every command exits non-zero with a clean
`error:` message (never a traceback) on failure.

## Requirements

The transfer engine is [rclone](https://rclone.org/downloads/). Install it and
Cloud Sync finds it automatically; without it you can still add, edit and review
remotes offline. The GUI additionally uses `customtkinter`.

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
