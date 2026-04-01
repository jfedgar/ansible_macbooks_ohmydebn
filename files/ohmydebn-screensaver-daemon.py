#!/usr/bin/python3
"""
OhMyDebn TTE Screensaver Daemon

Launches a fullscreen Alacritty terminal running Terminal Text Effects (TTE)
when the system goes idle, replicating the Omarchy screensaver experience on
Cinnamon/X11.

Security model: the daemon does NOT lock the session. Instead, it relies on
Cinnamon's native lock-on-suspend (org.cinnamon.settings-daemon.plugins.power
lock-on-suspend=true). The user sets the suspend timeout to match their desired
screensaver duration — when suspend fires, the session locks automatically.

The daemon must actively suppress cinnamon-screensaver during TTE playback
because csd-power unconditionally activates it at idle-delay (it ignores the
idle-activation-enabled flag). Without suppression, cinnamon-screensaver's
Stage (a Gtk.WindowType.POPUP override-redirect window) covers Alacritty.
"""
import subprocess
import signal
import os
import sys
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

POLL_INTERVAL = 0.5  # seconds between idle checks

# --- STATE ---
screensaver_active = False
alacritty_process = None
# Track whether we intentionally deactivated cinnamon-screensaver to avoid loops
suppressing_cinnamon = False


def log(msg):
    print(f"[ohmydebn-screensaver] {msg}", flush=True)


def get_gsettings(schema, key):
    try:
        output = subprocess.check_output(
            ["gsettings", "get", schema, key],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if "uint32" in output:
            return int(output.split()[-1])
        if "true" in output.lower():
            return True
        if "false" in output.lower():
            return False
        return output
    except Exception:
        return None


def get_idle_time():
    try:
        ms = int(subprocess.check_output(["xprintidle"]).decode().strip())
        return ms / 1000.0
    except Exception:
        return 0


def stop_screensaver():
    global screensaver_active, alacritty_process
    if alacritty_process:
        try:
            os.killpg(os.getpgid(alacritty_process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            log(f"Error killing alacritty: {e}")
        alacritty_process = None
    screensaver_active = False


def start_screensaver():
    global screensaver_active, alacritty_process
    if screensaver_active:
        return

    script = os.path.expanduser("~/.local/bin/ohmydebn-cmd-screensaver")
    if not os.path.exists(script):
        log(f"Script not found: {script}")
        return

    cmd = [
        "alacritty",
        "--class", "ohmydebn.screensaver",
        "-o", 'window.startup_mode="Fullscreen"',
        "-o", 'window.decorations="None"',
        "-o", "font.size=18",
        "-o", 'colors.primary.background="#000000"',
        "-o", 'colors.primary.foreground="#ffffff"',
        "-e", script,
    ]
    try:
        alacritty_process = subprocess.Popen(cmd, preexec_fn=os.setsid)
        screensaver_active = True
        log("TTE screensaver started")
    except Exception as e:
        log(f"Failed to start screensaver: {e}")


def deactivate_cinnamon_screensaver():
    """Dismiss cinnamon-screensaver's Stage so TTE remains visible."""
    global suppressing_cinnamon
    try:
        suppressing_cinnamon = True
        subprocess.run(
            ["cinnamon-screensaver-command", "-d"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        log("Suppressed cinnamon-screensaver activation during TTE")
    except Exception as e:
        log(f"Failed to deactivate cinnamon-screensaver: {e}")
    finally:
        # Reset after a short delay to allow the D-Bus signal to propagate
        GLib.timeout_add(500, _reset_suppressing)


def _reset_suppressing():
    global suppressing_cinnamon
    suppressing_cinnamon = False
    return False  # don't repeat


def on_active_changed(active):
    """
    D-Bus signal handler for org.cinnamon.ScreenSaver.ActiveChanged.

    If cinnamon-screensaver activates while TTE is playing, suppress it.
    If cinnamon-screensaver deactivates (user unlocked), clean up TTE.
    """
    if active and screensaver_active and not suppressing_cinnamon:
        # csd-power activated cinnamon-screensaver while TTE is running — fight it
        log("cinnamon-screensaver activated during TTE, suppressing...")
        deactivate_cinnamon_screensaver()
    elif active and not screensaver_active:
        # Manual lock or other activation — don't interfere
        pass
    elif not active:
        # User dismissed lock screen (or we suppressed it) — stop TTE if idle reset
        if suppressing_cinnamon:
            # We caused this deactivation, ignore it
            pass
        else:
            stop_screensaver()


def check_loop():
    global screensaver_active

    idle_delay = get_gsettings("org.cinnamon.desktop.session", "idle-delay")
    if idle_delay is None or idle_delay == 0:
        if screensaver_active:
            stop_screensaver()
        return True

    current_idle = get_idle_time()

    # Launch TTE when idle threshold is reached
    if current_idle >= idle_delay and not screensaver_active:
        start_screensaver()

    # Dismiss TTE on user activity (idle counter resets)
    if screensaver_active and current_idle < 1.0:
        log("User activity detected, stopping TTE")
        stop_screensaver()

    # Check if alacritty died unexpectedly
    if screensaver_active and alacritty_process and alacritty_process.poll() is not None:
        log("Alacritty process exited unexpectedly")
        screensaver_active = False
        alacritty_process = None

    return True


if __name__ == "__main__":
    log("Starting daemon")

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    bus.add_signal_receiver(
        on_active_changed,
        signal_name="ActiveChanged",
        dbus_interface="org.cinnamon.ScreenSaver",
        bus_name="org.cinnamon.ScreenSaver",
    )

    GLib.timeout_add(int(POLL_INTERVAL * 1000), check_loop)

    loop = GLib.MainLoop()

    def shutdown(signum, frame):
        log("Shutting down")
        stop_screensaver()
        loop.quit()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run()
