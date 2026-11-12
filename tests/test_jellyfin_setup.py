"""Tests for scripts/jellyfin-setup.

The service runs in-process on a loopback port, so the clock and the
discovery scan can be injected. The Jellyfin server and config-server are
loopback stubs, and systemctl is a stand-in script that records its
arguments. One test runs the script itself as a subprocess, the way the unit
does.
"""
import copy
import http.server
import importlib.machinery
import importlib.util
import json
import logging
import os
import pathlib
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "jellyfin-setup"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

SECRET = "QC-SECRET-5a1c0ffee5a1c0ffee5a1c0ffee"
TOKEN = "FAKE-TOKEN-1111111111111111111111"
CODE = "314359"
SERVER_ID = "0123456789abcdef0123456789abcdef"
USER_ID = "fedcba9876543210fedcba9876543210"


def load_module():
    loader = importlib.machinery.SourceFileLoader("jellyfin_setup", str(SCRIPT))
    spec = importlib.util.spec_from_loader("jellyfin_setup", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def js():
    return load_module()


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text())


class Stub:
    """A loopback HTTP server whose routes are plain functions."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlsplit(self.path)
                outer.requests.append({
                    "method": self.command,
                    "path": parsed.path,
                    "query": urllib.parse.parse_qs(parsed.query),
                    "headers": dict(self.headers),
                    "body": body,
                })
                route = outer.routes.get((self.command, parsed.path))
                if route is None:
                    status, payload = 404, None
                else:
                    status, payload = route(outer.requests[-1])
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def calls(self, method, path):
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeJellyfin(Stub):
    def __init__(self):
        self.initiate_status = 200
        self.authenticated = False
        super().__init__({
            ("GET", "/System/Info/Public"): lambda r: (200, {
                "LocalAddress": "http://192.0.2.10:8096",
                "ServerName": "Home",
                "Version": "12.4.0",
                "ProductName": "Jellyfin Server",
                "Id": SERVER_ID,
                "StartupWizardCompleted": True,
            }),
            ("POST", "/QuickConnect/Initiate"): self._initiate,
            ("GET", "/QuickConnect/Connect"): self._connect,
            ("POST", "/Users/AuthenticateWithQuickConnect"): self._authenticate,
            ("POST", "/Sessions/Logout"): lambda r: (204, None),
        })

    def _initiate(self, request):
        if self.initiate_status != 200:
            return self.initiate_status, None
        return 200, {"Authenticated": False, "Secret": SECRET, "Code": CODE,
                     "DeviceId": "x", "DeviceName": "x", "AppName": "HiFiBerryOS",
                     "AppVersion": "0", "DateAdded": "2026-11-12T12:00:00Z"}

    def _connect(self, request):
        if request["query"].get("secret") != [SECRET]:
            return 404, None
        return 200, {"Authenticated": self.authenticated, "Secret": SECRET, "Code": CODE}

    def _authenticate(self, request):
        if json.loads(request["body"] or b"{}").get("Secret") != SECRET:
            return 400, None
        return 200, {"AccessToken": TOKEN, "ServerId": SERVER_ID,
                     "User": {"Id": USER_ID, "Name": "alice", "ServerId": SERVER_ID},
                     "SessionInfo": {"Id": "s1"}}


class ConfigDB(Stub):
    def __init__(self, values):
        self.values = values
        super().__init__({})
        self.routes = _KeyRoutes(self)


class _KeyRoutes:
    """Answers GET /api/v1/key/<key> from ConfigDB.values."""

    def __init__(self, db):
        self.db = db

    def get(self, route):
        method, path = route
        prefix = "/api/v1/key/"
        if method != "GET" or not path.startswith(prefix):
            return None
        key = path[len(prefix):]

        def answer(request):
            if key not in self.db.values:
                return 404, {"status": "error", "message": "not found"}
            return 200, {"status": "success",
                         "data": {"key": key, "value": self.db.values[key]}}
        return answer


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# Logs its arguments; on stop/start/restart it also keeps a copy of
# users.json as it is at that moment ("<verb>.json", or "<verb>.missing").
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$2" in
  is-active) exit "$(cat "$FAKE_SYSTEMCTL_ACTIVE_RC")" ;;
  stop|start|restart)
    cp "$FAKE_USERS_JSON" "$FAKE_SNAPSHOTS/$2.json" 2>/dev/null || : > "$FAKE_SNAPSHOTS/$2.missing"
    [ "$2" = stop ] && exit "$(cat "$FAKE_SYSTEMCTL_STOP_RC")" ;;
esac
exit 0
"""

IS_ACTIVE = "--user is-active --quiet jellyfin.service"
STOP = "--user stop jellyfin.service"
START = "--user start jellyfin.service"
RESTART = "--user restart jellyfin.service"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Env:
    """Everything one service instance needs, and the service itself."""

    def __init__(self, js, tmp_path, monkeypatch, *, users=None, active=True,
                 server=None, server_url=None, scan=None):
        self.js = js
        self.conf_dir = tmp_path / "home" / ".config" / "jellyfin-mpv-shim"
        self.users_path = self.conf_dir / "users.json"
        if users is not None:
            self.conf_dir.mkdir(parents=True)
            self.users_path.write_text(json.dumps(users))
            self.users_path.chmod(0o600)

        self.jellyfin = FakeJellyfin()
        values = {}
        if server is not None:
            values["player.jellyfin.server"] = server
        if server_url is not None:
            values["player.jellyfin.server_url"] = server_url
        self.configdb = ConfigDB(values)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.systemctl = bin_dir / "systemctl"
        self.systemctl.write_text(FAKE_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.systemctl_log = tmp_path / "systemctl.log"
        self.systemctl_log.write_text("")
        self.active_rc = tmp_path / "active-rc"
        self.set_active(active)
        monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(self.systemctl_log))
        monkeypatch.setenv("FAKE_SYSTEMCTL_ACTIVE_RC", str(self.active_rc))
        self.stop_rc = tmp_path / "stop-rc"
        self.stop_rc.write_text("0")
        monkeypatch.setenv("FAKE_SYSTEMCTL_STOP_RC", str(self.stop_rc))
        self.snapshots = tmp_path / "snapshots"
        self.snapshots.mkdir()
        monkeypatch.setenv("FAKE_SNAPSHOTS", str(self.snapshots))
        monkeypatch.setenv("FAKE_USERS_JSON", str(self.users_path))

        self.clock = Clock()
        self.service = js.SetupService(
            conf_dir=str(self.conf_dir),
            config_server=self.configdb.url,
            systemctl_cmd=f"{self.systemctl} --user",
            device_name=lambda: "Living Room",
            clock=self.clock,
            scan=scan or (lambda: []),
        )
        self.httpd = js.make_server(self.service, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.bodies = []

    def set_active(self, active):
        self.active_rc.write_text("0" if active else "3")

    def systemctl_calls(self):
        return self.systemctl_log.read_text().splitlines()

    def snapshot(self, verb):
        """users.json as it was when systemctl <verb> ran (None: missing)."""
        path = self.snapshots / f"{verb}.json"
        if path.exists():
            return json.loads(path.read_text())
        assert (self.snapshots / f"{verb}.missing").exists(), f"no systemctl {verb}"
        return None

    def call(self, method, path, timeout=10):
        request = urllib.request.Request(self.url + path, method=method,
                                         data=b"" if method in ("POST",) else None)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        self.bodies.append(raw.decode())
        return status, json.loads(raw) if raw else None

    def users(self):
        return json.loads(self.users_path.read_text())

    def close(self):
        self.service.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.jellyfin.close()
        self.configdb.close()


@pytest.fixture
def make_env(js, tmp_path, monkeypatch):
    envs = []

    def make(**kwargs):
        env = Env(js, tmp_path, monkeypatch, **kwargs)
        envs.append(env)
        return env

    yield make
    for env in envs:
        env.close()


def jf_server(env):
    return env.jellyfin.url


# --- status -----------------------------------------------------------------

def test_status_not_signed_in_ready(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    status, body = env.call("GET", "/status")
    assert status == 200
    assert body["binary_installed"] is True
    assert body["logged_in"] is False
    assert body["bridge_connected"] is True
    assert body["is_active"] is True
    assert body["device_name"] == "Living Room"
    assert body["build_date"] is None and body["expires_on"] is None
    assert isinstance(body["version"], str) and body["version"]
    assert body["sign_in"] == {"method": "quick_connect", "account": None,
                               "server_name": None, "ready": True, "reason": None,
                               "other_server": False}


def test_status_no_server_not_ready(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), active=False)
    status, body = env.call("GET", "/status")
    assert status == 200
    assert body["logged_in"] is False
    assert body["is_active"] is False
    assert body["sign_in"]["ready"] is False
    assert body["sign_in"]["reason"] == "Choose a server first."


def test_status_empty_server_values_not_ready(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), server="", server_url="  ")
    _, body = env.call("GET", "/status")
    assert body["sign_in"]["ready"] is False


def test_status_without_users_json(make_env):
    env = make_env()
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    status, body = env.call("GET", "/status")
    assert status == 200
    assert body["logged_in"] is False
    assert body["sign_in"]["ready"] is True


def test_status_signed_in(make_env):
    users = fixture_json("users.signed-in.json")
    env = make_env(users=users)
    users["users"][0]["credentials"][0]["address"] = env.jellyfin.url
    users["users"][0]["credentials"][0]["Name"] = "Old name"
    env.users_path.write_text(json.dumps(users))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    status, body = env.call("GET", "/status")
    assert status == 200
    assert body["logged_in"] is True
    assert body["sign_in"]["account"] == "alice"
    # Live from the server's /System/Info/Public, not the stored copy.
    assert body["sign_in"]["server_name"] == "Home"
    assert body["sign_in"]["other_server"] is False
    assert "FAKE-TOKEN" not in json.dumps(body)


def test_status_credential_for_other_server_is_not_signed_in(make_env):
    users = fixture_json("users.signed-in.json")  # server A at 192.0.2.10
    users["users"][0]["credentials"][0]["Id"] = "a" * 32
    users["users"][0]["credentials"][0]["Name"] = "Cabin"
    env = make_env(users=users)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url  # server B
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is False
    # The shim still connects to A with it, so the panel has to say so and
    # offer to sign out.
    assert body["sign_in"]["other_server"] is True
    assert body["sign_in"]["account"] == "alice"
    assert body["sign_in"]["server_name"] == "Cabin"
    assert body["sign_in"]["ready"] is True
    assert "FAKE-TOKEN" not in json.dumps(body)


def test_status_credential_matches_by_server_id(make_env):
    users = fixture_json("users.signed-in.json")
    # Same server (same Id) stored under another address.
    users["users"][0]["credentials"][0]["address"] = "http://jellyfin.example.invalid:8096"
    env = make_env(users=users)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is True
    assert body["sign_in"]["account"] == "alice"
    assert body["sign_in"]["server_name"] == "Home"


def test_status_signed_in_but_no_server_chosen(make_env):
    env = make_env(users=fixture_json("users.signed-in.json"))
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is False
    assert body["sign_in"]["ready"] is False
    assert body["sign_in"]["other_server"] is True
    assert body["sign_in"]["account"] == "alice"
    assert body["sign_in"]["server_name"] == "Home"


def test_status_not_signed_in_anywhere_is_not_other_server(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    _, body = env.call("GET", "/status")
    assert body["sign_in"]["other_server"] is False
    assert body["sign_in"]["account"] is None


def test_status_signed_in_server_offline_uses_stored_name(make_env):
    env = make_env(users=fixture_json("users.signed-in.json"))  # 192.0.2.10
    env.service.server_timeout = 0.5
    env.configdb.values["player.jellyfin.server"] = "http://192.0.2.10:8096"
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is True
    assert body["sign_in"]["server_name"] == "Home"


def test_status_corrupt_users_json_not_signed_in(make_env, tmp_path):
    env = make_env(users={})
    env.users_path.write_text("{not json")
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is False
    assert env.users_path.read_text() == "{not json"  # status changes nothing


def test_server_choice_manual_wins(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = "http://192.0.2.99:8096"
    env.configdb.values["player.jellyfin.server_url"] = env.jellyfin.url
    status, body = env.call("POST", "/login")
    assert status == 200, body
    assert env.jellyfin.calls("POST", "/QuickConnect/Initiate")


def test_server_choice_read_fresh_and_normalised(js, make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    assert env.service.server_address() is None
    env.configdb.values["player.jellyfin.server"] = "http://192.0.2.10:8096/"
    assert env.service.server_address() == "http://192.0.2.10:8096"
    env.configdb.values["player.jellyfin.server_url"] = "jellyfin.example.invalid"
    assert env.service.server_address() == "http://jellyfin.example.invalid:8096"
    env.configdb.values["player.jellyfin.server_url"] = "https://jellyfin.example.invalid"
    assert env.service.server_address() == "https://jellyfin.example.invalid"
    env.configdb.values["player.jellyfin.server_url"] = "192.0.2.11:8920"
    assert env.service.server_address() == "http://192.0.2.11:8920"


# --- login ------------------------------------------------------------------

def test_initiate_returns_code(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    status, body = env.call("POST", "/login")
    assert status == 200
    assert body == {"code": CODE, "expires_in": 300}
    assert SECRET not in env.bodies[-1]
    auth = env.jellyfin.calls("POST", "/QuickConnect/Initiate")[0]["headers"]["Authorization"]
    assert auth.startswith('MediaBrowser Client="HiFiBerryOS", Device="Living Room", '
                           'DeviceId="00000000-0000-4000-8000-0000000000d1", Version="')
    assert "Token=" not in auth
    status, body = env.call("GET", "/login")
    assert body == {"state": "waiting", "code": CODE, "error": None}


def test_initiate_without_server(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    status, body = env.call("POST", "/login")
    assert status == 409
    assert body == {"error": "Choose a server first."}


def test_initiate_quick_connect_disabled(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.jellyfin.initiate_status = 401
    status, body = env.call("POST", "/login")
    assert status == 409
    assert body == {"error": "Quick Connect is disabled on the server"}
    _, body = env.call("GET", "/login")
    assert body["state"] == "idle"


def test_initiate_server_unreachable(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = closed_port_url()
    status, body = env.call("POST", "/login")
    assert status == 502
    assert body == {"error": "Server not reachable"}


def test_initiate_writes_nothing(make_env):
    users = fixture_json("users.fresh.json")
    env = make_env(users=users)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    before = env.users_path.read_bytes(), env.users_path.stat().st_mtime_ns
    assert env.call("POST", "/login")[0] == 200
    assert env.call("GET", "/login")[1]["state"] == "waiting"
    assert (env.users_path.read_bytes(), env.users_path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in env.conf_dir.iterdir()) == ["users.json"]
    assert env.systemctl_calls() == []


def test_users_json_created_with_the_credential(make_env):
    env = make_env()
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    status, _ = env.call("POST", "/login")
    assert status == 200
    assert not env.conf_dir.exists()  # nothing on disk before sign-in completes
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    users = env.users()
    fresh = fixture_json("users.fresh.json")
    assert set(users) == set(fresh)
    user = users["users"][0]
    assert set(user) == set(fresh["users"][0])
    assert users["active"] == user["id"]
    assert [c["AccessToken"] for c in user["credentials"]] == [TOKEN]
    assert user["default"] is True
    assert user["name"] == "(default)"
    import uuid
    assert uuid.UUID(user["id"]).version == 4
    assert uuid.UUID(user["device_id"]).version == 4
    assert user["id"] != user["device_id"]
    assert stat.S_IMODE(env.users_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.conf_dir.stat().st_mode) == 0o700
    for request in env.jellyfin.requests:
        assert f'DeviceId="{user["device_id"]}"' in request["headers"]["Authorization"]


def test_users_json_appearing_meanwhile_with_other_device_id_fails(make_env):
    env = make_env(active=True)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    # The shim started meanwhile and created its own users.json.
    env.conf_dir.mkdir(parents=True)
    env.users_path.write_text(json.dumps(fixture_json("users.fresh.json")))
    env.jellyfin.authenticated = True
    body = env.call("GET", "/login")[1]
    assert body["state"] == "failed" and body["error"]
    assert env.users() == fixture_json("users.fresh.json")
    assert env.systemctl_calls() == [IS_ACTIVE, STOP, START]


def test_poll_approved_writes_users_json(make_env):
    users = fixture_json("users.fresh.json")
    users["users"][0]["last_server"] = "kept"
    env = make_env(users=users, active=True)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    assert env.call("POST", "/login")[0] == 200
    assert env.call("GET", "/login")[1]["state"] == "waiting"
    assert not env.users()["users"][0]["credentials"]
    assert env.systemctl_calls() == []

    env.jellyfin.authenticated = True
    status, body = env.call("GET", "/login")
    assert status == 200
    assert body == {"state": "approved", "code": CODE, "error": None}

    saved = env.users()
    user = saved["users"][0]
    assert user["last_server"] == "kept"
    assert len(user["credentials"]) == 1
    cred = user["credentials"][0]
    sample = fixture_json("users.signed-in.json")["users"][0]["credentials"][0]
    assert list(cred) == list(sample)
    assert cred["address"] == env.jellyfin.url
    assert (cred["Name"], cred["Id"], cred["Version"]) == ("Home", SERVER_ID, "12.4.0")
    assert cred["UserId"] == USER_ID and cred["username"] == "alice"
    assert cred["AccessToken"] == TOKEN
    assert cred["Users"] == [{"Id": USER_ID, "IsSignedInOffline": True}]
    assert len(cred["DateLastAccessed"]) == len("2026-11-12T12:43:22Z")
    assert cred["DateLastAccessed"].endswith("Z")
    assert stat.S_IMODE(env.users_path.stat().st_mode) == 0o600

    auth_call = env.jellyfin.calls("POST", "/Users/AuthenticateWithQuickConnect")[0]
    assert json.loads(auth_call["body"]) == {"Secret": SECRET}
    for request in env.jellyfin.requests:
        assert 'DeviceId="00000000-0000-4000-8000-0000000000d1"' in request["headers"]["Authorization"]
    # Stopped before users.json was written, started after.
    assert env.systemctl_calls() == [IS_ACTIVE, STOP, START]
    assert env.snapshot("stop")["users"][0]["credentials"] == []
    assert env.snapshot("start") == saved

    # Further polls do not authenticate again.
    assert env.call("GET", "/login")[1]["state"] == "approved"
    assert len(env.jellyfin.calls("POST", "/Users/AuthenticateWithQuickConnect")) == 1

    _, status_body = env.call("GET", "/status")
    assert status_body["logged_in"] is True
    assert status_body["sign_in"]["account"] == "alice"


def test_poll_approved_player_inactive_no_restart(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), active=False)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    assert env.users()["users"][0]["credentials"]
    assert env.systemctl_calls() == [IS_ACTIVE]


def test_poll_approved_player_does_not_stop(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), active=True)
    env.stop_rc.write_text("1")
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    # Written anyway, then a restart, so the player at least reads it.
    assert env.systemctl_calls() == [IS_ACTIVE, STOP, RESTART]
    assert env.snapshot("restart")["users"][0]["credentials"][0]["AccessToken"] == TOKEN


def test_poll_approved_replaces_every_credential(make_env):
    # One device, one signed-in user on one server: the shim connects with
    # every credential it has, so one left behind for the previous server
    # would keep the device a cast target there.
    users = fixture_json("users.signed-in.json")
    other = copy.deepcopy(users["users"][0]["credentials"][0])
    other.update(Id="ffffffffffffffffffffffffffffffff", address="http://192.0.2.20:8096",
                 AccessToken="OTHER")
    users["users"][0]["credentials"].append(other)
    profile = copy.deepcopy(users["users"][0])
    profile.update(id="00000000-0000-4000-8000-000000000002", default=False,
                   device_id="00000000-0000-4000-8000-0000000000d2")
    users["users"].append(profile)
    env = make_env(users=users)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    saved = env.users()
    creds = saved["users"][0]["credentials"]
    assert [(c["Id"], c["AccessToken"]) for c in creds] == [(SERVER_ID, TOKEN)]
    assert saved["users"][1]["credentials"] == []
    raw = env.users_path.read_text()
    assert "OTHER" not in raw and "FAKE-TOKEN-0000" not in raw


def test_sign_in_rewrites_the_shim_backup(make_env):
    # The shim restores users.json from users.json.bak when the file is
    # damaged; a backup still holding the old server's token would bring it
    # back.
    old = fixture_json("users.signed-in.json")
    env = make_env(users=fixture_json("users.fresh.json"))
    backup = env.conf_dir / "users.json.bak"
    backup.write_text(json.dumps(old))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    assert json.loads(backup.read_text()) == env.users()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_sign_in_on_other_server_then_status(make_env):
    users = fixture_json("users.signed-in.json")  # server A at 192.0.2.10
    users["users"][0]["credentials"][0]["Id"] = "a" * 32
    env = make_env(users=users)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url  # server B
    assert env.call("GET", "/status")[1]["sign_in"]["other_server"] is True
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is True
    assert body["sign_in"]["other_server"] is False
    assert [c["Id"] for c in env.users()["users"][0]["credentials"]] == [SERVER_ID]


def test_poll_expires(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.clock.now += 299
    assert env.call("GET", "/login")[1]["state"] == "waiting"
    env.clock.now += 2
    env.jellyfin.authenticated = True  # too late
    status, body = env.call("GET", "/login")
    assert status == 200
    assert body["state"] == "expired"
    assert body["error"]
    assert not env.jellyfin.calls("POST", "/Users/AuthenticateWithQuickConnect")
    assert env.users()["users"][0]["credentials"] == []


def test_poll_secret_forgotten_by_server_expires(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.routes[("GET", "/QuickConnect/Connect")] = lambda r: (404, None)
    assert env.call("GET", "/login")[1]["state"] == "expired"


def test_poll_server_gone_keeps_waiting(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.routes[("GET", "/QuickConnect/Connect")] = lambda r: (503, None)
    assert env.call("GET", "/login")[1]["state"] == "waiting"


def test_poll_authenticate_rejected_fails(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    env.jellyfin.routes[("POST", "/Users/AuthenticateWithQuickConnect")] = lambda r: (401, None)
    body = env.call("GET", "/login")[1]
    assert body["state"] == "failed" and body["error"]
    assert env.users()["users"][0]["credentials"] == []


def test_second_initiate_replaces_pending(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    env.clock.now += 200
    assert env.call("POST", "/login")[0] == 200
    env.clock.now += 200  # 400 s after the first, 200 s after the second
    assert env.call("GET", "/login")[1]["state"] == "waiting"


def test_poll_idle(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    assert env.call("GET", "/login") == (200, {"state": "idle", "code": None, "error": None})


def test_cancel(make_env):
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("POST", "/login")
    status, _ = env.call("DELETE", "/login")
    assert status == 200
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1] == {"state": "idle", "code": None, "error": None}
    assert not env.jellyfin.calls("POST", "/Users/AuthenticateWithQuickConnect")
    assert env.users()["users"][0]["credentials"] == []


# --- logout -----------------------------------------------------------------

def test_logout_removes_credentials(make_env):
    users = fixture_json("users.signed-in.json")
    env = make_env(users=users, active=True)
    users["users"][0]["credentials"][0]["address"] = env.jellyfin.url
    env.users_path.write_text(json.dumps(users))
    status, _ = env.call("POST", "/logout")
    assert status == 200
    saved = env.users()
    assert saved["users"][0]["credentials"] == []
    assert saved["users"][0]["device_id"] == users["users"][0]["device_id"]
    assert stat.S_IMODE(env.users_path.stat().st_mode) == 0o600
    logout = env.jellyfin.calls("POST", "/Sessions/Logout")
    assert len(logout) == 1
    assert 'Token="FAKE-TOKEN-0000000000000000000000"' in logout[0]["headers"]["Authorization"]
    assert env.systemctl_calls() == [IS_ACTIVE, STOP, START]
    assert env.snapshot("stop")["users"][0]["credentials"]
    assert env.snapshot("start")["users"][0]["credentials"] == []
    _, body = env.call("GET", "/status")
    assert body["logged_in"] is False


def test_logout_removes_credentials_of_every_profile_and_server(make_env):
    users = fixture_json("users.signed-in.json")
    users["users"][0]["credentials"][0]["Id"] = "a" * 32  # another server
    users["users"][0]["credentials"][0]["address"] = closed_port_url()
    profile = copy.deepcopy(users["users"][0])
    profile.update(id="00000000-0000-4000-8000-000000000002", default=False)
    users["users"].append(profile)
    env = make_env(users=users, active=False)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    assert env.call("POST", "/logout")[0] == 200
    assert [u["credentials"] for u in env.users()["users"]] == [[], []]
    _, body = env.call("GET", "/status")
    assert body["sign_in"]["other_server"] is False
    assert body["sign_in"]["account"] is None


def _token_files(conf_dir):
    return sorted(p.name for p in conf_dir.iterdir()
                  if p.is_file() and "FAKE-TOKEN" in p.read_text(errors="replace"))


def test_logout_leaves_no_token_in_shim_backups(make_env):
    users = fixture_json("users.signed-in.json")
    env = make_env(users=users, active=False)
    text = json.dumps(users)
    for name in ("users.json.bak", "users.json.tmp", "users.json.unreadable-1700000000",
                 "users.json.corrupt"):
        (env.conf_dir / name).write_text(text)
    (env.conf_dir / "conf.json").write_text('{"volume": 42}')
    assert env.call("POST", "/logout")[0] == 200
    assert _token_files(env.conf_dir) == []
    # The backup is kept, matching users.json, as the shim expects it.
    assert json.loads((env.conf_dir / "users.json.bak").read_text()) == env.users()
    assert (env.conf_dir / "conf.json").read_text() == '{"volume": 42}'


def test_logout_cleans_backups_even_when_users_json_has_no_token(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), active=True)
    (env.conf_dir / "users.json.bak").write_text(json.dumps(fixture_json("users.signed-in.json")))
    assert env.call("POST", "/logout")[0] == 200
    assert _token_files(env.conf_dir) == []
    # Nothing in users.json changed, so the player is left alone.
    assert env.systemctl_calls() == []


def test_logout_server_unreachable_still_removes(make_env):
    env = make_env(users=fixture_json("users.signed-in.json"), active=False)
    users = env.users()
    users["users"][0]["credentials"][0]["address"] = closed_port_url()
    env.users_path.write_text(json.dumps(users))
    status, _ = env.call("POST", "/logout")
    assert status == 200
    assert env.users()["users"][0]["credentials"] == []
    assert env.systemctl_calls() == [IS_ACTIVE]


def test_logout_not_signed_in_leaves_player_alone(make_env):
    env = make_env(users=fixture_json("users.fresh.json"), active=True)
    before = env.users_path.read_bytes()
    assert env.call("POST", "/logout")[0] == 200
    assert env.users_path.read_bytes() == before
    assert env.systemctl_calls() == []


def test_logout_without_users_json(make_env):
    env = make_env()
    assert env.call("POST", "/logout")[0] == 200


# --- secrets ----------------------------------------------------------------

def test_no_token_in_any_response_or_log(make_env, caplog):
    caplog.set_level(logging.DEBUG)
    env = make_env(users=fixture_json("users.fresh.json"))
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.call("GET", "/status")
    env.call("POST", "/login")
    env.call("GET", "/login")
    env.jellyfin.authenticated = True
    env.call("GET", "/login")
    env.call("GET", "/login")
    env.call("GET", "/status")
    env.call("GET", "/servers")
    env.call("GET", "/nope")
    env.call("POST", "/logout")
    env.call("GET", "/status")
    assert env.jellyfin.calls("POST", "/Sessions/Logout")
    responses = "\n".join(env.bodies)
    logs = caplog.text
    assert logs  # something was logged, so the check below means something
    for secret in (TOKEN, SECRET):
        assert secret not in responses
        assert secret not in logs
        assert secret not in env.systemctl_log.read_text()


def test_auth_header_values_are_encoded(js):
    header = js.auth_header('Kitchen, "upstairs"', "d1")
    assert 'Device="Kitchen%2C %22upstairs%22"' in header
    assert header.count('="') == 4  # Client, Device, DeviceId, Version


def test_server_supplied_names_logged_escaped(make_env, caplog):
    caplog.set_level(logging.DEBUG)
    env = make_env(users=fixture_json("users.fresh.json"), active=False)
    env.configdb.values["player.jellyfin.server"] = env.jellyfin.url
    env.jellyfin.routes[("POST", "/Users/AuthenticateWithQuickConnect")] = lambda r: (200, {
        "AccessToken": TOKEN, "User": {"Id": USER_ID, "Name": "alice\nforged line"}})
    env.call("POST", "/login")
    env.jellyfin.authenticated = True
    assert env.call("GET", "/login")[1]["state"] == "approved"
    assert "forged line" in caplog.text
    assert not any("\n" in record.getMessage() for record in caplog.records)


# --- discovery --------------------------------------------------------------

def test_discovery_parses_replies(js):
    reply = json.dumps({"Address": "http://192.0.2.10:8096", "Id": SERVER_ID,
                        "Name": "Home", "EndpointAddress": None}).encode()
    assert js.parse_discovery_reply(reply) == {
        "id": SERVER_ID, "name": "Home", "address": "http://192.0.2.10:8096"}
    assert js.parse_discovery_reply(b"not json") is None
    assert js.parse_discovery_reply(b"[]") is None
    assert js.parse_discovery_reply(json.dumps({"Id": "x", "Name": "y"}).encode()) is None
    assert js.parse_discovery_reply(b"\xff\xfe") is None


def test_discovery_collects_and_dedups(js):
    replies = [
        json.dumps({"Address": "http://192.0.2.10:8096", "Id": "a", "Name": "Home"}).encode(),
        b"garbage",
        json.dumps({"Address": "http://192.0.2.10:8096", "Id": "a", "Name": "Home"}).encode(),
        json.dumps({"Address": "http://192.0.2.11:8096", "Id": "b", "Name": "Attic"}).encode(),
    ]
    assert js.collect_servers(replies) == [
        {"id": "b", "name": "Attic", "address": "http://192.0.2.11:8096"},
        {"id": "a", "name": "Home", "address": "http://192.0.2.10:8096"},
    ]


def test_discover_over_udp(js):
    """discover() broadcasts the probe and collects the answers."""
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    port = responder.getsockname()[1]
    seen = []

    def answer():
        data, addr = responder.recvfrom(1024)
        seen.append(data)
        responder.sendto(json.dumps({"Address": "http://192.0.2.10:8096", "Id": "a",
                                     "Name": "Home"}).encode(), addr)
    threading.Thread(target=answer, daemon=True).start()
    try:
        servers = js.discover(target=("127.0.0.1", port), listen=0.5)
    finally:
        responder.close()
    assert seen == [b"who is JellyfinServer?"]
    assert servers == [{"id": "a", "name": "Home", "address": "http://192.0.2.10:8096"}]


def test_servers_returns_cache_immediately(make_env):
    release = threading.Event()
    started = threading.Event()
    results = [[{"id": "a", "name": "Home", "address": "http://192.0.2.10:8096"}]]

    def slow_scan():
        started.set()
        release.wait(10)
        return results[0]

    env = make_env(scan=slow_scan)
    env.service.start()
    assert started.wait(5)
    t0 = time.monotonic()
    status, body = env.call("GET", "/servers", timeout=2)
    assert time.monotonic() - t0 < 0.1
    assert status == 200 and body == []
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = env.call("GET", "/servers")[1]
        if body:
            break
        time.sleep(0.02)
    assert body == results[0]


# --- HTTP surface -----------------------------------------------------------

def test_unknown_path_and_method(make_env):
    env = make_env()
    assert env.call("GET", "/nope")[0] == 404
    assert env.call("DELETE", "/status")[0] == 405


def test_main_serves_on_loopback(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ,
               JELLYFIN_CONF_DIR=str(tmp_path / "conf"),
               JELLYFIN_CONFIG_SERVER=closed_port_url(),
               JELLYFIN_SYSTEMCTL_CMD="false",
               JELLYFIN_DISCOVERY_TARGET="127.0.0.1:9")
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        body = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
                    body = json.loads(r.read())
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        assert body is not None
        assert body["logged_in"] is False
        assert body["is_active"] is False
        assert body["sign_in"]["ready"] is False
        # Not reachable on another local address (none on a host without a route).
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                lan_ip = probe.getsockname()[0]
        except OSError:
            lan_ip = "127.0.0.1"
        if lan_ip != "127.0.0.1":
            with pytest.raises(OSError):
                socket.create_connection((lan_ip, port), timeout=1).close()
    finally:
        proc.terminate()
        proc.wait(5)
