"""Tests for scripts/start-jellyfin.

start-jellyfin is run as a subprocess against a temporary tree, the way the
player unit runs it. Every path it touches on a device is redirected by an
environment variable, ConfigDB is a stub HTTP server on loopback, and the
shim it execs is a stand-in that records its argv and environment, so
nothing here needs a display, a sound card or the vendored venv.
"""
import http.server
import json
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "start-jellyfin"
SHARE = REPO / "data" / "usr" / "share" / "hifiberry-jellyfin"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

PI4 = "Raspberry Pi 4 Model B Rev 1.4"
PI5 = "Raspberry Pi 5 Model B Rev 1.0"
PI3 = "Raspberry Pi 3 Model B Plus Rev 1.3"

FAKE_SHIM = """#!/bin/sh
{
  for arg in "$@"; do printf 'ARG:%s\\n' "$arg"; done
  printf 'WAYLAND_DISPLAY=%s\\n' "${WAYLAND_DISPLAY-<unset>}"
  printf 'DBUS_SESSION_BUS_ADDRESS=%s\\n' "${DBUS_SESSION_BUS_ADDRESS-<unset>}"
  printf 'XDG_CACHE_HOME=%s\\n' "${XDG_CACHE_HOME-<unset>}"
} > "$FAKE_SHIM_OUT"
"""


class ConfigDB:
    """A loopback stand-in for config-server's /api/v1/key/<key>."""

    def __init__(self, values, port=0):
        self.values = values
        self.fail_with = None
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                if outer.fail_with:
                    self.send_response(outer.fail_with)
                    self.end_headers()
                    return
                prefix = "/api/v1/key/"
                key = self.path[len(prefix):] if self.path.startswith(prefix) else None
                if key not in outer.values:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"status":"error"}')
                    return
                body = json.dumps({"status": "success",
                                   "data": {"key": key, "value": outer.values[key]}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def unused_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class Tree:
    """A fake device: model file, DRM sysfs, runtime dir, home, cache root."""

    def __init__(self, root):
        self.root = root
        self.conf_dir = root / "home" / ".config" / "jellyfin-mpv-shim"
        self.drm = root / "sys" / "class" / "drm"
        self.drm.mkdir(parents=True)
        self.model_file = root / "model"
        # A unix socket path must stay under ~104 bytes (macOS), which a
        # pytest tmp_path does not; the runtime dir gets its own short one.
        self.runtime = pathlib.Path(tempfile.mkdtemp(prefix="jf", dir="/tmp"))
        self.cache_root = root / "tmp" / "jellyfin-mpv-shim"
        self.cache_root.parent.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.shim = self.bin / "fake-shim"
        self.shim.write_text(FAKE_SHIM)
        self.shim.chmod(0o755)
        self.shim_out = root / "shim-out"
        self.volume_target = root / "share" / "scripts" / "hifiberry-volume.lua"
        self.configdb = None
        self.configdb_url = unused_port_url()
        self.listeners = []
        self.set_model(PI4)
        self.fake_command("hostnamectl", 'echo "Living Room"')
        self.fake_command("hostname", 'echo "hifiberry"')
        # Signed in unless a test says otherwise: without a credential the
        # shim is not started at all.
        self.write_users(json.loads((FIXTURES / "users.signed-in.json").read_text()))

    def write_users(self, data, name="users.json"):
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        path = self.conf_dir / name
        path.write_text(data if isinstance(data, str) else json.dumps(data, indent=4))
        path.chmod(0o600)
        return path

    def cleanup(self):
        for s in self.listeners:
            s.close()
        if self.configdb:
            self.configdb.close()
        shutil.rmtree(self.runtime, ignore_errors=True)

    def set_model(self, model):
        # The device tree's model string is NUL-terminated.
        self.model_file.write_bytes(model.encode() + b"\0")

    def fake_command(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def add_card(self, n, driver, connectors=()):
        card = self.drm / f"card{n}"
        (card / "device").mkdir(parents=True)
        (card / "device" / "uevent").write_text(f"DRIVER={driver}\nOF_NAME=gpu\n")
        for name, status in connectors:
            conn = self.drm / f"card{n}-{name}"
            conn.mkdir()
            (conn / "status").write_text(status + "\n")

    def wayland_socket(self, name="wayland-0", listening=True):
        path = self.runtime / name
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        if listening:
            s.listen(16)
            self.listeners.append(s)
        else:
            s.close()  # leaves a stale socket file behind, as a dead cage would
        return path

    def set_configdb(self, values):
        self.configdb = ConfigDB(values)
        self.configdb_url = self.configdb.url

    def env(self):
        return {
            "PATH": str(self.bin),
            "HOME": str(self.root / "home"),
            "JELLYFIN_CONF_DIR": str(self.conf_dir),
            "JELLYFIN_DRM_ROOT": str(self.drm),
            "JELLYFIN_MODEL_FILE": str(self.model_file),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "JELLYFIN_CONFIG_SERVER": self.configdb_url,
            "JELLYFIN_CACHE_ROOT": str(self.cache_root),
            "JELLYFIN_SHARE_DIR": str(SHARE),
            "JELLYFIN_VOLUME_SCRIPT": str(self.volume_target),
            "JELLYFIN_SHIM": str(self.shim),
            "FAKE_SHIM_OUT": str(self.shim_out),
            # No retrying against an unreachable ConfigDB unless a test asks.
            "JELLYFIN_CONFIGDB_WAIT": "0",
        }

    def run(self, *args, extra_env=None):
        env = self.env()
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def plan(self):
        result = self.run("--dry-run")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def start(self):
        result = self.run()
        assert result.returncode == 0, result.stderr
        return result

    def shim_record(self):
        return self.shim_out.read_text().splitlines()

    def shim_argv(self):
        return [l[4:] for l in self.shim_record() if l.startswith("ARG:")]

    def mpv_conf(self):
        return (self.conf_dir / "mpv.conf").read_text()

    def conf_json(self):
        return json.loads((self.conf_dir / "conf.json").read_text())


@pytest.fixture
def tree(tmp_path):
    t = Tree(tmp_path)
    yield t
    t.cleanup()


def mpv_options(text):
    """mpv.conf as {option: value}, comments and blank lines dropped."""
    opts = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        opts[key] = value
    return opts


# --- template selection -----------------------------------------------------

def test_template_wayland_when_cage_socket_present(tree):
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.wayland_socket("wayland-0")

    plan = tree.plan()
    assert plan["template"] == "wayland"
    assert plan["env"]["WAYLAND_DISPLAY"] == "wayland-0"

    tree.start()
    opts = mpv_options(tree.mpv_conf())
    assert opts["vo"] == "gpu"
    assert opts["gpu-context"] == "wayland"
    assert opts["fullscreen"] == "yes"
    assert "drm-device" not in opts
    assert "WAYLAND_DISPLAY=wayland-0" in tree.shim_record()


def test_stale_wayland_socket_is_not_kiosk_mode(tree):
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.wayland_socket("wayland-0", listening=False)
    (tree.runtime / "wayland-0.lock").write_text("")

    assert tree.plan()["template"] == "drm"


def test_wayland_display_not_inherited_without_cage(tree):
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "disconnected")])
    result = tree.run(extra_env={"WAYLAND_DISPLAY": "wayland-9"})
    assert result.returncode == 0, result.stderr
    assert "WAYLAND_DISPLAY=<unset>" in tree.shim_record()


def test_template_drm_when_hdmi_connected(tree):
    # card0 is the render-only v3d; the display controller must be found by
    # driver, not assumed to be card0 or card1.
    tree.add_card(0, "v3d")
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected"), ("HDMI-A-2", "disconnected")])

    assert tree.plan()["template"] == "drm"

    tree.start()
    opts = mpv_options(tree.mpv_conf())
    assert opts["vo"] == "gpu"
    assert opts["gpu-context"] == "drm"
    assert opts["drm-device"] == "/dev/dri/card1"


def test_template_drm_finds_card_by_driver(tree):
    tree.add_card(0, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.add_card(1, "v3d")

    tree.start()
    assert mpv_options(tree.mpv_conf())["drm-device"] == "/dev/dri/card0"


def test_template_audio_only_without_display(tree):
    tree.add_card(0, "v3d")
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "disconnected"), ("HDMI-A-2", "disconnected")])

    assert tree.plan()["template"] == "audio"

    tree.start()
    opts = mpv_options(tree.mpv_conf())
    assert opts["vo"] == "null"
    assert opts["vid"] == "no"
    assert "drm-device" not in opts


def test_template_audio_only_without_drm_at_all(tree):
    assert tree.plan()["template"] == "audio"


def test_all_templates_use_pipewire_and_fast_profile(tree):
    for name in ("drm", "wayland", "audio"):
        opts = mpv_options((SHARE / f"mpv.conf.{name}").read_text())
        assert opts["ao"] == "pipewire", name
        assert opts["profile"] == "fast", name


def test_mpv_conf_replaced_on_every_start(tree):
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.conf_dir.mkdir(parents=True, exist_ok=True)
    (tree.conf_dir / "mpv.conf").write_text("vo=x11\nmy-own-option=1\n")

    tree.start()
    opts = mpv_options(tree.mpv_conf())
    assert opts["vo"] == "gpu"
    assert "my-own-option" not in opts


# --- hardware decoding by model ---------------------------------------------

def test_hwdec_pi4(tree):
    tree.set_model(PI4)
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.start()
    assert mpv_options(tree.mpv_conf())["hwdec"] == "drm-copy,v4l2m2m-copy"


def test_hwdec_pi5(tree):
    tree.set_model(PI5)
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.start()
    assert mpv_options(tree.mpv_conf())["hwdec"] == "drm-copy"


@pytest.mark.parametrize("model", ["Raspberry Pi 400 Rev 1.0",
                                   "Raspberry Pi Compute Module 4 Rev 1.0"])
def test_pi4_family(tree, model):
    tree.set_model(model)
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    plan = tree.plan()
    assert plan["template"] == "drm"
    assert plan["hwdec"] == "drm-copy,v4l2m2m-copy"


@pytest.mark.parametrize("model", [PI3, "Raspberry Pi Zero 2 W Rev 1.0", ""])
def test_other_models_are_audio_only(tree, model):
    tree.set_model(model)
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.wayland_socket("wayland-0")
    plan = tree.plan()
    assert plan["template"] == "audio"
    assert plan["hwdec"] is None


def test_missing_model_file_is_audio_only(tree):
    tree.model_file.unlink()
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    assert tree.plan()["template"] == "audio"


# --- conf.json --------------------------------------------------------------

OWNED = {
    "enable_gui": False,
    "headless": True,
    "close_to_tray": False,
    "media_keys": False,
    "connect_retry_mins": 1440,
    "player_name": "Living Room",
}


def write_conf(tree, data):
    tree.conf_dir.mkdir(parents=True, exist_ok=True)
    (tree.conf_dir / "conf.json").write_text(json.dumps(data, indent=4))


def test_conf_json_owned_keys(tree):
    write_conf(tree, {"enable_gui": True, "headless": False, "close_to_tray": True,
                      "media_keys": True, "player_name": "old", "fullscreen": False,
                      "local_kbps": 50000, "remote_kbps": 10000, "volume": 42})
    tree.start()
    conf = tree.conf_json()
    for key, value in OWNED.items():
        assert conf[key] == value, key
    # The shim's own keys are left alone.
    assert conf["fullscreen"] is False
    assert conf["volume"] == 42
    assert conf["local_kbps"] == 50000
    assert conf["remote_kbps"] == 10000


def test_conf_json_created_on_fresh_install(tree):
    tree.start()
    assert tree.conf_json() == OWNED


def test_unlimited_remote_on_copies_local_kbps(tree):
    tree.set_configdb({"player.jellyfin.unlimited_remote": "true"})
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 10000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 50000
    assert "/api/v1/key/player.jellyfin.unlimited_remote" in tree.configdb.requests


def test_unlimited_remote_on_fresh_install_uses_shim_default(tree):
    tree.set_configdb({"player.jellyfin.unlimited_remote": "true"})
    tree.start()
    conf = tree.conf_json()
    assert conf["remote_kbps"] == 2147483


def test_unlimited_remote_off_keeps_remote_cap(tree):
    tree.set_configdb({"player.jellyfin.unlimited_remote": "false"})
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 10000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 10000


def test_unlimited_remote_turned_off_restores_cap(tree):
    # Turned on earlier (remote_kbps was copied from local_kbps), now off.
    tree.set_configdb({"player.jellyfin.unlimited_remote": "false"})
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 50000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 10000


def test_unlimited_remote_unset_is_off(tree):
    tree.set_configdb({})
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 50000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 10000


def test_unlimited_remote_configdb_unreachable_keeps_unlimited(tree):
    # tree.configdb_url points at a port nothing listens on: the setting is
    # unknown, which must not undo "on" (remote_kbps == local_kbps) on a boot
    # where the player starts before config-server.
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 50000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 50000


def test_unlimited_remote_configdb_unreachable_keeps_cap(tree):
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 10000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 10000


def test_unlimited_remote_configdb_unreachable_fresh_install(tree):
    tree.start()
    assert "remote_kbps" not in tree.conf_json()


def test_unlimited_remote_configdb_server_error_keeps_value(tree):
    tree.set_configdb({"player.jellyfin.unlimited_remote": "true"})
    tree.configdb.fail_with = 500
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 50000})
    tree.start()
    assert tree.conf_json()["remote_kbps"] == 50000


def test_unlimited_remote_waits_for_configdb(tree):
    # config-server comes up a moment after the player starts.
    port = int(tree.configdb_url.rsplit(":", 1)[1])
    timer = threading.Timer(1.0, lambda: setattr(
        tree, "configdb",
        ConfigDB({"player.jellyfin.unlimited_remote": "true"}, port=port)))
    timer.start()
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 10000})
    result = tree.run(extra_env={"JELLYFIN_CONFIGDB_WAIT": "10"})
    timer.join()
    assert result.returncode == 0, result.stderr
    assert tree.conf_json()["remote_kbps"] == 50000


def test_dry_run_reports_conf_keys(tree):
    tree.set_configdb({"player.jellyfin.unlimited_remote": "true"})
    write_conf(tree, {"local_kbps": 50000, "remote_kbps": 10000})
    plan = tree.plan()
    assert plan["conf"] == dict(OWNED, remote_kbps=50000)


def test_corrupt_conf_json_is_set_aside(tree):
    tree.conf_dir.mkdir(parents=True, exist_ok=True)
    (tree.conf_dir / "conf.json").write_text("{not json")
    tree.start()
    assert tree.conf_json() == OWNED
    assert (tree.conf_dir / "conf.json.corrupt").read_text() == "{not json"


# --- player name ------------------------------------------------------------

def test_player_name_falls_back_to_hostname(tree):
    tree.fake_command("hostnamectl", "exit 1")
    tree.start()
    assert tree.conf_json()["player_name"] == "hifiberry"


def test_player_name_falls_back_to_hifiberry(tree):
    tree.fake_command("hostnamectl", "echo")
    tree.fake_command("hostname", "exit 1")
    tree.start()
    assert tree.conf_json()["player_name"] == "HiFiBerry"


# --- files around the shim --------------------------------------------------

def test_cache_dirs_created_0755(tree):
    tree.start()
    for path in (tree.cache_root, tree.cache_root / ".cache"):
        assert path.is_dir(), path
        assert stat.S_IMODE(path.stat().st_mode) == 0o755, path


def test_cache_dirs_permissions_repaired(tree):
    (tree.cache_root / ".cache").mkdir(parents=True)
    tree.cache_root.chmod(0o700)
    (tree.cache_root / ".cache").chmod(0o700)
    tree.start()
    for path in (tree.cache_root, tree.cache_root / ".cache"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755, path


def test_cache_root_symlink_refused(tree):
    elsewhere = tree.root / "elsewhere"
    elsewhere.mkdir()
    tree.cache_root.symlink_to(elsewhere)
    result = tree.run()
    assert result.returncode != 0
    assert not tree.shim_out.exists()


def test_volume_script_symlinked(tree):
    tree.start()
    link = tree.conf_dir / "scripts" / "hifiberry-volume.lua"
    assert link.is_symlink()
    assert os.readlink(link) == str(tree.volume_target)


def test_volume_script_link_target_is_packaged_script(tree):
    # Without JELLYFIN_VOLUME_SCRIPT the link goes to scripts/ under the
    # share directory: on a device /usr/share/hifiberry-jellyfin, here the
    # same layout in the repo's data/ tree. It must point at a real script.
    env = tree.env()
    del env["JELLYFIN_VOLUME_SCRIPT"]
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    link = tree.conf_dir / "scripts" / "hifiberry-volume.lua"
    assert os.readlink(link) == str(SHARE / "scripts" / "hifiberry-volume.lua")
    assert SHARE == REPO / "data" / "usr" / "share" / "hifiberry-jellyfin"
    target = link.resolve()
    assert target.is_file()
    script = target.read_text()
    assert "/api/v1/soundcard/detect" in script
    assert "volume-gain" in script


def test_volume_script_symlink_replaces_stale_entry(tree):
    scripts = tree.conf_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "hifiberry-volume.lua").write_text("-- an old copy\n")
    (scripts / "other.lua").write_text("-- not ours\n")
    tree.start()
    link = scripts / "hifiberry-volume.lua"
    assert link.is_symlink()
    assert os.readlink(link) == str(tree.volume_target)
    assert (scripts / "other.lua").read_text() == "-- not ours\n"


def test_users_json_untouched(tree):
    users = tree.conf_dir / "users.json"
    content = users.read_bytes()
    os.utime(users, (1_000_000_000, 1_000_000_000))
    tree.start()
    assert users.read_bytes() == content
    assert users.stat().st_mtime == 1_000_000_000
    assert tree.shim_argv() == ["--no-gui"]


# --- not signed in: no shim -------------------------------------------------
#
# Without a credential that connects, jellyfin-mpv-shim falls back to asking
# for a server URL on stdin. Under systemd stdin is /dev/null, so it dies of
# EOFError and Restart=on-failure starts it again, for ever. start-jellyfin
# therefore does not start it at all without a credential.

def fresh_users():
    return json.loads((FIXTURES / "users.fresh.json").read_text())


def assert_not_started(tree, result):
    assert result.returncode == 0, result.stderr
    assert not tree.shim_out.exists()
    lines = [l for l in result.stderr.splitlines() if l.strip()]
    assert len(lines) == 1, result.stderr
    assert "not signed in" in lines[0]
    # Nothing is prepared for a player that does not run.
    assert not (tree.conf_dir / "conf.json").exists()
    assert not (tree.conf_dir / "mpv.conf").exists()
    assert not tree.cache_root.exists()


def test_no_credential_does_not_start_shim(tree):
    users = tree.write_users(fresh_users())
    before = users.read_bytes()
    os.utime(users, (1_000_000_000, 1_000_000_000))
    assert_not_started(tree, tree.run())
    assert users.read_bytes() == before
    assert users.stat().st_mtime == 1_000_000_000


def test_no_users_json_does_not_start_shim(tree):
    (tree.conf_dir / "users.json").unlink()
    assert_not_started(tree, tree.run())
    assert not (tree.conf_dir / "users.json").exists()


def test_corrupt_users_json_does_not_start_shim(tree):
    tree.write_users("{not json")
    assert_not_started(tree, tree.run())
    assert (tree.conf_dir / "users.json").read_text() == "{not json"


def test_credential_without_token_does_not_start_shim(tree):
    users = json.loads((FIXTURES / "users.signed-in.json").read_text())
    del users["users"][0]["credentials"][0]["AccessToken"]
    tree.write_users(users)
    assert_not_started(tree, tree.run())


def test_credential_of_another_profile_only_does_not_start_shim(tree):
    # The shim connects with the active profile's credentials only.
    users = json.loads((FIXTURES / "users.signed-in.json").read_text())
    other = dict(fresh_users()["users"][0], id="00000000-0000-4000-8000-000000000002",
                 default=False)
    users["users"].append(other)
    users["active"] = other["id"]
    tree.write_users(users)
    assert_not_started(tree, tree.run())


def test_dangling_active_pointer_uses_first_profile(tree):
    users = json.loads((FIXTURES / "users.signed-in.json").read_text())
    users["active"] = "00000000-0000-4000-8000-0000000000ff"
    tree.write_users(users)
    tree.start()
    assert tree.shim_argv() == ["--no-gui"]


def test_credential_only_in_shim_backup_starts_shim(tree):
    # The shim restores users.json from its .bak when the file is gone or
    # damaged, so that credential is one it will connect with.
    signed_in = (tree.conf_dir / "users.json").read_text()
    (tree.conf_dir / "users.json").unlink()
    tree.write_users(signed_in, name="users.json.bak")
    tree.start()
    assert tree.shim_argv() == ["--no-gui"]


def test_backup_ignored_when_users_json_is_usable(tree):
    signed_in = (tree.conf_dir / "users.json").read_text()
    tree.write_users(fresh_users())
    tree.write_users(signed_in, name="users.json.bak")
    assert_not_started(tree, tree.run())


def test_dry_run_reports_sign_in(tree):
    assert tree.plan()["signed_in"] is True
    tree.write_users(fresh_users())
    assert tree.plan()["signed_in"] is False


def test_conf_json_retries_unreachable_server(tree):
    # connect_retry_mins 0 (the shim's default) makes a server that is not
    # reachable yet -- the usual case at boot -- fall through to the stdin
    # prompt; with it set the shim keeps retrying instead.
    write_conf(tree, {"connect_retry_mins": 0})
    tree.start()
    assert tree.conf_json()["connect_retry_mins"] == 1440
    assert tree.plan()["conf"]["connect_retry_mins"] == 1440


# --- exec -------------------------------------------------------------------

def test_execs_shim_without_gui(tree):
    tree.start()
    assert tree.shim_argv() == ["--no-gui"]


def test_unit_environment_reaches_shim(tree):
    bus = "unix:path=/run/dbus/system_bus_socket"
    cache = "/tmp/jellyfin-mpv-shim/.cache"
    result = tree.run(extra_env={"DBUS_SESSION_BUS_ADDRESS": bus,
                                 "XDG_CACHE_HOME": cache})
    assert result.returncode == 0, result.stderr
    record = tree.shim_record()
    assert f"DBUS_SESSION_BUS_ADDRESS={bus}" in record
    assert f"XDG_CACHE_HOME={cache}" in record


def test_dry_run_writes_nothing(tree):
    tree.add_card(1, "vc4-drm", [("HDMI-A-1", "connected")])
    tree.plan()
    assert sorted(p.name for p in tree.conf_dir.iterdir()) == ["users.json"]
    assert not tree.cache_root.exists()
    assert not tree.shim_out.exists()
