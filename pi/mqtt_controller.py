"""
MQTT Controller — manages a mosquitto broker for ESP32 MQTT client testing.

Used by the portal to start/stop a local MQTT broker accessible to devices
on the testbench WiFi AP.
"""

import logging
import os
import pwd
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_PORT = 1883
WORK_DIR = "/tmp/mqtt-tester"
MOSQUITTO_CONF = os.path.join(WORK_DIR, "mosquitto.conf")
MOSQUITTO_LOG = os.path.join(WORK_DIR, "mosquitto.log")
# What the broker prints before it opens MOSQUITTO_LOG (a bad config, a busy
# port), kept for the start-failure message.
MOSQUITTO_STDOUT = os.path.join(WORK_DIR, "mosquitto.stdout")
# Started as root, mosquitto drops to this user before opening its log.
BROKER_USER = "mosquitto"
# root:<BROKER_USER's group>, group-writable, so the broker can create its log.
WORK_DIR_MODE = 0o775

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active = False
_proc = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_work_dir():
    """Create WORK_DIR and let the broker user write its log there."""
    os.makedirs(WORK_DIR, exist_ok=True)
    try:
        gid = pwd.getpwnam(BROKER_USER).pw_gid
    except KeyError:
        # No such user: mosquitto keeps running as root and can write anyway.
        logger.warning("user %s not found; leaving %s root-owned",
                       BROKER_USER, WORK_DIR)
        return
    os.chown(WORK_DIR, 0, gid)
    os.chmod(WORK_DIR, WORK_DIR_MODE)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _kill_proc(proc, timeout=5.0):
    """Terminate a subprocess, SIGKILL if it won't die."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _kill_existing():
    """Kill a previously-started *test* broker, and only that (best effort).

    The pattern is matched against our own config path, not the bare word
    "mosquitto". A broad `pkill -f mosquitto` also kills a system
    `mosquitto.service` or any other broker on the host — a test fixture must
    never take down infrastructure it does not own.

    This reclaims the port after a portal restart, where the old broker is still
    running but `_proc` no longer refers to it.
    """
    try:
        subprocess.run(
            ["pkill", "-f", f"mosquitto -c {MOSQUITTO_CONF}"],
            capture_output=True, timeout=5, check=False,
        )
        time.sleep(0.3)
    except Exception:
        pass


def _port_owner() -> str:
    """Describe what is listening on MQTT_PORT, or '' if nothing is."""
    try:
        out = subprocess.run(
            ["ss", "-tlnp", f"sport = :{MQTT_PORT}"],
            capture_output=True, timeout=5, check=False,
        ).stdout.decode(errors="replace")
    except Exception:
        return ""
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    return lines[0].strip() if lines else ""


def broker_config(port: int) -> str:
    """The test broker's config: open, no auth, all interfaces.

    The broker writes MOSQUITTO_LOG itself, as user BROKER_USER, which is why
    `_ensure_work_dir` makes WORK_DIR writable for that user's group.
    """
    return "\n".join([
        f"listener {port}",
        "allow_anonymous true",
        f"log_dest file {MOSQUITTO_LOG}",
        "log_type all",
    ]) + "\n"


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start():
    """Start the mosquitto MQTT broker. Returns dict with port."""
    global _active, _proc

    with _lock:
        if _active and _proc is not None and _proc.poll() is None:
            return {"port": MQTT_PORT}

        _ensure_work_dir()
        _kill_existing()

        # If something still holds the port it is not ours, so refuse rather than
        # kill it. Silently killing a broker we did not start is how a test run
        # takes down a service somebody else depends on.
        owner = _port_owner()
        if owner:
            raise RuntimeError(
                f"port {MQTT_PORT} is already in use by a broker this service did "
                f"not start; stop it first. Listener: {owner}")

        with open(MOSQUITTO_CONF, "w") as f:
            f.write(broker_config(MQTT_PORT))

        # mosquitto appends to its log; removing it makes the log cover this
        # start only. Stdout goes to a file: a pipe nobody reads would stall
        # the broker once it filled.
        _remove(MOSQUITTO_LOG)
        with open(MOSQUITTO_STDOUT, "wb") as out:
            _proc = subprocess.Popen(
                ["mosquitto", "-c", MOSQUITTO_CONF],
                stdout=out, stderr=subprocess.STDOUT,
            )

        # Wait for it to initialise
        time.sleep(1.0)
        if _proc.poll() is not None:
            _active = False
            raise RuntimeError(
                f"mosquitto failed to start: {_read(MOSQUITTO_STDOUT)[-500:]}")

        _active = True
        logger.info("MQTT broker started on port %d", MQTT_PORT)
        return {"port": MQTT_PORT}


def stop():
    """Stop the mosquitto broker."""
    global _active, _proc

    with _lock:
        _kill_proc(_proc)
        _proc = None
        _active = False
        logger.info("MQTT broker stopped")


def status():
    """Return broker status dict."""
    global _active

    with _lock:
        running = _active and _proc is not None and _proc.poll() is None
        # If process died unexpectedly, update state
        if _active and not running:
            _active = False
        return {
            "running": running,
            "port": MQTT_PORT if running else None,
        }


def log():
    """The broker's log since it last started; '' if it never has."""
    return {"log": _read(MOSQUITTO_LOG)}
