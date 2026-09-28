import asyncio
import fcntl
import json
import logging
import os
import struct
import termios
from pathlib import Path
from typing import Dict, List, Optional, Set
from fastapi import WebSocket

from issueforge.config import settings

logger = logging.getLogger(__name__)


class TerminalSession:
    """Represents an active or historical terminal session for a sandbox run."""

    def __init__(self, task_id: str, run_id: str):
        self.task_id = task_id
        self.run_id = run_id
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.subproc_pid: Optional[int] = None
        self.websockets: Set[WebSocket] = set()
        self.history_buffer = bytearray()
        self._raw_file_path = self._resolve_raw_path()
        self._load_persisted_history()

    def _resolve_raw_path(self) -> Path:
        raw_path = (settings.forge_tasks_root / self.task_id / "metadata" / self.run_id / "terminal.raw").resolve()
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        return raw_path

    def _load_persisted_history(self) -> None:
        if self._raw_file_path.exists():
            try:
                content = self._raw_file_path.read_bytes()
                self.history_buffer.extend(content)
            except Exception as e:
                logger.warning("Failed to load persisted terminal history for %s/%s: %s", self.task_id, self.run_id, e)

    def append_output(self, data: bytes) -> None:
        """Append raw ANSI bytes to in-memory buffer and persist to disk."""
        if not data:
            return
        self.history_buffer.extend(data)
        try:
            with open(self._raw_file_path, "ab") as f:
                f.write(data)
                f.flush()
        except Exception as e:
            logger.debug("Failed to write terminal raw chunk to disk: %s", e)

    def append_text(self, text: str) -> None:
        """Convenience method to write text (auto-converting \n to \r\n for raw terminal display)."""
        formatted = text.replace("\r\n", "\n").replace("\n", "\r\n")
        self.append_output(formatted.encode("utf-8", errors="replace"))

    async def broadcast_bytes(self, data: bytes) -> None:
        """Broadcast raw bytes to all connected WebSockets and save to scrollback history."""
        self.append_output(data)
        disconnected = set()
        for ws in list(self.websockets):
            try:
                await ws.send_bytes(data)
            except Exception:
                disconnected.add(ws)
        self.websockets.difference_update(disconnected)

    async def broadcast_text(self, text: str) -> None:
        """Broadcast ANSI formatted text."""
        formatted = text.replace("\r\n", "\n").replace("\n", "\r\n")
        await self.broadcast_bytes(formatted.encode("utf-8", errors="replace"))

    def set_window_size(self, cols: int, rows: int) -> bool:
        """Set PTY window size using TIOCSWINSZ ioctl."""
        if self.master_fd is not None and self.master_fd >= 0:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
                return True
            except Exception as e:
                logger.debug("Failed to set window size on master_fd %s: %s", self.master_fd, e)
        return False

    def write_input(self, data: bytes) -> bool:
        """Send keyboard input from user WebSocket to master PTY."""
        if self.master_fd is not None and self.master_fd >= 0:
            try:
                os.write(self.master_fd, data)
                return True
            except Exception as e:
                logger.debug("Failed to write user input to master_fd %s: %s", self.master_fd, e)
        return False

    def get_history(self) -> bytes:
        return bytes(self.history_buffer)



class TerminalSessionManager:
    """Central manager for active and historical terminal sessions across tasks and runs."""

    def __init__(self):
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    def _make_key(self, task_id: str, run_id: str) -> str:
        return f"{task_id}:{run_id}"

    def get_session(self, task_id: str, run_id: str) -> TerminalSession:
        key = self._make_key(task_id, run_id)
        if key not in self._sessions:
            self._sessions[key] = TerminalSession(task_id, run_id)
        return self._sessions[key]

    async def register_websocket(self, task_id: str, run_id: str, ws: WebSocket) -> TerminalSession:
        session = self.get_session(task_id, run_id)
        session.websockets.add(ws)
        # Rehydrate client with existing scrollback buffer immediately
        history = session.get_history()
        if history:
            try:
                await ws.send_bytes(history)
            except Exception:
                pass
        return session

    def unregister_websocket(self, task_id: str, run_id: str, ws: WebSocket) -> None:
        key = self._make_key(task_id, run_id)
        if key in self._sessions:
            self._sessions[key].websockets.discard(ws)

    def get_raw_history(self, task_id: str, run_id: str) -> bytes:
        session = self.get_session(task_id, run_id)
        return session.get_history()


class InteractiveShellSession:
    """Manages an active interactive bash shell running inside a sandbox directory."""

    def __init__(self, task_id: str, run_id: str, cwd: Path):
        self.task_id = task_id
        self.run_id = run_id
        self.cwd = cwd
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.proc: Optional[Any] = None
        self.websockets: Set[WebSocket] = set()
        self._read_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        import pty
        import subprocess

        self.cwd.mkdir(parents=True, exist_ok=True)
        try:
            self.master_fd, self.slave_fd = pty.openpty()
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            env["PS1"] = r"\[\033[1;32m\]issueforge@sandbox\[\033[0m\]:\[\033[1;34m\]\w\[\033[0m\]\$ "

            self.proc = subprocess.Popen(
                ["/bin/bash", "--login"],
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                cwd=str(self.cwd),
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            # Close slave fd in parent process
            try:
                os.close(self.slave_fd)
            except Exception:
                pass
            self.slave_fd = None
            self._running = True

            try:
                loop = asyncio.get_running_loop()
                self._read_task = loop.create_task(self._reader_loop())
            except RuntimeError:
                pass
        except Exception as e:
            logger.error("Failed to start InteractiveShellSession: %s", e)
            self.stop()

    async def _reader_loop(self) -> None:
        while self._running and self.master_fd is not None:
            try:
                data = await asyncio.to_thread(os.read, self.master_fd, 4096)
                if not data:
                    break
                disconnected = set()
                for ws in list(self.websockets):
                    try:
                        await ws.send_bytes(data)
                    except Exception:
                        disconnected.add(ws)
                self.websockets.difference_update(disconnected)
            except OSError:
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Shell reader error: %s", e)
                break
        self.stop()

    def write_input(self, data: bytes) -> bool:
        if self.master_fd is not None and self.master_fd >= 0:
            try:
                os.write(self.master_fd, data)
                return True
            except Exception as e:
                logger.debug("Failed to write to interactive shell master_fd: %s", e)
        return False

    def set_window_size(self, cols: int, rows: int) -> bool:
        if self.master_fd is not None and self.master_fd >= 0:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
                return True
            except Exception as e:
                logger.debug("Failed to set window size on shell master_fd: %s", e)
        return False

    def stop(self) -> None:
        self._running = False
        if self.proc:
            try:
                import signal
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.proc = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()


class InteractiveShellManager:
    """Central manager for interactive bash shells in sandbox workspaces."""

    def __init__(self):
        self._shells: Dict[str, InteractiveShellSession] = {}
        self._lock = asyncio.Lock()

    def _make_key(self, task_id: str, run_id: str) -> str:
        return f"{task_id}:{run_id}"

    async def get_or_create_shell(self, task_id: str, run_id: str, cwd: Path) -> InteractiveShellSession:
        key = self._make_key(task_id, run_id)
        async with self._lock:
            shell = self._shells.get(key)
            if shell is None or not shell._running:
                shell = InteractiveShellSession(task_id, run_id, cwd)
                shell.start()
                self._shells[key] = shell
            return shell


terminal_manager = TerminalSessionManager()
shell_manager = InteractiveShellManager()
