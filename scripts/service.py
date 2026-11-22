"""DSM 6 service controller. Run all commands as the dedicated runtime user."""
import argparse
import errno
import fcntl
import logging
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from proxy_settings import configure_logging, private_directory

RESTART_DELAYS = (5, 15, 45)
STOP_TIMEOUT = 12


def command(state, *extra):
    return [sys.executable, str(ROOT / "c120_onvif_proxy.py"),
            "--config", str(state / "proxy.json"),
            "--credentials", str(state / "credentials.json"), *extra]


def request(state, action):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        try:
            client.connect(str(state / "control.sock"))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return "stopped"
            raise
        client.sendall(action.encode("ascii"))
        return client.recv(100).decode("ascii")


def open_lock(state):
    return os.open(state / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def supervise(state, lock):
    # Inherited flock remains held for the entire supervisor lifetime.
    address = state / "control.sock"
    if address.exists():
        address.unlink()
    logger = configure_logging("INFO", state / "service.log", logger=logging.getLogger("c120.service"))
    stopping = False
    child = None
    ready_fd = None
    failures = 0
    ready = False
    launch_at = time.monotonic()
    started = 0

    def stop(*_):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(address))
        server.listen(4)
        logger.info("Supervisor started")
        try:
            while not stopping:
                now = time.monotonic()
                if child is not None and child.poll() is not None:
                    if ready_fd is not None:
                        os.close(ready_fd)
                        ready_fd = None
                    if ready and now - started >= 300:
                        failures = 0
                    logger.warning("Proxy exited with code %s", child.returncode)
                    child, ready = None, False
                    if failures == len(RESTART_DELAYS):
                        logger.error("Restart limit reached")
                        break
                    launch_at = now + RESTART_DELAYS[failures]
                    failures += 1
                if child is None and now >= launch_at:
                    ready_fd, write_fd = os.pipe()
                    try:
                        env = dict(os.environ, C120_READY_FD=str(write_fd))
                        child = subprocess.Popen(
                            command(state, "--log-file", str(state / "proxy.log")),
                            pass_fds=(lock, write_fd), env=env)
                    finally:
                        os.close(write_fd)
                    started = now
                readers = [server]
                if ready_fd is not None:
                    readers.append(ready_fd)
                readable, _, _ = select.select(readers, [], [], 0.25)
                if ready_fd in readable:
                    ready = os.read(ready_fd, 1) == b"1"
                    os.close(ready_fd)
                    ready_fd = None
                if server in readable:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(0.25)
                        try:
                            if conn.recv(16) == b"stop":
                                stop()
                            status = ("stopping" if stopping else "running" if ready else
                                      "starting" if child is not None else "restart pending")
                            conn.sendall(status.encode("ascii"))
                        except OSError:
                            pass
        finally:
            if ready_fd is not None:
                os.close(ready_fd)
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            address.unlink()
            logger.info("Supervisor stopped")


def start(state):
    lock = open_lock(state)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Service already active or starting")
            return 0
        if subprocess.call(command(state, "--check")):
            return 1
        child = subprocess.Popen(
            [sys.executable, __file__, "--state", str(state), "_supervise", str(lock)],
            pass_fds=(lock,), start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and child.poll() is None:
            if request(state, "status") == "running":
                print("Service running")
                return 0
            time.sleep(0.1)
        print("Service exited or is still starting. See service.log and proxy.log.")
        return 1
    finally:
        os.close(lock)


def stop(state):
    # A held lock with no controller can be a starting supervisor or its
    # orphaned child. Never send signals to an unverified PID.
    deadline = time.monotonic() + STOP_TIMEOUT + 3
    try:
        request(state, "stop")
    except (TimeoutError, ConnectionResetError):
        pass
    lock = open_lock(state)
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print("Service stopped")
                return 0
            except BlockingIOError:
                time.sleep(0.2)
                try:
                    request(state, "stop")
                except (TimeoutError, ConnectionResetError, BrokenPipeError):
                    pass
    finally:
        os.close(lock)
    print("Stop timed out. Check service.log and verify process ownership.")
    return 1


def status(state):
    state_text = request(state, "status")
    if state_text == "stopped":
        lock = open_lock(state)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state_text = "lock held; controller unavailable"
        finally:
            os.close(lock)
    print(state_text, flush=True)
    return 0 if state_text == "running" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "diagnose", "_supervise"))
    parser.add_argument("lock", type=int, nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    state = args.state.absolute()
    try:
        private_directory(state)
        if len(os.fsencode(str(state / "control.sock"))) >= 108:
            raise ValueError("State directory path is too long for the control socket")
        if args.action == "_supervise" and args.lock is not None:
            supervise(state, args.lock)
        elif args.action == "diagnose":
            status(state)
            return subprocess.call(command(state, "--diagnose"))
        elif args.action == "restart":
            return stop(state) or start(state)
        elif args.action in ("start", "stop", "status"):
            return {"start": start, "stop": stop, "status": status}[args.action](state)
        else:
            parser.error("Invalid service command")
    except ValueError as exc:
        parser.error(str(exc))
    except OSError as exc:
        parser.error("Service operation failed ({}, errno {})".format(type(exc).__name__, exc.errno))


if __name__ == "__main__":
    sys.exit(main())
