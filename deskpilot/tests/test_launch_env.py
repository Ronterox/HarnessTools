"""Unit tests for what a launch gets (environment, command line) and the sandbox's private D-Bus bus.
No X display needed: the bus tests run a real dbus-daemon, the rest fake the processes.

Run: .venv/bin/python -m pytest tests/test_launch_env.py
"""
import ast
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from deskpilot import procs, xutil  # noqa: E402
from deskpilot.procs import SANDBOX_SET, isolated_command, launch_env  # noqa: E402

USER = {
    "DISPLAY": ":91", "HOME": "/home/u", "PATH": "/usr/bin", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "WAYLAND_DISPLAY": "wayland-0", "XMODIFIERS": "@im=ibus", "GTK_IM_MODULE": "ibus", "QT_IM_MODULE": "ibus",
    "GDK_SCALE": "2", "GDK_DPI_SCALE": "0.5", "QT_SCALE_FACTOR": "2", "QT_AUTO_SCREEN_SCALE_FACTOR": "1",
    "NO_AT_BRIDGE": "1", "GDK_BACKEND": "wayland",
}
BUS = "unix:path=/run/user/1000/deskpilot/sandbox91-x.bus,guid=1"
A11Y = "unix:path=/run/user/1000/at-spi/bus_1"
PROFILE = "/tmp/deskpilot-1000/sandbox91-x/profile-1"


# ---------------------------------------------------------------- launch_env

def test_desktop_launch_only_gains_accessibility():
    e = launch_env(USER)
    assert e["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] == "1"
    assert "NO_AT_BRIDGE" not in e
    rest = {k: v for k, v in e.items() if k != "QT_LINUX_ACCESSIBILITY_ALWAYS_ON"}
    assert rest == {k: v for k, v in USER.items() if k != "NO_AT_BRIDGE"}


def test_sandbox_launch_unsets_session_variables():
    e = launch_env(USER, sandbox=True, bus=BUS, a11y_bus=A11Y)
    for k in ("WAYLAND_DISPLAY", "QT_IM_MODULE", "GDK_SCALE", "GDK_DPI_SCALE", "QT_SCALE_FACTOR",
              "QT_AUTO_SCREEN_SCALE_FACTOR", "NO_AT_BRIDGE"):
        assert k not in e, k  # really unset, not ""


def test_sandbox_launch_sets_defaults():
    e = launch_env(USER, sandbox=True, bus=BUS, a11y_bus=A11Y)
    want = {
        "GTK_USE_PORTAL": "0", "GDK_DEBUG": "no-portals", "GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb",
        "SDL_VIDEODRIVER": "x11", "MOZ_ENABLE_WAYLAND": "0", "ELECTRON_OZONE_PLATFORM_HINT": "x11",
        "LIBGL_ALWAYS_SOFTWARE": "1", "__GLX_VENDOR_LIBRARY_NAME": "mesa", "XMODIFIERS": "@im=none",
        "GTK_IM_MODULE": "gtk-im-context-simple", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
        "DBUS_SESSION_BUS_ADDRESS": BUS, "AT_SPI_BUS_ADDRESS": A11Y,
    }
    assert {k: e.get(k) for k in want} == want
    assert e["DISPLAY"] == ":91" and e["HOME"] == "/home/u"  # the rest is inherited
    assert USER["WAYLAND_DISPLAY"] == "wayland-0"  # the input dict is not modified


def test_gdk_debug_is_appended_to():
    assert launch_env({**USER, "GDK_DEBUG": "events"}, sandbox=True)["GDK_DEBUG"] == "events,no-portals"
    assert launch_env({**USER, "GDK_DEBUG": "no-portals,misc"}, sandbox=True)["GDK_DEBUG"] == "no-portals,misc"
    assert launch_env({**USER, "GDK_DEBUG": "events"})["GDK_DEBUG"] == "events"  # desktop: untouched


def test_sandbox_without_private_bus_keeps_users_bus():
    e = launch_env(USER, sandbox=True, bus=None, a11y_bus=A11Y)
    assert e["DBUS_SESSION_BUS_ADDRESS"] == USER["DBUS_SESSION_BUS_ADDRESS"]
    assert "AT_SPI_BUS_ADDRESS" not in e  # the user's bus already leads to their a11y bus


# ---------------------------------------------------------------- launch() on a fake sandbox

class FakePopen:
    calls = []

    def __init__(self, command, **kw):
        self.pid, self.command, self.kw = 424242, command, kw
        FakePopen.calls.append(self)

    def poll(self):
        return None


@pytest.fixture
def fake_sandbox(tmp_path, monkeypatch):
    """procs.launch aimed at a sandbox that isn't there: Popen captured, nothing runs."""
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(procs.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(procs, "_a11y_bus", lambda: A11Y)
    monkeypatch.setattr(procs, "PROCS", {})
    sb = procs.Sandbox(":199", "Xvfb", "800x600", None, str(tmp_path), BUS)
    monkeypatch.setattr(procs, "SANDBOX", sb)
    monkeypatch.setattr(xutil, "_display", ":199")
    FakePopen.calls = []
    return sb


def test_launch_caller_env_overrides_defaults(fake_sandbox, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    p = procs.launch("./app", env={"GDK_BACKEND": "wayland", "LIBGL_ALWAYS_SOFTWARE": "0",
                                   "WAYLAND_DISPLAY": "wayland-1"})
    e = FakePopen.calls[-1].kw["env"]
    assert e["GDK_BACKEND"] == "wayland" and e["LIBGL_ALWAYS_SOFTWARE"] == "0"
    assert e["WAYLAND_DISPLAY"] == "wayland-1"
    assert e["DISPLAY"] == ":199" and e["DBUS_SESSION_BUS_ADDRESS"] == BUS and e["AT_SPI_BUS_ADDRESS"] == A11Y
    assert set(SANDBOX_SET) <= set(e)
    assert p.warning == "" and p.display == ":199"


def test_launch_rewrites_firefox_and_makes_profile(fake_sandbox):
    p = procs.launch("firefox https://x.com")
    assert p.command.startswith(f"firefox --new-instance --profile {fake_sandbox.tmp}/profile-")
    assert FakePopen.calls[-1].command == p.command
    assert os.path.isdir(p.command.split()[3])
    assert "browser.preonboarding.enabled" in open(os.path.join(p.command.split()[3], "user.js")).read()


def test_launch_warns_without_a11y_bus(fake_sandbox, monkeypatch):
    monkeypatch.setattr(procs, "_a11y_bus", lambda: None)
    p = procs.launch("./app")
    assert "ui()/act() won't see this app" in p.warning
    assert FakePopen.calls[-1].kw["env"].get("AT_SPI_BUS_ADDRESS") == os.environ.get("AT_SPI_BUS_ADDRESS")


def test_launch_warns_without_private_bus(fake_sandbox):
    fake_sandbox.bus, fake_sandbox.bus_error = None, "dbus-daemon not installed"
    p = procs.launch("./app")
    assert "no private D-Bus bus (dbus-daemon not installed)" in p.warning


def test_desktop_launch_gets_no_sandbox_defaults(fake_sandbox, monkeypatch):
    monkeypatch.setattr(xutil, "_display", None)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    p = procs.launch("firefox")
    e = FakePopen.calls[-1].kw["env"]
    assert p.command == "firefox" and e["WAYLAND_DISPLAY"] == "wayland-0" and "GTK_USE_PORTAL" not in e


# ---------------------------------------------------------------- isolated_command

def test_firefox_gets_new_instance_and_profile():
    assert isolated_command("firefox", PROFILE) == f"firefox --new-instance --profile {PROFILE}"
    assert isolated_command("firefox 'https://x.com/a b'", PROFILE) == \
        f"firefox --new-instance --profile {PROFILE} 'https://x.com/a b'"
    assert isolated_command('firefox "https://a.com/?a=1&b=2"', PROFILE) == \
        f'firefox --new-instance --profile {PROFILE} "https://a.com/?a=1&b=2"'
    assert isolated_command("/usr/bin/firefox-esr https://x.com/#top", PROFILE) == \
        f"/usr/bin/firefox-esr --new-instance --profile {PROFILE} https://x.com/#top"
    assert isolated_command("firefox", "/tmp/a b") == "firefox --new-instance --profile '/tmp/a b'"


def test_firefox_own_instance_flag_still_gets_profile():
    assert isolated_command("firefox --new-instance", PROFILE) == f"firefox --profile {PROFILE} --new-instance"
    assert isolated_command("firefox -no-remote x.com", PROFILE) == f"firefox --profile {PROFILE} -no-remote x.com"


def test_chrome_and_libreoffice_get_profiles():
    assert isolated_command("google-chrome about:blank", PROFILE) == \
        f"google-chrome --user-data-dir={PROFILE} --no-first-run --no-default-browser-check " \
        "--force-renderer-accessibility about:blank"
    assert isolated_command("chromium", PROFILE).startswith(f"chromium --user-data-dir={PROFILE} ")
    assert isolated_command("soffice --writer", PROFILE) == \
        f"soffice -env:UserInstallation=file://{PROFILE} --writer"
    assert isolated_command("libreoffice", "/tmp/a b") == "libreoffice -env:UserInstallation=file:///tmp/a%20b"


def test_left_alone():
    for cmd in ("firefox -P work", "firefox --profile /tmp/mine", "firefox --profile=/tmp/mine",
                "firefox -ProfileManager", "google-chrome --user-data-dir=/tmp/c",
                "soffice -env:UserInstallation=file:///tmp/lo", "firefox https://x.com & sleep 1",
                "firefox x.com; rm -f y", "firefox x.com | cat", "firefox x.com > log", "(firefox)",
                "MOZ_LOG=x firefox", "firefox $URL", 'firefox "$URL"', 'firefox "a\'$HOME\'b"', 'firefox "a\\b"', "firefox `cat u`", r"firefox a\ b",
                "firefox 'unbalanced", "firefox x\nfirefox y", "gedit firefox", "firefoxx", "code .",
                "./build/app --x", ""):
        assert isolated_command(cmd, PROFILE) == cmd, cmd
    assert isolated_command("firefox '$HOME'", PROFILE).endswith(" '$HOME'")  # single-quoted: literal


# ---------------------------------------------------------------- _a11y_bus

@pytest.fixture
def no_a11y_cache(monkeypatch):
    monkeypatch.setattr(procs, "_A11Y_BUS", None)
    monkeypatch.delenv("AT_SPI_BUS_ADDRESS", raising=False)


def test_a11y_bus_prefers_env(no_a11y_cache, monkeypatch):
    monkeypatch.setenv("AT_SPI_BUS_ADDRESS", "unix:path=/x")
    monkeypatch.setattr(procs.subprocess, "run", lambda *a, **k: pytest.fail("asked gdbus"))
    assert procs._a11y_bus() == "unix:path=/x"


def test_a11y_bus_failure_is_none_and_retried(no_a11y_cache, monkeypatch):
    calls = []

    def run(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no gdbus")
        if len(calls) == 2:
            return subprocess.CompletedProcess(a, 1, "", "Error: name not owned")
        return subprocess.CompletedProcess(a, 0, f"('{A11Y}',)\n", "")
    monkeypatch.setattr(procs.subprocess, "run", run)
    assert procs._a11y_bus() is None
    assert procs._a11y_bus() is None
    assert procs._a11y_bus() == A11Y
    assert procs._a11y_bus() == A11Y and len(calls) == 3  # found once, then cached


# ---------------------------------------------------------------- private dirs, the bus, the sweep

def test_private_dir_refuses_symlinks_and_tightens_mode(tmp_path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    procs.private_dir(str(d))
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (oct(os.stat(procs.private_dir(str(tmp_path / "new"))).st_mode & 0o777)) == "0o700"
    (tmp_path / "link").symlink_to(d)
    with pytest.raises(RuntimeError, match="symlink"):
        procs.private_dir(str(tmp_path / "link"))


def _gdbus(addr, method):
    return subprocess.run(["gdbus", "call", "--address", addr, "--dest", "org.freedesktop.DBus",
                           "--object-path", "/org/freedesktop/DBus", "--method", f"org.freedesktop.DBus.{method}"],
                          capture_output=True, text=True, timeout=5)


@pytest.mark.skipif(not procs.shutil.which("dbus-daemon"), reason="needs dbus-daemon")
def test_start_bus_and_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(procs, "_socket_dir", lambda: str(tmp_path))  # not the real $XDG_RUNTIME_DIR
    tmp = str(tmp_path / "sandbox199-test")
    os.mkdir(tmp)
    addr, bus_p, err, sock = procs._start_bus(tmp, ":199")
    assert addr and bus_p and err == "", err
    assert addr.startswith(f"unix:path={sock}") and os.path.exists(sock)
    names = _gdbus(addr, "ListActivatableNames").stdout
    activatable = sorted(ast.literal_eval(names)[0])
    want = ["ca.desrt.dconf", "org.freedesktop.DBus"] if os.listdir(os.path.join(tmp, "services")) \
        else ["org.freedesktop.DBus"]
    assert activatable == want, names
    assert bus_p.stdout.closed  # never read again, never blocks
    x = subprocess.Popen(["sleep", "60"])  # stands in for the X server
    monkeypatch.setattr(procs, "PROCS", {})
    monkeypatch.setattr(procs, "SANDBOX", procs.Sandbox(":199", "Xvfb", "8x8", x, tmp, addr, bus_p, "", sock))
    assert procs.stop_sandbox() == "sandbox :199 stopped"
    assert bus_p.poll() is not None and x.poll() is not None
    assert not os.path.exists(sock) and not os.path.exists(tmp)
    assert _gdbus(addr, "ListNames").returncode != 0


def test_start_bus_failures_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path))
    tmp = str(tmp_path / "sandbox199-test")
    os.mkdir(tmp)
    monkeypatch.setattr(procs, "_socket_dir", lambda: "/tmp/" + "x" * 120)
    addr, p, err, _ = procs._start_bus(tmp, ":199")
    assert addr is None and p is None and "socket path too long" in err
    monkeypatch.setattr(procs, "_socket_dir", lambda: 1 / 0)
    addr, p, err, _ = procs._start_bus(tmp, ":199")
    assert addr is None and "ZeroDivisionError" in err  # an exception means no bus, never a raise
    monkeypatch.setattr(procs.shutil, "which", lambda name: None)
    assert procs._start_bus(tmp, ":199") == (None, None, "dbus-daemon not installed", "")


@pytest.mark.skipif(not procs.shutil.which("dbus-daemon"), reason="needs dbus-daemon")
def test_start_bus_interrupted_leaves_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(procs, "_socket_dir", lambda: str(tmp_path))
    tmp = str(tmp_path / "sandbox199-test")
    os.mkdir(tmp)
    started = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        started.append(real_popen(*a, **k))
        return started[-1]

    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(procs.subprocess, "Popen", popen)
    monkeypatch.setattr(procs.selectors, "DefaultSelector", interrupt)
    with pytest.raises(KeyboardInterrupt):
        procs._start_bus(tmp, ":199")
    assert started and started[0].poll() is not None  # the daemon was killed before letting it through


def _dead_pid():
    """(pid, start) of a process that has exited."""
    gone = subprocess.Popen(["true"])
    start = procs._proc_start(gone.pid)
    gone.wait()
    return gone.pid, start


def test_sweep_ends_what_a_dead_deskpilot_left(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(procs, "_socket_dir", lambda: str(tmp_path))
    gone = subprocess.Popen(["true"])
    gone_start = procs._proc_start(gone.pid)
    gone.wait()
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    sock = tmp_path / "sandbox198-stale.bus"
    sock.write_text("")
    stale = tmp_path / "sandbox198-stale"
    stale.mkdir()
    (stale / "owner.json").write_text(json.dumps({"pid": gone.pid, "start": gone_start, "sock": str(sock),
                                                  "procs": [[orphan.pid, procs._proc_start(orphan.pid)]]}))
    live = tmp_path / "sandbox197-live"
    live.mkdir()
    (live / "owner.json").write_text(json.dumps({"pid": os.getpid(), "start": procs._proc_start(os.getpid()),
                                                 "procs": [[orphan.pid, procs._proc_start(orphan.pid)]]}))
    setting_up = tmp_path / "sandbox196-new"
    setting_up.mkdir()  # no owner.json yet
    procs._sweep_stale()
    for _ in range(30):
        if orphan.poll() is not None:
            break
        time.sleep(0.1)
    assert orphan.poll() is not None
    assert not stale.exists() and not sock.exists()
    assert live.exists() and setting_up.exists()


@pytest.mark.parametrize("bad", [[-1, None], [0, None], [None, None], ["1", None], [1, None], ["-1", "x"],
                                 [os.getpid(), procs._proc_start(os.getpid())], [2.5, 1], [[3], 4], "x", [5]])
def test_sweep_never_kills_from_a_bad_record(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(procs, "_socket_dir", lambda: str(tmp_path))
    killed = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: killed.append(pid))
    pid, start = _dead_pid()
    d = tmp_path / "sandbox198-bad"
    d.mkdir()
    (d / "owner.json").write_text(json.dumps({"pid": pid, "start": start, "procs": [bad]}))
    procs._sweep_stale()
    assert killed == []


def test_sweep_leaves_foreign_sock_and_unjudgeable_records(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(procs, "_socket_dir", lambda: str(tmp_path))
    os.mkdir(tmp_path / "logs")
    victim = tmp_path / "precious"
    victim.write_text("keep me")
    pid, start = _dead_pid()
    d = tmp_path / "logs" / "sandbox198-foreign"
    d.mkdir()
    (d / "owner.json").write_text(json.dumps({"pid": pid, "start": start, "sock": str(victim), "procs": []}))
    procs._sweep_stale()
    assert victim.exists() and not d.exists()  # the dir is cleaned, the foreign path untouched
    for owner in ({"pid": -1, "start": None}, {"pid": "1", "start": 1}, {"pid": pid}, [1, 2]):
        d = tmp_path / "logs" / "sandbox197-odd"
        d.mkdir()
        (d / "owner.json").write_text(json.dumps(owner))
        procs._sweep_stale()
        assert d.exists(), owner  # can't tell whether its deskpilot is gone: leave it
        procs.shutil.rmtree(d)
    real = tmp_path / "real"
    real.mkdir()
    (real / "owner.json").write_text(json.dumps({"pid": pid, "start": start, "procs": []}))
    (tmp_path / "logs" / "sandbox196-link").symlink_to(real)
    procs._sweep_stale()
    assert real.exists() and (real / "owner.json").exists()  # a symlinked dir is never followed
