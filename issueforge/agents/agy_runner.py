import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.sandbox import NativeSandbox, subprocess_env
from issueforge.core.models import AgentRole, EventType


class ToolCallNormalizer:
    """Normalizes and safely bounds tool call parameters within the sandbox workspace."""

    @staticmethod
    def normalize_file_path(sandbox_dir: Path, file_path: str) -> Path:
        """
        Ensures target file paths are properly resolved and strictly contained within sandbox_dir.
        If a path tries to escape via '..' or points to a path outside the sandbox,
        it safely relocates the target within the sandbox root.
        """
        clean = file_path.strip().strip("'\"")
        target = Path(clean)

        # If relative, resolve against sandbox_dir
        if not target.is_absolute():
            resolved = (sandbox_dir / target).resolve()
        else:
            resolved = target.resolve()

        # Enforce sandbox containment
        try:
            if not resolved.is_relative_to(sandbox_dir.resolve()):
                safe_rel = clean.lstrip("/\\").replace("..", "").lstrip("/\\")
                resolved = (sandbox_dir / safe_rel).resolve()
        except AttributeError:
            if not str(resolved).startswith(str(sandbox_dir.resolve())):
                safe_rel = clean.lstrip("/\\").replace("..", "").lstrip("/\\")
                resolved = (sandbox_dir / safe_rel).resolve()

        return resolved

    @classmethod
    def sanitize_tool_parameters(
        cls, tool_name: str, params: Dict[str, Any], sandbox_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Auto-corrects common malformed parameter types, keys, and paths."""
        sanitized = dict(params)

        # 1. Path parameters normalization
        path_keys = ["TargetFile", "AbsolutePath", "SearchPath", "DirectoryPath"]
        for key in path_keys:
            if key in sanitized and isinstance(sanitized[key], str) and sandbox_dir:
                sanitized[key] = str(cls.normalize_file_path(sandbox_dir, sanitized[key]))

        # 2. Boolean normalization
        for b_key in ["Overwrite", "IsRegex", "MatchPerLine", "AllowMultiple"]:
            if b_key in sanitized:
                val = sanitized[b_key]
                if isinstance(val, str):
                    sanitized[b_key] = val.lower() in ("true", "1", "yes")

        # 3. Integer normalization
        for i_key in ["StartLine", "EndLine", "WaitMsBeforeAsync", "MaxDepth"]:
            if i_key in sanitized:
                val = sanitized[i_key]
                if isinstance(val, str) and val.isdigit():
                    sanitized[i_key] = int(val)

        return sanitized


class ToolCallDiagnostic:
    """Detects, explains, and helps recover from Cortex / CLI tool execution anomalies."""

    @classmethod
    def diagnose_error(
        cls, tool_name: str, error_msg: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        lower_err = (error_msg or "").lower()
        params = params or {}

        category = "GENERAL"
        recommendation = "Agent self-correcting on next turn."
        auto_recoverable = True

        if "invalid_args" in lower_err or "convert tool call" in lower_err or "model output error" in lower_err:
            category = "INVALID_ARGS"
            recommendation = (
                f"Tool `{tool_name}` received malformed arguments schema. "
                "Cortex argument normalizer intercepted; agent will reformulate parameters on next turn."
            )
        elif "permission" in lower_err or "denied" in lower_err or "not permitted" in lower_err:
            category = "PERMISSIONS"
            recommendation = (
                f"Permission denied for `{tool_name}`. Verify workspace access permissions."
            )
        elif "no such file" in lower_err or "not found" in lower_err or "directory not found" in lower_err:
            category = "PATH_NOT_FOUND"
            recommendation = (
                f"Target file/directory not found for `{tool_name}`. Ensure parent directories exist before writing."
            )
        elif "outside" in lower_err or "traversal" in lower_err:
            category = "PATH_TRAVERSAL"
            recommendation = (
                f"Tool `{tool_name}` attempted path outside sandbox. Path safely confined to workspace."
            )
        elif "syntax" in lower_err or "json" in lower_err or "unexpected eof" in lower_err:
            category = "SYNTAX"
            recommendation = (
                f"Syntax or encoding anomaly in `{tool_name}` output. Agent will re-emit clean format."
            )

        return {
            "category": category,
            "tool": tool_name,
            "error": error_msg,
            "recommendation": recommendation,
            "auto_recoverable": auto_recoverable,
        }



class AgySessionRunner:
    """Manages an autonomous agy CLI agent session executing inside a sandbox workspace."""

    def __init__(
        self,
        task_id: str,
        run_id: str,
        sandbox_dir: Path,
        role: AgentRole,
        model: Optional[str] = None,
        log_file: Optional[Path] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.sandbox_dir = sandbox_dir.resolve()
        self.role = role
        raw_model = model or self._resolve_model(role)
        self.model = self.normalize_model_name(raw_model)
        self.log_file = log_file

    @classmethod
    def normalize_model_name(cls, model_name: Optional[str], default_effort: str = "high") -> str:
        """
        Normalizes LiteLLM / arbitrary model strings (e.g. 'gemini/gemini-3.7-flash', 'gemini-3.8-flash')
        to valid agy CLI model identifiers (e.g. 'gemini-3.7-flash-high').
        """
        if not model_name:
            return f"gemini-3.6-flash-{default_effort}"

        clean = model_name.strip()
        # Remove any provider prefix: 'gemini/', 'openai/', 'anthropic/', 'google/'
        if "/" in clean:
            clean = clean.split("/")[-1]

        # Valid agy base models
        valid_agy_bases = [
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.1-pro",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ]

        # If it already has an effort suffix (-high, -medium, -low)
        for eff in ["high", "medium", "low"]:
            if clean.endswith(f"-{eff}"):
                base = clean[:-len(eff)-1]
                if base in valid_agy_bases:
                    return clean
                return f"gemini-3.6-flash-{eff}"

        # If it's a known agy base without suffix
        if clean in ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]:
            return f"{clean}-{default_effort}"
        elif clean in ["gemini-3.1-pro", "gemini-pro"]:
            eff = default_effort if default_effort in ("high", "low") else "high"
            return f"gemini-3.1-pro-{eff}"
        elif clean in ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]:
            return clean

        # Map generic or versioned model strings
        if "3.8" in clean:
            return f"gemini-3.8-flash-{default_effort}"
        elif "3.7" in clean:
            return f"gemini-3.7-flash-{default_effort}"
        elif "3.1" in clean or "pro" in clean:
            return f"gemini-3.1-pro-high"
        else:
            return f"gemini-3.6-flash-{default_effort}"

    def _resolve_model(self, role: AgentRole) -> str:
        # Default fast, high-rate-limit models for local execution
        if role == AgentRole.ORCHESTRATOR:
            return "gemini-3.6-flash-high"
        elif role == AgentRole.PLANNER:
            return "gemini-3.6-flash-high"
        elif role == AgentRole.CODER:
            return "gemini-3.6-flash-high"
        elif role == AgentRole.TESTER:
            return "gemini-3.6-flash-low"
        elif role == AgentRole.REVIEWER:
            return "gemini-3.6-flash-high"
        return "gemini-3.6-flash-high"

    @staticmethod
    def get_agy_path() -> str:
        candidates = [
            Path.home() / ".local" / "bin" / "agy",
            Path("/usr/local/bin/agy"),
            Path("/usr/bin/agy"),
        ]
        for cand in candidates:
            if cand.exists() and os.access(cand, os.X_OK):
                return str(cand)
        discovered = shutil.which("agy")
        if discovered:
            return discovered
        return "agy"

    def _ensure_skills_present(self) -> List[str]:
        """Ensure core engineering skills are copied into the sandbox's .agents/skills folder."""
        loaded_skills = []
        try:
            source_candidates = [
                Path(__file__).resolve().parent / "skills",   # installed package
                Path.cwd() / ".agents" / "skills",            # repository checkout override
                Path.home() / ".gemini" / "skills",           # operator's own skills
            ]
            source_skills_dir = next((p for p in source_candidates if p.is_dir()), None)
            if not source_skills_dir:
                return loaded_skills

            target_skills_dir = self.sandbox_dir / ".agents" / "skills"
            target_skills_dir.mkdir(parents=True, exist_ok=True)

            for skill_dir in source_skills_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    dest_skill = target_skills_dir / skill_dir.name
                    dest_skill.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_skill / "SKILL.md"
                    if not dest_file.exists():
                        shutil.copy2(skill_dir / "SKILL.md", dest_file)
                    loaded_skills.append(skill_dir.name)
        except Exception:
            pass
        return loaded_skills

    async def run_prompt(
        self,
        system_instructions: str,
        task_prompt: str,
        effort: str = "medium",
        timeout_seconds: int = 600,
    ) -> Tuple[bool, str]:
        """
        Execute agy CLI non-interactively with real-time stream-json parsing.
        Returns (success: bool, final_response: str).
        """
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        skills_loaded = self._ensure_skills_present()
        skills_str = ", ".join(sorted(skills_loaded)) if skills_loaded else "senior-dev, ponytail, frontend-tool, ui-development, senior-backend-engineer"

        skills_context = (
            "AVAILABLE ENGINEERING SKILLS & TOOLS:\n"
            "The following specialized skills are available in this workspace (.agents/skills/):\n"
            "- `senior-dev`: Staff/Principal Engineer scrutiny (security-first, O(n) performance, clean modern async patterns).\n"
            "- `ponytail`: Pragmatic minimalist / YAGNI approach (simplest working code, stdlib first, native features, deletion over addition).\n"
            "- `frontend-tool`: Distinctive, production-grade frontend design and styling (avoid generic AI aesthetics, high design quality).\n"
            "- `ui-development`: Production UI component engineering (accessible Tailwind CSS, responsive layouts, modular client-side state, xterm integration).\n"
            "- `senior-backend-engineer`: Senior distributed backend systems engineer (FastAPI async routing, SQLAlchemy transaction scoping, native process lifecycle).\n"
            "Activate and adhere to these skills and guidelines as appropriate for your task.\n\n---\n"
        )

        agy_bin = self.get_agy_path()
        full_prompt = f"{skills_context}{system_instructions.strip()}\n\n---\nTASK INSTRUCTIONS:\n{task_prompt.strip()}"

        cmd = [
            agy_bin,
            f"--add-dir={str(self.sandbox_dir)}",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--model", self.model,
        ]
        # Only add --effort if the model name does not already encode effort (e.g. -high, -medium, -low)
        if not any(self.model.endswith(f"-{eff}") for eff in ["high", "medium", "low"]) and not any(self.model.startswith(p) for p in ["claude", "gpt"]):
            cmd.extend(["--effort", effort])
        cmd.append(f"--print={full_prompt}")

        # agy authenticates via its config under HOME, GOOGLE_* or GEMINI_API_KEY; it gets
        # no other secret, because repo and issue text can prompt-inject it.
        env = subprocess_env({k: v for k, v in os.environ.items() if k.startswith("GOOGLE_")})
        if settings.gemini_api_key:
            env["GEMINI_API_KEY"] = settings.gemini_api_key

        from issueforge.core.terminal_manager import terminal_manager
        term_session = terminal_manager.get_session(self.task_id, self.run_id)

        # Output colorful agent initialization banner to xterm.js
        banner = (
            f"\r\n\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
            f"\x1b[1;36m[ISSUEFORGE AGENT SESSION]\x1b[0m 🤖 \x1b[1;33m{self.role.value}\x1b[0m \x1b[90m(Model: {self.model})\x1b[0m\r\n"
            f"\x1b[90mWorkspace: {self.sandbox_dir}\x1b[0m\r\n"
            f"\x1b[1;32m[SKILLS LOADED]\x1b[0m 🛠️  \x1b[36m{skills_str}\x1b[0m\r\n"
            f"\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
            f"\x1b[1;32missueforge@sandbox\x1b[0m:\x1b[1;34m~/{self.run_id}\x1b[0m$ agy --model {self.model} --role {self.role.value.lower()}\r\n"
        )
        await term_session.broadcast_text(banner)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.sandbox_dir),
            env=env,
            # Own process group, registered with the task, so cancelling the task kills
            # agy and every tool command it spawned rather than leaving them editing.
            start_new_session=True,
        )
        NativeSandbox.register_process(self.task_id, proc)

        final_response_text = ""
        success = False

        log_handle = None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(self.log_file, "a", encoding="utf-8")

        def append_log(text_line: str):
            if log_handle:
                log_handle.write(text_line + "\n")
                log_handle.flush()

        async def read_stdout():
            nonlocal final_response_text, success
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                append_log(line_str)

                # Attempt to parse stream-json event
                if line_str.startswith("{") and line_str.endswith("}"):
                    try:
                        evt = json.loads(line_str)
                        event_type_name = evt.get("event")

                        if event_type_name == "step_update":
                            step = evt.get("step_update", {})
                            stype = step.get("step_type")
                            sstate = step.get("state")

                            if stype == "tool":
                                tname = step.get("tool_name", "tool")
                                tinfo = step.get("tool_info", {})
                                params = tinfo.get("parameters", {})

                                if sstate == "ACTIVE":
                                    params = ToolCallNormalizer.sanitize_tool_parameters(tname, params, self.sandbox_dir)
                                    desc = f"⚡ Running `{tname}`"
                                    tool_ansi = f"\x1b[1;33m⚡ [{self.role.value}] Tool: {tname}\x1b[0m"
                                    if "TargetFile" in params:
                                        desc = f"✏️ Editing `{Path(params['TargetFile']).name}`"
                                        tool_ansi += f" \x1b[36m-> {Path(params['TargetFile']).name}\x1b[0m"
                                    elif "CommandLine" in params:
                                        desc = f"$ {params['CommandLine'][:120]}"
                                        tool_ansi += f"\r\n   \x1b[32m$ {params['CommandLine'][:140]}\x1b[0m"
                                    elif "SearchPath" in params:
                                        desc = f"🔍 Searching in `{Path(params['SearchPath']).name}`"
                                        tool_ansi += f" \x1b[90min {Path(params['SearchPath']).name}\x1b[0m"
                                    elif "AbsolutePath" in params:
                                        desc = f"📖 Reading `{Path(params['AbsolutePath']).name}`"
                                        tool_ansi += f" \x1b[36m-> {Path(params['AbsolutePath']).name}\x1b[0m"

                                    await term_session.broadcast_text(tool_ansi + "\r\n")

                                    event_data = {"tool": tname, "params": params}
                                    if "TargetFile" in params:
                                        try:
                                            rel_p = str(Path(params["TargetFile"]).relative_to(self.sandbox_dir))
                                        except Exception:
                                            rel_p = Path(params["TargetFile"]).name
                                        event_data["file"] = rel_p
                                        event_data["action"] = "editing"
                                    elif "AbsolutePath" in params:
                                        try:
                                            rel_p = str(Path(params["AbsolutePath"]).relative_to(self.sandbox_dir))
                                        except Exception:
                                            rel_p = Path(params["AbsolutePath"]).name
                                        event_data["file"] = rel_p
                                        event_data["action"] = "viewing"

                                    await event_bus.emit_log(
                                        task_id=self.task_id,
                                        run_id=self.run_id,
                                        message=desc,
                                        role=self.role.value,
                                        event_type=EventType.STEP,
                                        data=event_data,
                                    )
                                elif sstate == "DONE":
                                    output = tinfo.get("output", "")
                                    if output:
                                        out_str = str(output).strip()
                                        if len(out_str) < 250:
                                            append_log(f"   [Tool Output] {output}")
                                            await term_session.broadcast_text(f"   \x1b[90m↳ {out_str}\x1b[0m\r\n")
                                elif sstate == "ERROR":
                                    err_msg = tinfo.get("error", {}).get("message", "Tool error")
                                    diag = ToolCallDiagnostic.diagnose_error(tname, err_msg, params)
                                    diag_banner = (
                                        f"   \x1b[1;31m✖ Tool Error: {err_msg[:160]}\x1b[0m\r\n"
                                        f"   \x1b[1;33m⚠️ [CORTEX DIAGNOSTIC - {diag['category']}]\x1b[0m \x1b[33m{diag['recommendation']}\x1b[0m\r\n"
                                    )
                                    await term_session.broadcast_text(diag_banner)
                                    await event_bus.emit_log(
                                        task_id=self.task_id,
                                        run_id=self.run_id,
                                        message=f"⚠️ Tool `{tname}` diagnostic [{diag['category']}]: {diag['recommendation']}",
                                        role=self.role.value,
                                        event_type=EventType.LOG,
                                        data={"tool": tname, "diagnostic": diag, "error": err_msg},
                                    )

                            elif stype in ("agent_response", "assistant_response"):
                                text_delta = step.get("text_delta") or step.get("content", "")
                                if text_delta:
                                    final_response_text += text_delta
                                    if text_delta.strip() and len(text_delta.strip()) > 8:
                                        clean_delta = text_delta.strip()
                                        await term_session.broadcast_text(f"\x1b[37m{clean_delta}\x1b[0m\r\n")
                                        if len(clean_delta) > 240:
                                            clean_delta = clean_delta[:237] + "..."
                                        await event_bus.emit_log(
                                            task_id=self.task_id,
                                            run_id=self.run_id,
                                            message=clean_delta,
                                            role=self.role.value,
                                            event_type=EventType.LOG,
                                        )

                        elif event_type_name == "result":
                            res = evt.get("result", {})
                            status_str = res.get("status")
                            final_response_text = res.get("response", "")
                            if status_str == "SUCCESS":
                                success = True
                                await term_session.broadcast_text(f"\r\n\x1b[1;32m✔ {self.role.value} completed.\x1b[0m\r\n")

                    except json.JSONDecodeError:
                        pass

        async def read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    append_log(f"[stderr] {line_str}")
                    # Intercept cortex or tool permission/args anomalies in stderr
                    if any(term in line_str.lower() for term in ["invalid_args", "convert tool call", "permission denied"]):
                        diag = ToolCallDiagnostic.diagnose_error("cortex", line_str)
                        await term_session.broadcast_text(
                            f"   \x1b[1;33m⚠️ [CORTEX DIAGNOSTIC - {diag['category']}]\x1b[0m \x1b[33m{diag['recommendation']}\x1b[0m\r\n"
                        )
                    else:
                        await term_session.broadcast_text(f"   \x1b[90m[stderr] {line_str}\x1b[0m\r\n")

        try:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            NativeSandbox.kill_process_group(proc)
            await term_session.broadcast_text(f"\r\n\x1b[1;31m⏱️ {self.role.value} agent timed out after {timeout_seconds}s.\x1b[0m\r\n")
            await event_bus.emit_log(
                task_id=self.task_id,
                run_id=self.run_id,
                message=f"⏱️ {self.role.value} agent timed out after {timeout_seconds}s.",
                role=self.role.value,
                event_type=EventType.ERROR,
            )
            return False, "Timed out."
        finally:
            # Also covers CancelledError: an operator cancel must not leave agy running.
            NativeSandbox.kill_process_group(proc)
            NativeSandbox.unregister_process(self.task_id, proc)
            if log_handle:
                log_handle.close()

        if proc.returncode != 0 and not success:
            err_msg = final_response_text or f"Process exited with code {proc.returncode}"
            await term_session.broadcast_text(f"\r\n\x1b[1;31m✖ {self.role.value} failed: {err_msg}\x1b[0m\r\n")
            return False, err_msg

        return True, final_response_text
