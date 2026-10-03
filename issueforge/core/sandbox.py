import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import EventType


class CommandResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str, timed_out: bool = False, duration: float = 0.0):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration = duration

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def __repr__(self) -> str:
        return f"<CommandResult exit_code={self.exit_code} success={self.success} timed_out={self.timed_out} duration={self.duration:.2f}s>"


def bytecode_free_env(sandbox: "NativeSandbox") -> Dict[str, str]:
    """Env that keeps `compileall` from littering __pycache__ inside the git workspace.

    Without this, bytecode dirs land in the repo and `git add -A` sweeps them into the
    commit — and they can satisfy the pipeline's "diff is non-empty" guard on their own.
    """
    return {"PYTHONPYCACHEPREFIX": str(sandbox.meta_dir / "pycache")}


class NativeSandbox:
    """Manages isolated native filesystem workspaces and secure subprocess execution on Jetson Orin."""

    # Every live subprocess per task: agy sessions and sandbox commands can overlap (a
    # dashboard diff request runs git while an agent works), and cancel must reach all.
    _active_processes: Dict[str, Set[asyncio.subprocess.Process]] = {}

    def __init__(self, task_id: str, run_id: Optional[str] = None):
        self.task_id = task_id
        self.run_id = run_id or "run-1"
        settings.ensure_directories()
        self.task_dir = (settings.forge_tasks_root / task_id).resolve()
        self.workspace_path = self.task_dir / "sandboxes" / self.run_id
        self.meta_dir = self.task_dir / "metadata" / self.run_id
        self.log_file = self.meta_dir / "sandbox.log"
        self.history_file = self.meta_dir / "execution_history.json"


    @classmethod
    def register_process(cls, task_id: str, proc: asyncio.subprocess.Process) -> None:
        cls._active_processes.setdefault(task_id, set()).add(proc)

    @classmethod
    def unregister_process(cls, task_id: str, proc: asyncio.subprocess.Process) -> None:
        procs = cls._active_processes.get(task_id)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                del cls._active_processes[task_id]

    @staticmethod
    def kill_process_group(proc: asyncio.subprocess.Process) -> bool:
        """Kill a process started with start_new_session=True and everything it spawned."""
        if proc.returncode is not None:
            return False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                break
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True

    @classmethod
    def terminate_task_process(cls, task_id: str) -> bool:
        """Immediately terminate every active subprocess running for task_id."""
        killed = False
        for proc in list(cls._active_processes.get(task_id, ())):
            killed = cls.kill_process_group(proc) or killed
        return killed

    def setup(self) -> Path:
        """Create the workspace and metadata directories if they don't exist and inject agent skills."""
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._inject_agent_skills()
        return self.workspace_path

    def _inject_agent_skills(self) -> None:
        """Ensure core engineering skills (.agents/skills) are available in the sandbox workspace."""
        try:
            source_candidates = [
                Path(__file__).resolve().parent.parent / "agents" / "skills",   # installed package
                Path.cwd() / ".agents" / "skills",                              # repository checkout override
                Path.home() / ".gemini" / "skills",                             # operator's own skills
            ]
            source_skills_dir = next((p for p in source_candidates if p.is_dir()), None)
            if not source_skills_dir:
                return

            target_skills_dir = self.workspace_path / ".agents" / "skills"
            target_skills_dir.mkdir(parents=True, exist_ok=True)

            for skill_dir in source_skills_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    dest_skill = target_skills_dir / skill_dir.name
                    dest_skill.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_skill / "SKILL.md"
                    if not dest_file.exists():
                        shutil.copy2(skill_dir / "SKILL.md", dest_file)
        except Exception as e:
            logger.warning(f"Failed to inject agent skills into sandbox {self.sandbox_id}: {e}")

    def cleanup(self) -> None:
        """Remove the workspace directory, metadata, and their contents."""
        if self.workspace_path.exists():
            shutil.rmtree(self.workspace_path, ignore_errors=True)
        if self.meta_dir.exists():
            shutil.rmtree(self.meta_dir, ignore_errors=True)


    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a relative file path safely inside the workspace."""
        clean_rel = relative_path.lstrip("/\\")
        target = (self.workspace_path / clean_rel).resolve()
        # Ensure path stays within workspace to prevent path traversal
        if not str(target).startswith(str(self.workspace_path)):
            raise ValueError(f"Security: Path '{relative_path}' traverses outside workspace directory.")
        return target

    def read_file(self, relative_path: str) -> str:
        """Read content of a file in the workspace."""
        target = self.resolve_path(relative_path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, relative_path: str, content: str) -> None:
        """Write content to a file in the workspace, creating parent dirs if needed."""
        target = self.resolve_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def file_exists(self, relative_path: str) -> bool:
        """Check if file exists in workspace."""
        try:
            return self.resolve_path(relative_path).exists()
        except ValueError:
            return False

    def list_files(self, max_depth: int = 5, max_files: int = 200) -> List[str]:
        """List relative file paths in the workspace, excluding hidden and VCS dirs."""
        ignore_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "target", "dist", "build"}
        files = []
        
        if not self.workspace_path.exists():
            return files

        for root, dirs, filenames in os.walk(self.workspace_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            rel_root = Path(root).relative_to(self.workspace_path)
            depth = len(rel_root.parts)
            if depth > max_depth:
                continue

            for f in filenames:
                if f.startswith("."):
                    continue
                rel_file = (rel_root / f).as_posix()
                if rel_file != ".":
                    files.append(rel_file)
                if len(files) >= max_files:
                    return files
        return files

    def _append_to_file(self, path: Path, text: str) -> None:
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception:
            pass

    async def run_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        env_vars: Optional[Dict[str, str]] = None,
        emit_events: bool = True
    ) -> CommandResult:
        """Execute a shell command inside the workspace directory, streaming output in real-time."""
        if not self.workspace_path.exists():
            self.setup()

        timeout_sec = timeout or settings.forge_execution_timeout_seconds
        start_time = time.time()
        start_iso = datetime.now(timezone.utc).isoformat()

        # Build clean environment
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        if emit_events:
            await event_bus.emit_log(
                task_id=self.task_id,
                message=f"$ {command}",
                event_type=EventType.STEP,
                data={"command": command}
            )

        # Write command start header directly to sandbox.log
        self._append_to_file(self.log_file, f"[{start_iso}] $ {command}\n")

        exit_code = 1
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        timed_out = False
        streamed_sse_count = 0
        MAX_SSE_LINES = 200

        async def read_stream(stream: Optional[asyncio.StreamReader], is_stderr: bool, acc: List[str]):
            nonlocal streamed_sse_count
            if not stream:
                return
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line_str = line_bytes.decode("utf-8", errors="replace")
                acc.append(line_str)
                # Immediately record line to sandbox.log on disk
                prefix = "  [stderr] " if is_stderr else "  "
                self._append_to_file(self.log_file, f"{prefix}{line_str}")

                clean_line = line_str.rstrip("\r\n")
                if clean_line and emit_events:
                    if streamed_sse_count < MAX_SSE_LINES:
                        streamed_sse_count += 1
                        await event_bus.emit_log(
                            task_id=self.task_id,
                            message=clean_line,
                            event_type=EventType.LOG,
                            data={"stream": "stderr" if is_stderr else "stdout"}
                        )
                    elif streamed_sse_count == MAX_SSE_LINES:
                        streamed_sse_count += 1
                        await event_bus.emit_log(
                            task_id=self.task_id,
                            message="... [Live stream rate-limited; full logs available in Raw sandbox.log tab] ...",
                            event_type=EventType.LOG
                        )

        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True
            )
            NativeSandbox.register_process(self.task_id, process)

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(process.stdout, False, stdout_lines),
                        read_stream(process.stderr, True, stderr_lines),
                        process.wait()
                    ),
                    timeout=float(timeout_sec)
                )
                exit_code = process.returncode if process.returncode is not None else 0
            except asyncio.TimeoutError:
                timed_out = True
                exit_code = 124
                NativeSandbox.kill_process_group(process)
                msg = f"Command timed out after {timeout_sec}s: {command}"
                stderr_lines.append(msg + "\n")
                self._append_to_file(self.log_file, f"  [timeout] {msg}\n")
                if emit_events:
                    await event_bus.emit_log(
                        task_id=self.task_id,
                        message=msg,
                        event_type=EventType.ERROR
                    )
            except asyncio.CancelledError:
                NativeSandbox.kill_process_group(process)
                self._append_to_file(self.log_file, f"[{datetime.now(timezone.utc).isoformat()}] Status: CANCELLED\n{'-' * 80}\n")
                raise

        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_msg = f"Failed to execute command '{command}': {str(e)}"
            stderr_lines.append(err_msg + "\n")
            exit_code = 1
            self._append_to_file(self.log_file, f"  [error] {err_msg}\n")
            if emit_events:
                await event_bus.emit_log(
                    task_id=self.task_id,
                    message=err_msg,
                    event_type=EventType.ERROR
                )
        finally:
            if process is not None:
                NativeSandbox.unregister_process(self.task_id, process)

        duration = time.time() - start_time
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        res = CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration=duration)

        # Append execution summary footer to sandbox.log
        status_str = "TIMEOUT" if timed_out else ("SUCCESS" if exit_code == 0 else f"FAILED (exit {exit_code})")
        end_iso = datetime.now(timezone.utc).isoformat()
        self._append_to_file(
            self.log_file,
            f"[{end_iso}] Status: {status_str} | Duration: {duration:.2f}s\n{'-' * 80}\n"
        )

        # Append structured entry to execution_history.json
        self._append_execution_history(command, exit_code, timed_out, duration, stdout, stderr, start_iso)

        return res

    def _append_execution_history(
        self, command: str, exit_code: int, timed_out: bool, duration: float, stdout: str, stderr: str, timestamp_iso: str
    ) -> None:
        """Write structured execution object to execution_history.json."""
        try:
            entry = {
                "command": command,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": round(duration, 3),
                "timestamp": timestamp_iso,
                "stdout_preview": stdout[:2000] if stdout else "",
                "stderr_preview": stderr[:2000] if stderr else "",
            }
            history: List[Dict[str, Any]] = []
            if self.history_file.exists():
                try:
                    history = json.loads(self.history_file.read_text(encoding="utf-8"))
                except Exception:
                    history = []
            history.append(entry)
            self.history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_sandbox_log(self) -> str:
        """Return the complete execution log for this sandbox."""
        if self.log_file.exists():
            return self.log_file.read_text(encoding="utf-8", errors="replace")
        return "No execution stream recorded in this sandbox yet."

    def get_execution_history(self) -> List[Dict[str, Any]]:
        """Return the structured execution history list for this sandbox."""
        if self.history_file.exists():
            try:
                return json.loads(self.history_file.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def get_stats(self) -> Dict[str, Any]:
        """Return metadata, disk usage, and command count for this sandbox workspace."""
        exists = self.workspace_path.exists()
        size_bytes = 0
        file_count = 0
        mtime = None
        if exists:
            try:
                mtime = datetime.fromtimestamp(self.workspace_path.stat().st_mtime, tz=timezone.utc).isoformat()
            except Exception:
                pass
            for root, _, files in os.walk(self.workspace_path):
                for f in files:
                    fp = Path(root) / f
                    try:
                        size_bytes += fp.stat().st_size
                        file_count += 1
                    except Exception:
                        pass

        history = self.get_execution_history()
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "workspace_path": str(self.workspace_path),
            "exists": exists,
            "size_bytes": size_bytes,
            "size_formatted": self._format_bytes(size_bytes),
            "file_count": file_count,
            "command_count": len(history),
            "has_log": self.log_file.exists(),
            "last_modified": mtime
        }

    def delete_workspace(self) -> bool:
        """Completely remove the sandbox workspace directory and metadata from disk."""
        deleted = False
        if self.workspace_path.exists():
            shutil.rmtree(self.workspace_path, ignore_errors=True)
            deleted = True
        if self.meta_dir.exists():
            shutil.rmtree(self.meta_dir, ignore_errors=True)
            deleted = True
        return deleted

    @staticmethod
    def _format_bytes(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @classmethod
    def list_all_sandboxes(cls) -> List[Dict[str, Any]]:
        """List all active or ended sandboxes stored on disk across tasks and runs."""
        settings.ensure_directories()
        tasks_root = settings.forge_tasks_root.resolve()
        sandboxes = []
        if tasks_root.exists():
            for task_dir in sorted(tasks_root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
                if task_dir.is_dir() and not task_dir.name.startswith("."):
                    sb_dir = task_dir / "sandboxes"
                    if sb_dir.exists():
                        for run_dir in sorted(sb_dir.iterdir()):
                            if run_dir.is_dir():
                                sb = cls(task_dir.name, run_id=run_dir.name)
                                sandboxes.append(sb.get_stats())
                    else:
                        sb = cls(task_dir.name)
                        sandboxes.append(sb.get_stats())

        # Also support legacy workspaces root if present
        ws_root = settings.forge_workspace_root.resolve()
        if ws_root.exists() and ws_root != tasks_root:
            for item in sorted(ws_root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
                if item.is_dir() and not item.name.startswith(".") and not any(s["task_id"] == item.name for s in sandboxes):
                    sb = cls(item.name)
                    sandboxes.append(sb.get_stats())

        return sandboxes

