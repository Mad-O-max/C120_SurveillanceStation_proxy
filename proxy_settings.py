"""Validated settings and private credentials for the C120 proxy."""
import argparse
import getpass
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import sys

DEFAULTS = dict(port=12020, rtsp_port=12554, camera_http_port=2020,
                camera_rtsp_port=554, lifetime=600, renew_margin=30, pull_timeout=5,
                io_timeout=8, log_level="INFO")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def read_json(stream):
    data = stream.read(16385)
    if len(data) > 16384:
        raise ValueError("Settings file exceeds 16 KiB")
    try:
        result = json.loads(data)
    except ValueError:
        raise ValueError("Invalid JSON settings file") from None
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def settings(args):
    values = dict(DEFAULTS)
    if args.config:
        with open(args.config, encoding="utf-8") as stream:
            values.update(read_json(stream))
    if values.keys() - (DEFAULTS.keys() | {"camera"}):
        raise ValueError("Unknown configuration key")
    for key in DEFAULTS.keys() | {"camera"}:
        override = getattr(args, key, None)
        if override is not None:
            values[key] = override
    try:
        camera = ipaddress.IPv4Address(values.get("camera", ""))
    except (ValueError, TypeError):
        raise ValueError("Supply the camera IPv4 address with --camera or config") from None
    if not isinstance(values["camera"], str) or camera.is_unspecified or camera.is_multicast or int(camera) == 0xffffffff:
        raise ValueError("Camera must be a unicast IPv4 address")
    limits = dict(port=(1024, 65535), rtsp_port=(1024, 65535),
                  camera_http_port=(1, 65535), camera_rtsp_port=(1, 65535),
                  lifetime=(30, 3600), renew_margin=(5, 180), pull_timeout=(1, 5),
                  io_timeout=(2, 10))
    for key, (low, high) in limits.items():
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError("{} must be an integer between {} and {}".format(key, low, high))
    if values["port"] == values["rtsp_port"]:
        raise ValueError("Listener ports must differ")
    if values["io_timeout"] <= values["pull_timeout"]:
        raise ValueError("io_timeout must exceed pull_timeout")
    if camera.is_loopback and {values["port"], values["rtsp_port"]} & {
            values["camera_http_port"], values["camera_rtsp_port"]}:
        raise ValueError("Loopback upstream overlaps a listener")
    if values["log_level"] not in LEVELS:
        raise ValueError("Invalid log_level")
    return argparse.Namespace(**values)


def private_directory(path):
    if os.name != "posix":
        raise ValueError("Unattended mode requires POSIX permissions")
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Private directory must be owned by the runtime user with mode 0700")


def credentials(path):
    if path is None:
        if not sys.stdin.isatty():
            raise ValueError("Use --credentials for unattended operation")
        user = input("Camera account username: ").strip()
        password = getpass.getpass("Camera account password: ")
    else:
        private_directory(Path(path).parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Credential file must be owned by the runtime user with mode 0600")
            data = read_json(stream)
        if data.keys() != {"username", "password"}:
            raise ValueError("Credentials require username and password")
        user, password = data["username"], data["password"]
    if any(not isinstance(value, str) or not value or len(value) > 1024 for value in (user, password)):
        raise ValueError("Invalid camera credentials")
    return user, password


def configure_logging(level, filename=None, logger=None):
    logger = logger or logging.getLogger("c120")
    handler = (RotatingFileHandler(filename, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
               if filename else logging.StreamHandler())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for previous in logger.handlers[:]:
        logger.removeHandler(previous)
        previous.close()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    logging.raiseExceptions = False
    return logger
