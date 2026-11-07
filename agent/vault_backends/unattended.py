"""Opt-in Bitwarden bootstrap path validation and profile-local serialization."""
from __future__ import annotations

import os
import stat
import threading
from contextlib import ExitStack
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def profile_lock():
    from hermes_constants import get_hermes_home

    key = str(get_hermes_home().resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def private_path(path_text: str, root_name: str, *, secret_file: bool) -> str:
    """Walk beneath this profile with owned, no-follow descriptors; never source a shell file."""
    from hermes_constants import get_hermes_home

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
        raise RuntimeError("Unattended Bitwarden requires no-follow, owner-checked file access")
    home = get_hermes_home().resolve()
    path = Path(path_text)
    if not path.is_absolute():
        raise RuntimeError("Bitwarden unattended path must be absolute")
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        raise RuntimeError("Bitwarden unattended path must be inside this profile") from None
    if len(parts) < 2 or parts[0] != root_name or any(p in ("", ".", "..") for p in parts):
        raise RuntimeError("Bitwarden unattended path must be inside this profile's private directory")
    directory_parts = parts[:-1] if secret_file else parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    with ExitStack() as opened:
        fd = os.open(home, flags)
        opened.callback(os.close, fd)
        for part in directory_parts:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError("Bitwarden unattended directory must be owner-controlled")
            fd = os.open(part, flags, dir_fd=fd)
            opened.callback(os.close, fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("Bitwarden unattended directory must be owner-controlled")
        if not secret_file:
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError("Bitwarden CLI appdata directory must be mode 0700")
            return str(path)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                          | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
        opened.callback(os.close, file_fd)
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeError("Bitwarden unattended password file must be private, owned and mode 0600")
        if info.st_size > 4096:
            raise RuntimeError("Bitwarden unattended password file is too large")
        raw = os.read(file_fd, 4097)
        if len(raw) > 4096:
            raise RuntimeError("Bitwarden unattended password file is too large")
        return raw.decode("utf-8")


def read_password(path: str) -> str:
    lines = private_path(path, "secrets", secret_file=True).splitlines()
    if len(lines) != 1 or not lines[0].startswith("BW_PASSWORD="):
        raise RuntimeError("Bitwarden unattended password file must contain one BW_PASSWORD assignment")
    password = lines[0][len("BW_PASSWORD="):]
    if not password or "\x00" in password:
        raise RuntimeError("Bitwarden unattended password file is empty or invalid")
    return password
