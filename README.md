# hifiberry-jellyfin

Makes a HiFiBerryOS device a Jellyfin cast target: cast music, movies and
series to it from any Jellyfin app. It runs
[jellyfin-mpv-shim](https://github.com/jellyfin/jellyfin-mpv-shim), a headless
player that plays through libmpv, as the systemd `--user` unit
`jellyfin.service` of the HiFiBerryOS player user (`/etc/hifiberry.user`).
Audio goes through PipeWire like every other source; video plays full-screen on
HDMI when a display is connected.

## Layout

| Path | Purpose |
| --- | --- |
| `/usr/lib/hifiberry-jellyfin/venv/` | jellyfin-mpv-shim and its Python dependencies (`requirements.txt`). |
| `/usr/bin/start-jellyfin` | Prepares the shim's configuration, then execs it with `--no-gui`. |
| `/usr/share/hifiberry-jellyfin/mpv.conf.{drm,wayland,audio}` | mpv templates. |
| `/usr/lib/systemd/user/jellyfin.service` | The player. Ships disabled; toggled from Services > Players. |
| `/usr/bin/jellyfin-setup` | Sign-in and server discovery for the WebUI, on 127.0.0.1:3551. |
| `/usr/lib/systemd/user/jellyfin-setup.service` | Runs `jellyfin-setup`. Ships enabled. |

## The shim is vendored

jellyfin-mpv-shim is not in Debian. `debian/rules` installs it and its
dependencies from PyPI into a venv at package build time, pinned by version and
hash in `requirements.txt`, so nothing is downloaded on the device. Only
pure-Python wheels are allowed, which keeps the package `Architecture: all`;
the build fails if a compiled module lands in the venv. The venv is created
with `--system-site-packages` and has no pip of its own: `gi` (MPRIS) and
`PIL` come from Debian's `python3-gi` and `python3-pil`.

The venv belongs to the Python minor version it was built with
(`lib/python3.X`), so the package depends on exactly that version.

To move to a new shim release, resolve its dependencies again and update the
versions and hashes in `requirements.txt`, keeping to `py3-none-any` wheels.

## What `start-jellyfin` does on every start

All of it lives in `~/.config/jellyfin-mpv-shim/` of the player user.

- **`mpv.conf`** is ours and is replaced on each start, from one of three
  templates:
  - `wayland` when kiosk mode's compositor answers on a socket in
    `$XDG_RUNTIME_DIR` (`wayland-N`): mpv becomes a full-screen client of it,
    and kiosk mode keeps running. `WAYLAND_DISPLAY` is exported for the shim.
  - `drm` when the `vc4-drm` display controller has a connected HDMI port:
    mpv drives the display directly, with `drm-device=/dev/dri/cardN` for
    that card (found by driver; `v3d` is render-only).
  - `audio` otherwise (`vo=null`, `vid=no`): a cast still plays its sound.

  Hardware decoding follows the model in `/proc/device-tree/model`: Pi 4
  family `hwdec=drm-copy,v4l2m2m-copy`, Pi 5 family `hwdec=drm-copy`. Any
  other model (Pi 3, Zero 2) always gets the `audio` template, because
  software video there is too slow to be worth showing.

  The display is checked when the player starts, not while it runs: plugging
  in a screen or starting kiosk mode takes effect at the next player restart.
- **`conf.json`**: `enable_gui=false`, `headless=true`, `close_to_tray=false`,
  `media_keys=false`, and `player_name` = the device name
  (`hostnamectl hostname --pretty`, else `hostname`, else `HiFiBerry`). Every
  other key is the shim's own and is kept. The shim writes settings back to
  this file while it runs, so it is only edited before the exec. A file that
  is not valid JSON is moved to `conf.json.corrupt`.
- **Full quality over VPN.** The shim counts a server as local only at an
  RFC 1918 address; anything else, a VPN address included, is capped at
  `remote_kbps` (10 Mbit/s) and gets transcoded. When ConfigDB's
  `player.jellyfin.unlimited_remote` is `true`, `remote_kbps` is set to
  `local_kbps`. When it is off (`false`, or never set) a `remote_kbps` equal
  to `local_kbps` (what turning it on leaves behind) is put back to 10000;
  any other value is left alone. When config-server does not answer -- the
  player can start before it on boot -- `start-jellyfin` retries for up to
  10 s and then leaves `remote_kbps` exactly as it is, so the last known
  setting stays in force rather than being read as off.
- **`scripts/hifiberry-volume.lua`** is a symlink to
  `/usr/share/hifiberry-jellyfin/scripts/hifiberry-volume.lua`.
- **`users.json`** holds the server token and belongs to the setup service;
  `start-jellyfin` never reads, creates or writes it.
- **Artwork**: `jellyfin.service` sets
  `XDG_CACHE_HOME=/tmp/jellyfin-mpv-shim/.cache`, and `start-jellyfin` creates
  `/tmp/jellyfin-mpv-shim` and its `.cache` with mode 0755 first. audiocontrol
  runs as its own user and reads the covers from there; a cache base the shim
  creates itself is 0700. It refuses to start if something other than a
  directory of the player user's is at that path.

`start-jellyfin --dry-run` prints the template, hwdec, display card,
`WAYLAND_DISPLAY`, the conf.json keys it would set and the resulting
`mpv.conf` as JSON, and changes nothing.

## `jellyfin-setup`: signing in

The player card in the WebUI signs the device in to a Jellyfin server with
Quick Connect, so nobody has to type a password on the device. nginx forwards
`/api/jellyfin/*` to `jellyfin-setup`, which listens on 127.0.0.1:3551 only:

| Request | Answer |
| --- | --- |
| `GET /status` | `logged_in`, `is_active` (is `jellyfin.service` running), `device_name`, `version`, and `sign_in`: `{method: "quick_connect", account, server_name, ready, reason}`. `logged_in`, `account` and `server_name` describe the credential for the server chosen now (same address, or same server id); one for another server does not count. `ready` is false, with `reason` "Choose a server first.", while no server is set. |
| `GET /servers` | Servers found on the LAN, `[{id, name, address}]`. config-server uses it for the server drop-down. |
| `POST /login` | Starts Quick Connect: `{code, expires_in: 300}`. 409 when no server is set or Quick Connect is disabled on the server, 502 when the server does not answer. |
| `GET /login` | `{state, code, error}`, `state` one of `idle`, `waiting`, `approved`, `expired`, `failed`. Each poll asks the server whether the code was entered; once it was, the credentials are stored. |
| `DELETE /login` | Cancels the pending code. |
| `POST /logout` | Revokes the token on the server (best effort) and removes the stored credentials. |

- **Which server**: ConfigDB's `player.jellyfin.server_url` (typed in by hand)
  when it is set, otherwise `player.jellyfin.server` (picked from the
  drop-down), read on every request. A bare host gets `http://` and port 8096.
- **Discovery**: the Jellyfin probe `who is JellyfinServer?` broadcast to UDP
  port 7359, answers collected for 2 s, at start and every 60 s in the
  background. `/servers` answers from that list at once and never waits for
  a scan. A server stays listed for 5 minutes after its last answer.
- **Credentials** go into the shim's `users.json` in the shape the shim's own
  `--quick-connect` writes, replacing any earlier entry for the same server.
  Jellyfin ties a token to the device id it was issued for, so every request
  to the server carries `users.json`'s `device_id`; when there is no
  `users.json` yet, a new device id is chosen and the file is created together
  with the credential. Starting a sign-in writes nothing.
- **The player is stopped while `users.json` changes.** The shim keeps its
  users in memory and writes `users.json` itself, so a file rewritten under a
  running shim can be overwritten again: a new sign-in lost, a removed one
  back. After signing in or out, `jellyfin-setup` checks whether
  `jellyfin.service` is running; if so it stops it (waiting up to 30 s),
  writes the file, and starts it again. A player that is not running is left
  alone. A player that fails to stop gets the file anyway, then a restart.
- **Secrets**: the Quick Connect secret and the access token are never
  logged, never returned by any request and never passed on a command line;
  `users.json` is mode 0600 in a 0700 directory.

## Tests

```sh
python3 -m pytest tests/ -v
```

`tests/test_jellyfin_setup.py` runs `jellyfin-setup` in-process against
loopback stubs of ConfigDB and a Jellyfin server, with a stand-in for
`systemctl` (`JELLYFIN_SYSTEMCTL_CMD`).
`tests/test_start_jellyfin.py` runs `start-jellyfin` against a temporary tree;
every device path is redirected through the environment (`JELLYFIN_CONF_DIR`,
`JELLYFIN_DRM_ROOT`, `JELLYFIN_MODEL_FILE`, `XDG_RUNTIME_DIR`,
`JELLYFIN_CONFIG_SERVER`, `JELLYFIN_CONFIGDB_WAIT`, `JELLYFIN_CACHE_ROOT`, `JELLYFIN_SHARE_DIR`,
`JELLYFIN_VOLUME_SCRIPT`, `JELLYFIN_SHIM`). The package build runs the same
suite.

## Building

On a Debian host, `./build.sh`. On any other host, build in the HiFiBerryOS
builder container (see `packages/build.md` in the hifiberry-os repository).
The build needs network access to PyPI; the device does not.
