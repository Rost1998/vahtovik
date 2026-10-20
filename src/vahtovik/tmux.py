"""The tmux session where every running group session has a window of its own."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config

OPTION = "@vahtovik"  # window option that names the group: <watch>/<key>


class TmuxError(Exception):
    pass


@dataclass(frozen=True)
class Window:
    id: str  # tmux window id, like @12
    ref: str  # <watch>/<key>
    clients: int  # how many terminals show this window now


class Tmux:
    use_systemd_run = True  # tests turn this off

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = cfg.tmux.session
        self.base = ["tmux", *(["-L", cfg.tmux.socket] if cfg.tmux.socket else [])]

    @staticmethod
    def installed() -> bool:
        return shutil.which("tmux") is not None

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run([*self.base, *args], capture_output=True, text=True)
        except OSError as exc:
            raise TmuxError(f"cannot run tmux: {exc}") from None
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def _has_session(self) -> bool:
        return self._run("has-session", "-t", f"={self.session}", check=False).returncode == 0

    def ensure_session(self) -> None:
        """Create the session if it is missing. Its first window shows the journal."""
        if self._has_session():
            return
        new = [*self.base, "new-session", "-d", "-s", self.session, "-n", "journal", "-x", "200", "-y", "50",
               "-c", str(self.cfg.root), "-e", f"VAHTOVIK_HOME={self.cfg.root}",
               "--", sys.executable, "-m", "vahtovik", "log", "-f"]
        server_running = self._run("list-sessions", check=False).returncode == 0
        if not server_running and self.use_systemd_run and shutil.which("systemd-run"):
            # A tmux server started by a run belongs to the run's systemd service, and systemd kills it
            # when the run ends, together with any tmux session of yours that joined it meanwhile.
            # A scope of its own keeps it alive.
            if subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--", *new],
                              capture_output=True).returncode == 0:
                return
        proc = subprocess.run(new, capture_output=True, text=True)
        if proc.returncode != 0 and not self._has_session():  # another process may have created it meanwhile
            raise TmuxError(f"cannot create tmux session {self.session}: {proc.stderr.strip()}")

    def windows(self) -> list[Window]:
        """vahtovik's group session windows."""
        if not self.installed():
            return []
        proc = self._run("list-windows", "-t", f"={self.session}",
                         "-F", f"#{{window_id}}\t#{{{OPTION}}}\t#{{window_active_clients}}", check=False)
        if proc.returncode != 0:  # no server, or no session
            return []
        found = []
        for line in proc.stdout.splitlines():
            window_id, ref, clients = line.split("\t")
            if ref:
                found.append(Window(window_id, ref, int(clients or 0)))
        return found

    def window(self, ref: str) -> Window | None:
        return next((w for w in self.windows() if w.ref == ref), None)

    def new_window(self, ref: str, command: list[str], cwd: Path) -> str:
        self.ensure_session()
        window_id = self._run("new-window", "-d", "-t", f"={self.session}:", "-n", ref, "-c", str(cwd),
                              "-e", f"VAHTOVIK_HOME={self.cfg.root}", "-P", "-F", "#{window_id}",
                              "--", *command).stdout.strip()
        self._run("set-option", "-w", "-t", window_id, OPTION, ref, check=False)
        return window_id

    def capture(self, target: str) -> str:
        """The text on the screen of a window or pane."""
        return self._run("capture-pane", "-p", "-t", target, check=False).stdout

    def send_keys(self, window_id: str, keys: list[str]) -> None:
        self._run("send-keys", "-t", window_id, *keys)

    def close(self, window_id: str) -> None:
        self._run("kill-window", "-t", window_id, check=False)

    def attach(self, window_id: str) -> bool:
        """Show the window in this terminal. Inside tmux on the same server, switch to it and return True
        at once; otherwise attach and return False when the person detaches."""
        self._run("select-window", "-t", window_id)
        inside = os.environ.get("TMUX", "").split(",")[0]
        if inside and inside == self._run("display-message", "-p", "-t", window_id, "#{socket_path}").stdout.strip():
            self._run("switch-client", "-t", window_id)
            return True
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}  # attach from inside another server too
        subprocess.run([*self.base, "attach-session", "-t", window_id], env=env)
        return False
