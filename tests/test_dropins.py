"""Tests for the files the package drops into other components' directories,
and for the maintainer scripts that wire them up.

The descriptor is checked with configurator's own sanitize_settings and the
auth manifest with hifiberry-auth's own resolver when those are importable
(put their source trees on PYTHONPATH); otherwise with a copy of the rules
they apply, so the suite also runs in the package build.

debian/postinst, debian/prerm and debian/postrm run for real under sh, against a
temporary root ($DPKG_ROOT) and with stand-ins for getent, id, usermod,
systemctl and systemd-run that log what they were asked to do.
"""
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DESCRIPTOR = DATA / "usr/share/hifiberry/players.d/jellyfin.json"
ICON = DATA / "usr/share/hifiberry/players.d/icons/jellyfin.svg"
ACR = DATA / "etc/audiocontrol/players.d/jellyfin.json"
CONFSERVER = DATA / "etc/configserver/conf.d/jellyfin.json"
NGINX = DATA / "etc/nginx/hifiberry-api.d/jellyfin.nginx"
AUTH = DATA / "etc/hifiberry/auth.d/jellyfin-auth.json"
POSTINST = REPO / "debian/postinst"
POSTRM = REPO / "debian/postrm"
PRERM = REPO / "debian/prerm"
CONTROL = REPO / "debian/control"

BUS_NAME = "org.mpris.MediaPlayer2.com.github.iwalton3.jellyfin-mpv-shim"
POLICY = "usr/share/dbus-1/system.d/hifiberry-jellyfin.conf"


def load(path):
    return json.loads(path.read_text())


# --- descriptor -------------------------------------------------------------

try:
    from configurator.handlers.player_registry_handler import (
        REQUIRED_FIELDS, sanitize_settings)
except Exception:  # configurator not installed: the same checks, by hand
    REQUIRED_FIELDS = ("name", "provided_by", "systemd_service", "icon")

    def sanitize_settings(descriptor):
        clean = []
        for e in descriptor.get("settings", []):
            if not all(k in e for k in ("key", "type", "label", "default")):
                continue
            if e["type"] not in ("toggle", "select", "number", "secret", "text"):
                continue
            if e["type"] == "select" and not e.get("options") and not re.match(
                    r"^https?://(127\.0\.0\.1|localhost|\[::1\])[:/]", e.get("options_url", "")):
                continue
            if e["type"] == "text" and "pattern" in e:
                re.compile(e["pattern"])
            clean.append(e)
        return clean


def test_descriptor_required_fields():
    d = load(DESCRIPTOR)
    for field in REQUIRED_FIELDS:
        assert d.get(field), field
    assert d["name"] == "Jellyfin"
    assert d["provided_by"] == "hifiberry-jellyfin"
    assert d["systemd_service"] == "jellyfin"
    assert d["icon"] == "jellyfin"
    assert d["allow_change"] is True
    assert d["setup"] == {"base_url": "/api/jellyfin"}
    assert d["maintainer_name"] == "Wanted"
    assert d["maintainer_url"] == (
        "https://github.com/hifiberry/hifiberry-os/blob/hbosng/docs/maintainers-wanted.md")


def test_descriptor_settings_survive_sanitize():
    d = load(DESCRIPTOR)
    assert sanitize_settings(d) == d["settings"]


def test_descriptor_settings_exact():
    by_key = {s["key"]: s for s in load(DESCRIPTOR)["settings"]}
    assert list(by_key) == ["server", "server_url", "unlimited_remote"]

    server = by_key["server"]
    assert server["type"] == "select"
    assert server["default"] == ""
    assert server["options_url"] == "http://127.0.0.1:3551/servers"
    assert server["options_value"] == "address"
    assert server["options_label"] == "name"

    url = by_key["server_url"]
    assert url["type"] == "text"
    # The pattern rejects a bare host; the description has to say why.
    assert "http://" in url["description"] and "https://" in url["description"]
    assert "start with" in url["description"]
    assert url["default"] == ""
    assert url["pattern"] == "^(https?://.+)?$"
    pattern = re.compile(url["pattern"])
    for ok in ("", "http://jellyfin.local:8096", "https://media.example.org"):
        assert pattern.fullmatch(ok), ok
    for bad in ("jellyfin.local", "ftp://x", "http://"):
        assert not pattern.fullmatch(bad), bad

    unlimited = by_key["unlimited_remote"]
    assert unlimited["type"] == "toggle"
    assert unlimited["label"] == "Full quality over VPN"
    assert unlimited["default"] is False


def test_descriptor_icon_follows_theme():
    svg = ICON.read_text()
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "currentColor" in svg
    # No hard-coded colours: the WebUI themes icons through currentColor.
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", svg)


# --- audiocontrol, config-server --------------------------------------------

def test_acr_player_on_system_bus():
    assert load(ACR) == {"mpris": {
        "bus_name": BUS_NAME,
        "name": "jellyfin",
        "bus": "system",
        "poll_interval": 1.0,
        "clear_song_when_stopped": True,
        "artwork_dirs": ["/tmp/jellyfin-mpv-shim/.cache/jellyfin-mpv-shim/mpris"],
    }}


def test_acr_artwork_dir_matches_unit_cache():
    unit = (DATA / "usr/lib/systemd/user/jellyfin.service").read_text()
    cache = re.search(r"^Environment=XDG_CACHE_HOME=(\S+)$", unit, re.M).group(1)
    assert load(ACR)["mpris"]["artwork_dirs"] == [cache + "/jellyfin-mpv-shim/mpris"]


UNITS = DATA / "usr/lib/systemd/user"


def unit_settings(name):
    """{(section, key): [values]} of a unit file."""
    settings, section = {}, None
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        settings.setdefault((section, key.strip()), []).append(value.strip())
    return settings


@pytest.mark.parametrize("unit", ["jellyfin.service", "jellyfin-setup.service"])
def test_user_units_order_only_on_user_units(unit):
    # A --user manager cannot see system units: After=/Wants= on
    # network-online.target or sound.target do nothing there and only
    # suggest an ordering that does not exist.
    text = (UNITS / unit).read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for system_target in ("network-online.target", "network.target", "sound.target"):
        assert system_target not in code, (unit, system_target)


def test_player_unit_start_limit_is_reachable():
    # Restart=on-failure must end in a failed unit for a player that keeps
    # crashing, not restart it for ever: the burst has to fit in the
    # interval even with a few seconds of start-up per attempt.
    u = unit_settings("jellyfin.service")
    assert u[("Service", "Restart")] == ["on-failure"]
    restart_sec = float(u[("Service", "RestartSec")][0])
    interval = float(u[("Unit", "StartLimitIntervalSec")][0])
    burst = int(u[("Unit", "StartLimitBurst")][0])
    startup = 5  # generous: Python start plus the shim's imports on a Pi
    assert burst * (restart_sec + startup) < interval


def test_configserver_may_toggle_both_units():
    assert load(CONFSERVER) == {"systemd": {"jellyfin": "all", "jellyfin-setup": "all"}}


# --- nginx ------------------------------------------------------------------

def nginx_locations():
    text = re.sub(r"#.*", "", NGINX.read_text())
    return re.findall(r"location\s+(=\s*)?(\S+)\s*\{([^}]*)\}", text)


def test_nginx_exact_match_only():
    locations = nginx_locations()
    assert locations, "no location blocks"
    for exact, _, _ in locations:
        assert exact.strip() == "=", "prefix location would publish every path"
    assert not re.search(r"location\s+/api/jellyfin", re.sub(r"#.*", "", NGINX.read_text()))


def test_nginx_login_waits_for_sign_in_completion():
    # The poll that completes a sign-in stops the player, writes users.json
    # and starts it again (up to ~70 s); nginx's default 60 s would cut it.
    bodies = {path: body for _, path, body in nginx_locations()}
    timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", bodies["/api/jellyfin/login"])
    assert timeout and int(timeout.group(1)) >= 120


def test_nginx_proxies_setup_paths():
    proxied = {}
    for _, path, body in nginx_locations():
        target = re.search(r"proxy_pass\s+(\S+);", body).group(1)
        proxied[path] = target
    assert proxied == {
        "/api/jellyfin/status": "http://127.0.0.1:3551/status",
        "/api/jellyfin/login": "http://127.0.0.1:3551/login",
        "/api/jellyfin/logout": "http://127.0.0.1:3551/logout",
    }


# --- auth -------------------------------------------------------------------

def tier_for(method, uri):
    try:
        from hifiberry_auth.manifests import TierMap
    except Exception:
        TierMap = None
    if TierMap is not None:
        return TierMap(str(AUTH.parent)).load().tier(method, uri)
    # hifiberry-auth's resolution: first rule whose method and path match,
    # else default_tier; path segments matched with fnmatch, "**" = the rest.
    m = load(AUTH)
    prefix = m["match_prefix"].rstrip("/")
    if not (uri == prefix or uri.startswith(prefix + "/")):
        return "risky"
    rest = [s for s in uri[len(prefix):].split("/") if s]
    for rule in m["rules"]:
        methods = [x.upper() for x in rule.get("methods", ["*"])]
        if "*" not in methods and method not in methods:
            continue
        for pat in rule["paths"]:
            segs = [s for s in pat.split("/") if s]
            hit = True
            for i, p in enumerate(segs):
                if p == "**":
                    break
                if i >= len(rest) or not fnmatch.fnmatchcase(rest[i], p):
                    hit = False
                    break
            else:
                hit = len(segs) == len(rest)
            if hit:
                return "ok" if rule["tier"] == "ok" else "risky"
    return "ok" if m.get("default_tier") == "ok" else "risky"


def test_auth_manifest_shape():
    m = load(AUTH)
    assert m["service"] == "jellyfin"
    assert m["match_prefix"] == "/api/jellyfin"
    assert m["default_tier"] == "risky"


@pytest.mark.parametrize("method,path,tier", [
    ("GET", "/status", "ok"),
    # Only the client that started the sign-in needs the pending code.
    ("GET", "/login", "risky"),
    ("POST", "/login", "risky"),
    ("DELETE", "/login", "risky"),
    ("POST", "/logout", "risky"),
    ("GET", "/logout", "risky"),
    ("POST", "/status", "risky"),
    ("GET", "/servers", "risky"),
    ("GET", "/anything/else", "risky"),
])
def test_auth_tiers(method, path, tier):
    assert tier_for(method, "/api/jellyfin" + path) == tier


# --- debian/control ---------------------------------------------------------

def control_fields():
    text = CONTROL.read_text()
    binary = text.split("\nPackage:", 1)[1]
    binary = "\n".join(l for l in binary.splitlines() if not l.startswith("#"))
    fields = {}
    key = None
    for line in ("Package:" + binary).splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def test_control_extension_fields():
    f = control_fields()
    assert f["XB-Hifiberry-Extension"] == "yes"
    assert f["XB-Extension-Name"] == "Jellyfin"
    assert f["XB-Extension-Category"] == "player"
    assert f["XB-Extension-Needs-Reboot"] == "maybe"
    # Served by the WebUI itself, so it shows on a device without internet.
    assert f["XB-Extension-Icon-Url"].startswith("/images/")


def test_control_depends():
    deps = [d.strip() for d in control_fields()["Depends"].replace("\n", " ").split(",")]
    for want in ("${misc:Depends}", "${venv:Python}", "python3-gi", "gir1.2-glib-2.0",
                 "python3-pil", "libmpv2", "curl", "alsa-utils",
                 "hifiberry-configurator (>= 2.19.0)", "hifiberry-audiocontrol",
                 # The player card's text setting and sign-in panel.
                 "hifiberry-webui (>= 0.28.0)"):
        assert want in deps, want


# --- maintainer scripts -----------------------------------------------------

STUBS = {
    "getent": """#!/bin/sh
grep "^$2:" "$FAKE/$1" || exit 2
""",
    "id": """#!/bin/sh
case "$1" in
  -u) grep "^$2:" "$FAKE/passwd" | cut -d: -f3 ;;
  -nG) for g in $(cut -d: -f1 "$FAKE/group"); do
         grep "^$g:" "$FAKE/group" | cut -d: -f4 | tr , '\\n' | grep -qx "$2" && printf '%s ' "$g"
       done; echo ;;
  *) exit 1 ;;
esac
""",
    "usermod": """#!/bin/sh
echo "usermod $*" >> "$FAKE/log"
# usermod -aG GROUP USER
awk -F: -v OFS=: -v g="$2" -v u="$3" \\
  '$1 == g { $4 = ($4 == "" ? u : $4 "," u) } { print }' \\
  "$FAKE/group" > "$FAKE/group.new" && mv "$FAKE/group.new" "$FAKE/group"
""",
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$FAKE/log"
if [ "$1" = is-active ]; then [ -e "$FAKE/active-$3" ]; exit $?; fi
""",
    "systemd-run": """#!/bin/sh
echo "systemd-run $*" >> "$FAKE/log"
""",
}


class System:
    def __init__(self, tmp):
        self.root = tmp / "root"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        for d in (self.root / "etc", self.fake, self.bin):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.fake / "passwd").write_text(
            "root:x:0:0::/root:/bin/sh\nmusic:x:1000:1000::/home/music:/bin/sh\n")
        (self.fake / "group").write_text("audio:x:29:music\nrender:x:105:\n")
        (self.fake / "log").write_text("")

    def user_file(self, content):
        (self.root / "etc/hifiberry.user").write_text(content)

    def running(self, user_manager=True):
        (self.root / "run/systemd/system").mkdir(parents=True)
        if user_manager:
            (self.root / "run/user/1000").mkdir(parents=True)

    def run(self, script, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "DPKG_ROOT": str(self.root),
               "FAKE": str(self.fake)}
        return subprocess.run(["sh", str(script), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def log(self):
        return (self.fake / "log").read_text().splitlines()

    def policy(self):
        return self.root / POLICY

    def reboot_flag(self):
        return self.root / "run/reboot-required"


@pytest.fixture
def system(tmp_path):
    return System(tmp_path)


@pytest.mark.parametrize("script", [POSTINST, PRERM, POSTRM])
def test_maintainer_script_syntax(script):
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_postinst_generates_dbus_policy_for_player_user(system):
    system.user_file("# the media player user\n\nmusic\n")
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    root = ET.parse(system.policy()).getroot()
    assert root.tag == "busconfig"
    owners = root.findall("policy[@user='music']/allow")
    assert [a.attrib for a in owners] == [{"own": BUS_NAME}]
    senders = root.findall("policy[@context='default']/allow")
    assert [a.attrib for a in senders] == [{"send_destination": BUS_NAME}]


def test_postinst_reloads_dbus_on_running_system(system):
    system.user_file("music\n")
    system.running()
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert "systemctl reload dbus" in system.log()


def test_postinst_without_player_user_writes_no_policy(system):
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert not system.policy().exists()
    assert not any(l.startswith("usermod") for l in system.log())


def test_postinst_unknown_player_user_writes_no_policy(system):
    system.user_file("nobody-here\n")
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert not system.policy().exists()


def test_postinst_adds_render_group_and_asks_for_reboot(system):
    system.user_file("music\n")
    system.running()
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert "usermod -aG render music" in system.log()
    assert system.reboot_flag().exists()


def test_postinst_render_member_already_no_reboot(system):
    system.user_file("music\n")
    system.running()
    (system.fake / "group").write_text("audio:x:29:music\nrender:x:105:music\n")
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert not any(l.startswith("usermod") for l in system.log())
    assert not system.reboot_flag().exists()


def test_postinst_second_run_asks_for_no_reboot(system):
    system.user_file("music\n")
    system.running()
    assert system.run(POSTINST, "configure").returncode == 0
    system.reboot_flag().unlink()
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert [l for l in system.log() if l.startswith("usermod")] == ["usermod -aG render music"]
    assert not system.reboot_flag().exists()


def test_postinst_starts_setup_service_in_user_manager(system):
    system.user_file("music\n")
    system.running()
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    runs = [l for l in system.log() if l.startswith("systemd-run")]
    assert any("--uid 1000" in l and l.endswith("systemctl --user daemon-reload") for l in runs)
    assert any("--uid 1000" in l and "jellyfin-setup.service" in l for l in runs)
    # The player itself ships disabled; installing the package does not start it.
    assert not any(re.search(r"\bjellyfin\.service", l) for l in runs)


def user_manager_calls(system):
    """The systemctl --user command lines run through systemd-run."""
    return [l.split(" systemctl ", 1)[1] for l in system.log()
            if l.startswith("systemd-run") and "--uid 1000" in l
            and "XDG_RUNTIME_DIR=/run/user/1000" in l and " systemctl " in l]


def test_postinst_upgrade_restarts_a_running_player(system):
    # An upgrade replaces the shim and start-jellyfin under a running player;
    # try-restart picks the new ones up and leaves a stopped player stopped.
    system.user_file("music\n")
    system.running()
    r = system.run(POSTINST, "configure", "0.1.0")
    assert r.returncode == 0, r.stderr
    assert "--user try-restart jellyfin.service" in user_manager_calls(system)


def test_postinst_fresh_install_does_not_touch_the_player(system):
    system.user_file("music\n")
    system.running()
    r = system.run(POSTINST, "configure", "")
    assert r.returncode == 0, r.stderr
    assert not any(re.search(r"\bjellyfin\.service", c) for c in user_manager_calls(system))


def test_prerm_remove_stops_both_user_units(system):
    system.user_file("music\n")
    system.running()
    r = system.run(PRERM, "remove")
    assert r.returncode == 0, r.stderr
    calls = user_manager_calls(system)
    assert any(c.startswith("--user stop") and "jellyfin.service" in c
               and "jellyfin-setup.service" in c for c in calls), calls


def test_prerm_upgrade_leaves_the_player_running(system):
    # postinst's try-restart restarts a running player with the new files;
    # stopped here, it would stay off after every upgrade.
    system.user_file("music\n")
    system.running()
    r = system.run(PRERM, "upgrade", "0.2.0")
    assert r.returncode == 0, r.stderr
    assert user_manager_calls(system) == []


def test_prerm_without_user_manager_is_not_an_error(system):
    system.user_file("music\n")
    system.running(user_manager=False)
    r = system.run(PRERM, "remove")
    assert r.returncode == 0, r.stderr
    assert not any(l.startswith("systemd-run") for l in system.log())


def test_prerm_without_player_user_is_not_an_error(system):
    system.running()
    r = system.run(PRERM, "remove")
    assert r.returncode == 0, r.stderr
    assert not any(l.startswith("systemd-run") for l in system.log())


def test_prerm_failing_stop_does_not_block_removal(system):
    system.user_file("music\n")
    system.running()
    (system.bin / "systemd-run").write_text(
        '#!/bin/sh\necho "systemd-run $*" >> "$FAKE/log"\nexit 1\n')
    r = system.run(PRERM, "remove")
    assert r.returncode == 0, r.stderr


def test_prerm_not_running_system_does_nothing(system):
    system.user_file("music\n")
    r = system.run(PRERM, "remove")
    assert r.returncode == 0, r.stderr
    assert system.log() == []


def test_postinst_no_user_manager_is_not_an_error(system):
    system.user_file("music\n")
    system.running(user_manager=False)
    r = system.run(POSTINST, "configure")
    assert r.returncode == 0, r.stderr
    assert not any(l.startswith("systemd-run") for l in system.log())


def test_postinst_reloads_nginx_only_when_running(system):
    system.user_file("music\n")
    system.running()
    assert system.run(POSTINST, "configure").returncode == 0
    assert "systemctl reload nginx" not in system.log()
    (system.fake / "active-nginx").write_text("")
    assert system.run(POSTINST, "configure").returncode == 0
    assert "systemctl reload nginx" in system.log()


def test_postinst_never_restarts_config_server(system):
    system.user_file("music\n")
    system.running()
    (system.fake / "active-nginx").write_text("")
    assert system.run(POSTINST, "configure").returncode == 0
    assert not any("config-server" in l for l in system.log())


def test_postrm_purge_removes_policy(system):
    system.user_file("music\n")
    assert system.run(POSTINST, "configure").returncode == 0
    assert system.policy().exists()
    r = system.run(POSTRM, "remove")
    assert r.returncode == 0, r.stderr
    assert system.policy().exists()
    r = system.run(POSTRM, "purge")
    assert r.returncode == 0, r.stderr
    assert not system.policy().exists()
