# iOS Network Backup Docker App

This project packages `libimobiledevice` tooling and `netmuxd` together with a FastAPI + HTML dashboard to pair iOS devices and trigger incremental (incremental-capable) backups over the network. USB pairing workflow is fully supported on Linux hosts. On macOS (Docker Desktop) direct USB passthrough is not available, so you must pair on the host and then run network backups inside the container.

## Key Features

- Run `netmuxd` inside the container (or attach to host usbmuxd).
- Pair devices via USB directly from the web API.
- Trigger incremental backups using `idevicebackup2` (Apple-style MobileSync format).
- Persist lockdown pairing certificates and backup data across container restarts.
- Simple REST API + Server-Sent Events (SSE) log streaming for backup progress.
- Multi-architecture support (amd64, arm64) with automatic fallback to source build for `netmuxd`.
- Built-in HTML dashboard at `/ui` (root `/` redirects there) for health, devices, pairing, backup jobs, and log streaming.

## Repository Layout

- `Dockerfile` – Multi-stage build (downloads or builds `netmuxd`; installs dependencies; sets up app).
- `app/` – FastAPI application (`main.py`, requirements).
- `docker-compose.yml` – Example orchestration with volumes and USB passthrough.

## Concepts

- Pairing must happen with a physical USB connection (trust dialog appears on device). (macOS containers cannot access USB; pair on the macOS host then copy lockdown plist into the container.)
- After pairing, backups can occur while the device is connected only via Wi-Fi (through `netmuxd` network muxing).
- Each device’s backup lives under `/data/backups/<UDID>`.
- Pairing information (lockdown plists) lives under `/data/lockdown` (symlinked to `/var/lib/lockdown` inside container).
- Weak OpenSSL config enables legacy SHA1 signatures (`OPENSSL_WEAK_CONFIG`).

## Building

Note: The Dockerfile supports a build argument `LIBIMOBILEDEVICE_PKG` to specify the correct libimobiledevice runtime package (e.g. `libimobiledevice-1.0-6` on Debian stable). The base image is now `python:3.12-slim`, so a separate virtual environment is no longer used.

```
docker build --build-arg LIBIMOBILEDEVICE_PKG=libimobiledevice-1.0-6 -t ios-backup .
```

You can also pin both the netmuxd tag and the libimobiledevice package simultaneously:

```
docker build \
  --build-arg NETMUXD_VERSION=v0.3.0 \
  --build-arg LIBIMOBILEDEVICE_PKG=libimobiledevice-1.0-6 \
  -t ios-backup:v0.3.0 .
```

Basic build (current directory is project root):

    docker build -t ios-backup .

Multi-arch build (requires Docker Buildx):

    docker buildx build --platform linux/amd64,linux/arm64 -t yourrepo/ios-backup:latest .

Pin a specific netmuxd tag (default is v0.3.0):

    docker build --build-arg NETMUXD_VERSION=v0.3.0 -t ios-backup:v0.3.0 .

## Running (Single Container)

USB pairing mode (device connected via USB at least once):

    docker run -d \
      --name ios-backup \
      -p 8080:8080 \
      -v ios_backups:/data/backups \
      -v ios_lockdown:/data/lockdown \
      -v ios_config:/data/config \
      --device /dev/bus/usb \
      --restart unless-stopped \
      ios-backup:latest

Network-only backup (already paired on host; mount host usbmuxd socket instead of USB bus):

    docker run -d \
      --name ios-backup \
      -p 8080:8080 \
      -v ios_backups:/data/backups \
      -v ios_lockdown:/data/lockdown \
      -v ios_config:/data/config \
      -v /var/run/usbmuxd:/var/run/usbmuxd \
      -e APP_MODE=backup-only \
      --restart unless-stopped \
      ios-backup:latest

## Using docker-compose

    docker compose up -d

Edit `docker-compose.yml` to adjust environment variables, volumes, or device mappings.

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| APP_MODE | `full` starts `netmuxd`; `backup-only` skips it | full |
| PORT | Web server port | 8080 |
| BACKUP_DIR | Backup root | /data/backups |
| LOCKDOWN_DIR | Lockdown plist directory | /var/lib/lockdown |
| OPENSSL_WEAK_CONFIG | Path to custom OpenSSL config | /data/config/openssl-weak.conf |
| USBMUXD_SOCKET_ADDRESS | Host:port for netmuxd socket | 127.0.0.1:27015 |
| MAX_CONCURRENT_BACKUPS | Parallel backup limit | 2 |
| NETMUXD_BIN | Override netmuxd binary name/path | netmuxd |
| NETMUXD_DISABLE_UNIX | Disable unix domain socket (-–disable-unix) | true |
| NETMUXD_HOST | netmuxd host binding | 127.0.0.1 |
| START_USBMUXD | Autostart usbmuxd inside container before launching the FastAPI app | true |
| USBMUXD_FLAGS | Flags passed to usbmuxd (default "-f" to stay foreground; script backgrounds it) | -f |
| USBMUXD_SOCKET_PATH | Path to usbmuxd UNIX domain socket (shared with netmuxd if needed) | /var/run/usbmuxd |

## API Overview

Base URL: `http://<host>:8080`

- GET `/health` – Service health + device list.
- GET `/devices` – List detected device UDIDs and pairing status.
- GET `/devices/{udid}/info` – Detailed device info (from `ideviceinfo`).
- POST `/pair` – Pair a device. Body optional `{ "udid": "<UDID>" }`; if omitted and exactly one device present, it is auto-selected.
- POST `/unpair` – Remove lockdown plist. Body `{ "udid": "<UDID>" }`.
- POST `/backup` – Start backup. Body `{ "udid": "<UDID>" }`.
- GET `/backup/jobs` – List all jobs.
- GET `/backup/{jobId}/status` – Status snapshot (running, success, error).
- GET `/backup/{jobId}/logs` – Last log lines.
- GET `/backup/{jobId}/stream` – SSE stream for real-time logs.
- GET `/backups/{udid}` – Summary (size, file count).
- GET `/backups/{udid}/files` – List all files (metadata only).
- DELETE `/backups/{udid}` – Remove a device’s backup folder.
- GET `/ui` – HTML dashboard (root `/` redirects here).

### Pairing Flow

The container entrypoint uses a startup script (`/usr/local/bin/container-start`) which, when `START_USBMUXD=true`, launches `usbmuxd` first (passing `USBMUXD_FLAGS`), then execs the Python application. If you prefer to rely on a host `usbmuxd` instead (e.g. mounting `/var/run/usbmuxd`), set `START_USBMUXD=false` to skip starting it inside the container. Adjust `USBMUXD_SOCKET_PATH` if your host uses a non-standard location.

1. Connect device via USB.
2. GET `/devices` – confirm UDID present.
3. POST `/pair` – trust prompt appears on device, accept it.
4. Confirm lockdown file persisted under `/data/lockdown`.
5. Disconnect USB; ensure device and container share network (Wi-Fi / LAN).
6. POST `/backup` with UDID.

### Backup Behavior

- Uses `idevicebackup2 backup -n --full <BACKUP_DIR>`.
- Incremental by design; subsequent runs update existing backup set.
- Server sets `OPENSSL_CONF` and `USBMUXD_SOCKET_ADDRESS` environment variables per job.
- Progress estimation is heuristic (log line count) because tool does not expose granular progress metrics.

### Multiple Devices

- Pair each separately (UDID-specific plist).
- Jobs enforce one backup at a time per device; concurrency limit overall via `MAX_CONCURRENT_BACKUPS`.

## Persistence & Volumes

Recommended named volumes:

- `ios_backups` → `/data/backups`
- `ios_lockdown` → `/data/lockdown`
- `ios_config` → `/data/config`
- (Optional) `logs` → `/data/logs`

Recreating the container will retain pairing state if volumes are preserved.

## Security Considerations

- Avoid `--privileged` unless necessary; prefer `--device /dev/bus/usb`.
- Limit network exposure of port 8080 (use reverse proxy, auth layer, or bind to internal interfaces).
- Pairing and backup APIs are unauthenticated by default; add an auth middleware (e.g. API key, JWT) for production.
- Backups may contain sensitive data (encrypted iTunes-style). Secure volume storage and enforce appropriate access controls.

## Troubleshooting

- Device not listed:
  - Confirm USB passthrough (check `docker logs ios-backup` for permission errors).
  - On host, verify `lsusb` shows Apple vendor (05ac).
- Pair fails:
  - Ensure device prompt was accepted; re-run `POST /pair`.
  - Check OpenSSL weak config exists at `OPENSSL_WEAK_CONFIG`.
- Backup stalls early:
  - Inspect SSE stream (`/backup/{jobId}/stream`) for errors.
  - Confirm network stability; large backups can take 20+ minutes.
- netmuxd crashes:
  - Recreate container or inspect logs for version incompatibilities; try pinning another `NETMUXD_VERSION`.

## Extending

- Add restore endpoint (`idevicebackup2 restore`) – requires caution (device prompts).
- Add scheduling (cron-like) inside container or external orchestrator.
- Integrate Prometheus metrics endpoint.
- Replace heuristic progress with parsed phases if format evolves.
- Provide web frontend (React / Vue) consuming the REST API.

## Updating netmuxd

Rebuild with new tag:

    docker build --build-arg NETMUXD_VERSION=v0.3.1 -t ios-backup:latest .

If release asset missing for your architecture, source build fallback is automatic (Rust toolchain installed in builder stage).

## Publishing & Pulling from GHCR (GitHub Container Registry)

The repository includes a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that will build and push multi-architecture images (amd64, arm64) to GHCR when you push to `main` or create a version tag (e.g. `v0.1.0`).

### 1. Enable GHCR for Your Account/Org
Log in once locally:
```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <your_github_username> --password-stdin
```
(or create a classic PAT with `read:packages` + `write:packages` scopes if needed).

### 2. Set Repository Visibility
If the repo is private and you want the image public, make the repository public OR adjust package visibility in GitHub’s “Packages” settings.

### 3. Tag a Release
```bash
git tag v0.1.0
git push origin v0.1.0
```
The workflow will produce:
- `ghcr.io/<owner>/ios-network-backup:v0.1.0`
- `ghcr.io/<owner>/ios-network-backup:latest` (if on main or tagged)
- `ghcr.io/<owner>/ios-network-backup:sha-<short_sha>`

### 4. Pull the Image
To run without building locally:
```bash
docker pull ghcr.io/<owner>/ios-network-backup:latest
docker run -d --name ios-backup \
  -p 8080:8080 \
  -v ios_backups:/data/backups \
  -v ios_lockdown:/data/lockdown \
  -v ios_config:/data/config \
  --device /dev/bus/usb \
  ghcr.io/<owner>/ios-network-backup:latest
```

### 5. Pin to a Specific Version
```bash
docker pull ghcr.io/<owner>/ios-network-backup:v0.1.0
docker run -d --name ios-backup ... ghcr.io/<owner>/ios-network-backup:v0.1.0
```

### 6. Overriding netmuxd Version in Workflow
Push a tag like `v0.3.1` and the workflow will attempt to use that as `NETMUXD_VERSION`. If you want a different netmuxd tag than the repo version, manually dispatch the workflow with an input version:
- GitHub UI → Actions → “Publish Multi-Arch Docker Image” → “Run workflow” → set version input (e.g. `v0.3.0`).

### 7. Local Build vs GHCR
Local:
```bash
docker build -t ios-backup:dev .
```
Remote (pull):
```bash
docker run ghcr.io/<owner>/ios-network-backup:latest
```

### 8. Cleaning Up Old Images
Use GitHub “Packages” UI or CLI:
```bash
gh api \
  -X DELETE \
  /user/packages/container/ios-network-backup/versions/<version_id>
```

### 9. SBOM & Provenance
The workflow enables SBOM and provenance (`provenance: true`, `sbom: true`). Consumers can inspect:
```bash
docker buildx imagetools inspect ghcr.io/<owner>/ios-network-backup:latest
```

Replace `<owner>` with your GitHub user or org name (lowercase).

### CI Authentication (GITHUB_TOKEN vs PAT)

For GitHub Actions builds in this repository you do **not** need a Personal Access Token (PAT). The workflow uses the built-in `GITHUB_TOKEN` with `packages: write` permission to authenticate to GHCR automatically. Use a PAT or GitHub App token only when:
- You are pushing images locally (outside Actions) to a private package.
- You need to pull a private image from another system that is not executing inside this repository’s Actions.

Public images (package set to public) can be pulled with no authentication:
```
docker pull ghcr.io/<owner>/ios-network-backup:latest
```

If you do need a PAT for local pushes:
1. Create a fine‑grained PAT with Package permissions (Read & Write) for this repo.
2. Login locally:
   ```
   echo $PAT | docker login ghcr.io -u <your_username> --password-stdin
   ```
3. Push:
   ```
   docker push ghcr.io/<owner>/ios-network-backup:<tag>
   ```

Deploy keys (SSH keys tied to a single repo) cannot be used for GHCR because they only grant Git access, not registry/package API access.


## License

This project aggregates third-party binaries (`netmuxd`, `libimobiledevice`). Review their upstream licenses separately. All original code in this repository is released under **The Unlicense** (public domain). See `LICENSE` for details.

## Disclaimer

Use at your own risk. Backups should be periodically validated. This tool does not guarantee immunity to data corruption or incomplete backup states.

---

Happy backing up!

Python Environment:
Dependencies are installed directly into the `python:3.12-slim` base image; no per-project virtual environment is created. To add or update Python packages, edit `app/requirements.txt` and rebuild the image.