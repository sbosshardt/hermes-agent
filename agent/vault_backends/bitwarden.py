"""Bitwarden Password Manager logins as a vault backend (``bw`` CLI).

This is the personal/org *password* vault (``bw``), distinct from the
Bitwarden Secrets Manager (``bws``) source that hydrates API keys at startup.
Unlock: ``bw unlock --raw --passwordenv VAR`` (the CLI rejects a piped password) mints a
``BW_SESSION`` token. List: ``bw list items`` filtered to type=1 (login) with
a URI. Resolve: ``bw get password <id>``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Dict, List, Optional

from agent.secret_sources.base import run_cli, scrub_ansi
from agent.vault_backends import unlock as _unlock
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_secret_env
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_ENV_KEEP = ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot",
             "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "BITWARDENCLI_APPDATA_DIR")
_UNATTENDED_LOCK = threading.Lock()


def _private_path(path_text: str, root_name: str, *, secret_file: bool) -> str:
    """Validate and open each component below this profile, without following symlinks.

    Only the profile user may replace a checked component before the CLI uses a directory;
    group/world-writable parent directories are refused. The file is read from its verified
    descriptor, so a path swap between stat and read cannot substitute a different secret.
    """
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
            next_fd = os.open(part, flags, dir_fd=fd)
            opened.callback(os.close, next_fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("Bitwarden unattended directory must be owner-controlled")
        if not secret_file:
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError("Bitwarden CLI appdata directory must be mode 0700")
            return str(path)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
        opened.callback(os.close, file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("Bitwarden unattended password file must be owner-controlled and mode 0600")
        if info.st_size > 4096:
            raise RuntimeError("Bitwarden unattended password file is too large")
        return os.read(file_fd, 4097).decode("utf-8")


class BitwardenLoginBackend(LoginBackend):
    name = "bitwarden"
    display_name = "Bitwarden"
    prefix = "bw:"
    needs_unlock = True

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def _bw(self) -> Path:
        explicit = str(self.cfg.get("binary_path") or "")
        found = explicit or shutil.which("bw")
        if not found:
            raise RuntimeError("Bitwarden CLI (bw) not found — install it or set vault.bitwarden.binary_path")
        return Path(found)

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        if self.cfg.get("unattended_password_file"):
            appdata = str(self.cfg.get("appdata_dir") or "")
            env["BITWARDENCLI_APPDATA_DIR"] = _private_path(appdata, "vault", secret_file=False)
        env["NO_COLOR"] = "1"
        if session_token:
            env["BW_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        if not _unlock.is_unlocked(self.name):
            return False
        if self.cfg.get("unattended_password_file"):
            try:
                if not self._account_is_expected():
                    _unlock.lock(self.name)
                    return False
            except Exception:
                _unlock.lock(self.name)
                return False
        return True

    def _account_is_expected(self) -> bool:
        expected = str(self.cfg.get("account_email") or "").strip().lower()
        if not expected:
            return False
        proc = run_cli([str(self._bw()), "status", "--nointeraction"], env=self._env(None),
                       timeout=_TIMEOUT, timeout_message="bw status timed out", label="bw",
                       stdin=subprocess.DEVNULL)
        try:
            account = json.loads(proc.stdout or "")
        except (ValueError, TypeError):
            return False
        return proc.returncode == 0 and account.get("status") in ("locked", "unlocked") and (
            str(account.get("userEmail") or "").lower() == expected
        )

    def unlock(self, master_password: str) -> None:
        # bw refuses a piped password ("Master password is required"); its non-interactive contract is
        # --passwordenv: the variable exists only in the child's environment, never in argv or ours.
        generation = _unlock.begin_unlock(self.name)
        proc = run_with_secret_env([str(self._bw()), "unlock", "--raw", "--nointeraction", "--passwordenv", "HERMES_BW_MASTER"],
                                   env=self._env(None), secret_env="HERMES_BW_MASTER", secret=master_password,
                                   timeout=_TIMEOUT, label="bw")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            err = scrub_ansi(proc.stderr or "").strip()[:200]
            if "not logged in" in err.lower():
                err = "not logged in — run `bw login` once in a terminal first"
            raise RuntimeError(f"Bitwarden unlock failed: {err or 'no session key'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("Bitwarden was locked while unlocking; try again")

    def try_unattended_unlock(self) -> bool:
        """Opt in to the dedicated bot account's existing 0600 bootstrap; never expose its value."""
        configured = str(self.cfg.get("unattended_password_file") or "")
        if not configured:
            return False
        with _UNATTENDED_LOCK:
            if self.is_unlocked():
                return True
            lines = _private_path(configured, "secrets", secret_file=True).splitlines()
            if len(lines) != 1 or len(lines[0]) > 4096:
                raise RuntimeError("Bitwarden unattended password file must contain exactly one line")
            password = lines[0].removeprefix("BW_PASSWORD=")
            if not password:
                raise RuntimeError("Bitwarden unattended password file is empty")
            if not self._account_is_expected():
                raise RuntimeError("Bitwarden CLI is not signed in as the configured bot account")
            try:
                self.unlock(password)
            except Exception:
                raise RuntimeError("Bitwarden unattended unlock failed") from None
            finally:
                del password
            return True

    def _run(self, *args: str) -> str:
        token = _unlock.get_session_token(self.name)
        if not token:
            raise UnlockRequired(self)
        if self.cfg.get("unattended_password_file") and not self._account_is_expected():
            _unlock.lock(self.name)
            raise UnlockRequired(self)
        proc = run_cli([str(self._bw()), *args, "--nointeraction"], env=self._env(token), timeout=_TIMEOUT,
                       label="bw", timeout_message="bw timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = scrub_ansi(proc.stderr or "")
            if "locked" in err.lower() or "session" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"bw failed: {err[:200]}")
        return proc.stdout or ""

    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        # Read the manager's latest value rather than silently reusing its encrypted disk cache
        # after an item is added or rotated. A failed sync fails closed.
        self._run("sync")
        raw = json.loads(self._run("list", "items") or "[]")
        out: List[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            if item.get("type") != 1 or not isinstance(item.get("login"), dict):
                continue
            login = item["login"]
            origins: List[str] = []
            for uri in login.get("uris") or []:
                if uri.get("match") == 5:  # Bitwarden URI match "Never": not a fill target
                    continue
                try:
                    origin = normalize_origin(str(uri.get("uri") or ""))
                except Exception:
                    continue
                if origin and origin not in origins:
                    origins.append(origin)
            if not origins:
                continue
            username = str(login.get("username") or "").strip() or None
            # Fill targets are browser pages, so app URIs (androidapp:// etc.) never widen
            # the fill set; an app-URI-only item keeps its single origin exactly as before.
            web_origins = tuple(o for o in origins if o.startswith(("http://", "https://"))) or (origins[0],)
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("name") or origins[0]),
                origin=origins[0], created_at=str(item.get("creationDate") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=web_origins, has_otp=bool(login.get("totp"))))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str) -> str:
        return self._run("get", "password", handle[len(self.prefix):]).rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `bw get totp <id>` mints the current code from the item's TOTP seed; "No TOTP available" otherwise.
        try:
            code = self._run("get", "totp", handle[len(self.prefix):]).strip()
        except Exception:
            return None
        return code if code.isdigit() else None
