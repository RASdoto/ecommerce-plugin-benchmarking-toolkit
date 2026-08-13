"""
Thin paramiko wrapper shared by bootstrap, sysmon, dbprobe, and seeder.
Handles key- or password-based auth, command exec, and SFTP put/get.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import paramiko
except Exception:  # pragma: no cover
    paramiko = None


class SSHError(RuntimeError):
    pass


class SSHClient:
    def __init__(self, host: str, user: str, port: int = 22,
                 key_path: str = "", password: str = ""):
        if paramiko is None:
            raise SSHError("paramiko is required (pip install paramiko)")
        self.host = host
        self.user = user
        self.port = port
        self.key_path = os.path.expanduser(key_path) if key_path else ""
        self.password = password
        self._client: Optional["paramiko.SSHClient"] = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(hostname=self.host, port=self.port, username=self.user,
                      timeout=20, banner_timeout=20, auth_timeout=20)
        if self.key_path and Path(self.key_path).exists():
            kwargs["key_filename"] = self.key_path
        elif self.password:
            kwargs["password"] = self.password
            kwargs["look_for_keys"] = False
        client.connect(**kwargs)
        self._client = client

    def run(self, command: str, timeout: float = 120.0) -> tuple[int, str, str]:
        if self._client is None:
            self.connect()
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def run_ok(self, command: str, timeout: float = 120.0) -> str:
        code, out, err = self.run(command, timeout=timeout)
        if code != 0:
            raise SSHError(f"cmd failed ({code}): {command}\n{err.strip()}")
        return out

    def put(self, local: str, remote: str) -> None:
        if self._client is None:
            self.connect()
        sftp = self._client.open_sftp()
        try:
            sftp.put(local, remote)
        finally:
            sftp.close()

    def put_text(self, text: str, remote: str) -> None:
        if self._client is None:
            self.connect()
        sftp = self._client.open_sftp()
        try:
            with sftp.file(remote, "w") as fh:
                fh.write(text)
        finally:
            sftp.close()

    def get(self, remote: str, local: str) -> None:
        if self._client is None:
            self.connect()
        sftp = self._client.open_sftp()
        try:
            sftp.get(remote, local)
        finally:
            sftp.close()

    def exists(self, remote_path: str) -> bool:
        code, _, _ = self.run(f"test -e {remote_path}")
        return code == 0

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def from_site(site) -> SSHClient:
    """Build an SSHClient from a SiteConfig."""
    return SSHClient(
        host=site.ssh_host or site.url.split("//")[-1].split("/")[0],
        user=site.ssh_user,
        port=site.ssh_port,
        key_path=site.ssh_key,
        password=site.ssh_pass,
    )
