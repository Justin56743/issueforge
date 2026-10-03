import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from issueforge.config import settings
from issueforge.core.models import Task, TaskStatus, safe_path_id
from issueforge.vault.canvas_builder import CANVAS_FILENAME

logger = logging.getLogger("issueforge.core.task_dossier")


class TaskDossierManager:
    """
    Manages structured task dossier folders on disk at `.issueforge/tasks/{task_id}/`.
    Maintains `task_summary.json`, a human-readable `README.md`, and multi-sandbox subdirectories.
    """

    # ------------------------------------------------------------------ vault helpers

    @staticmethod
    def _slug(value: Any) -> str:
        """Lowercase, filesystem- and wikilink-safe slug."""
        text = re.sub(r"[^a-z0-9/_-]+", "-", str(value or "").lower())
        return re.sub(r"-{2,}", "-", text).strip("-/") or "unknown"

    @classmethod
    def _node(cls, prefix: str, *parts: Any) -> str:
        """Build a flat Obsidian node name.

        Flat on purpose: a "/" inside [[...]] is a vault *path* in Obsidian, so nested names
        would make it auto-create folders and could collide with real files.
        """
        tail = "-".join(cls._slug(part).replace("/", "-") for part in parts if part not in (None, ""))
        return f"{prefix}-{tail}" if tail else prefix

    @staticmethod
    def _escape_wikilinks(text: Any) -> str:
        """Neutralize [[...]] inside operator/LLM-supplied free text.

        Without this, an issue description containing "[[foo]]" would be indexed as a real
        graph edge and would spawn a junk node in the Obsidian graph.
        """
        return str(text or "").replace("[[", "[\u200b[").replace("]]", "]\u200b]")

    @classmethod
    def _tags(cls, task: Task) -> List[str]:
        tags = [
            "issueforge/task",
            f"status/{cls._slug(task.status.value)}",
            f"platform/{cls._slug(task.platform.value)}",
            f"repo/{cls._slug(task.repo_name)}",
        ]
        if task.priority:
            # GitLab scoped labels arrive as "Priority::High" -> "priority/high"
            tags.append(f"priority/{cls._slug(str(task.priority).split('::')[-1])}")
        return tags

    @classmethod
    def _frontmatter(cls, task: Task) -> str:
        """YAML frontmatter block. Built with safe_dump: real titles contain quotes,
        backticks, LaTeX and code fences that break hand-rolled quoting."""
        data: Dict[str, Any] = {
            "task_id": task.id,
            # Single-line: a folded multi-line scalar renders badly in Obsidian properties.
            "title": " ".join(str(task.title or "").split()),
            "repo_name": task.repo_name,
            "repo_url": task.repo_url,
            "platform": task.platform.value,
            "task_type": task.task_type.value,
            "status": task.status.value,
            "active_run": task.active_run_id,
            "base_branch": task.base_branch,
            "selected_target_branch": task.selected_target_branch,
            "candidate_branches": list(task.target_branch_candidates or []),
            "working_branch": task.working_branch,
            "issue_number": task.issue_number,
            "pr_number": task.pr_number,
            "pr_url": task.pr_url,
            "priority": task.priority,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "tags": cls._tags(task),
        }
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        return f"---\n{body}---\n"

    @staticmethod
    def get_task_dir(task_id: str) -> Path:
        settings.ensure_directories()
        return (settings.forge_tasks_root / safe_path_id(task_id)).resolve()

    @classmethod
    def ensure_task_directory(cls, task: Task) -> Path:
        task_dir = cls.get_task_dir(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "sandboxes").mkdir(parents=True, exist_ok=True)
        (task_dir / "metadata").mkdir(parents=True, exist_ok=True)
        return task_dir

    @classmethod
    def write_task_summary(cls, task: Task) -> Path:
        task_dir = cls.ensure_task_directory(task)
        summary_file = task_dir / "task_summary.json"

        summary_data = {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "task_type": task.task_type.value,
            "platform": task.platform.value,
            "repo_url": task.repo_url,
            "repo_name": task.repo_name,
            "base_branch": task.base_branch,
            "working_branch": task.working_branch,
            "selected_target_branch": task.selected_target_branch or task.base_branch,
            "status": task.status.value,
            "active_run_id": task.active_run_id,
            "issue_number": task.issue_number,
            "pr_number": task.pr_number,
            "pr_url": task.pr_url,
            "labels": task.labels,
            "priority": task.priority,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "collaborators": {
                "author": task.collaborators.author if task.collaborators else task.sender,
                "assignees": task.collaborators.assignees if task.collaborators else [],
                "reviewers": task.collaborators.reviewers if task.collaborators else [],
                "participants": task.collaborators.participants if task.collaborators else []
            },
            "subtasks": [
                {
                    "id": s.id,
                    "title": s.title,
                    "completed": s.completed,
                    "created_at": s.created_at.isoformat() if s.created_at else None
                }
                for s in task.subtasks
            ],
            "runs": [
                {
                    "run_id": r.run_id,
                    "attempt_number": r.attempt_number,
                    "status": r.status.value,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "duration_seconds": r.duration_seconds,
                    "failure_stage": r.failure_stage,
                    "error_message": r.error_message,
                    "test_summary": r.test_summary,
                    "diff_stat": r.diff_stat,
                    "commit_hash": r.commit_hash,
                    "sandbox_dir": r.sandbox_dir
                }
                for r in task.runs
            ]
        }

        summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
        return summary_file

    @classmethod
    def write_task_readme(cls, task: Task) -> Path:
        task_dir = cls.ensure_task_directory(task)
        readme_file = task_dir / "README.md"

        target_branch = task.selected_target_branch or task.base_branch or "main"
        created_str = task.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if task.created_at else "N/A"
        author = (task.collaborators.author if task.collaborators and task.collaborators.author else task.sender) or "Unknown"

        # Format Collaborators
        assignees = (task.collaborators.assignees if task.collaborators else None) or []
        participants = (task.collaborators.participants if task.collaborators else None) or []
        assignees_str = ", ".join(assignees) if assignees else "None"
        participants_str = ", ".join(participants) if participants else "None"

        # Format Subtasks Checklist
        subtasks_md = ""
        if task.subtasks:
            subtasks_md = "\n### 📋 Subtasks & Checklist\n\n"
            for st in task.subtasks:
                check = "x" if st.completed else " "
                subtasks_md += f"- [{check}] {cls._escape_wikilinks(st.title)}\n"
        else:
            subtasks_md = "\n### 📋 Subtasks & Checklist\n\n_No subtasks registered yet._\n"

        # Format Pipeline Runs Table
        runs_md = ""
        if task.runs:
            runs_md = "\n### ⏱️ Pipeline Run History\n\n"
            runs_md += "| Run | Attempt | Status | Failure Stage | Duration | Summary / Error |\n"
            runs_md += "| :--- | :---: | :--- | :--- | :---: | :--- |\n"
            for r in task.runs:
                status_badge = "✅ COMPLETED" if r.status == TaskStatus.COMPLETED else ("❌ FAILED" if r.status == TaskStatus.FAILED else f"⏳ {r.status.value}")
                stage = r.failure_stage or "—"
                dur = f"{r.duration_seconds:.1f}s" if r.duration_seconds is not None else "In Progress"
                details = r.error_message or r.test_summary or (r.diff_stat[:60] + "..." if r.diff_stat else "—")
                # sanitize pipes
                details = cls._escape_wikilinks(details).replace("|", "\\|").replace("\n", " ")
                if len(details) > 80:
                    details = details[:77] + "..."
                runs_md += f"| `{r.run_id}` | {r.attempt_number} | {status_badge} | {stage} | {dur} | {details} |\n"
        else:
            runs_md = "\n### ⏱️ Pipeline Run History\n\n_No pipeline runs recorded yet._\n"

        # Format Sandboxes Listing
        sandboxes_md = "\n### 📦 Sandboxes & Workspaces\n\n"
        if task.runs:
            for r in task.runs:
                rel_sb = f"sandboxes/{r.run_id}"
                rel_log = f"metadata/{r.run_id}/sandbox.log"
                sandboxes_md += f"- **Run `{r.run_id}`** (Attempt #{r.attempt_number}): [`{rel_sb}`](./{rel_sb}) | Log: [`{rel_log}`](./{rel_log})\n"
        else:
            sandboxes_md += "_No active sandboxes allocated._\n"

        # Wikilinked entities. Branch and issue nodes are repo-qualified so two repositories'
        # "main" branches do not collapse into one graph node. Most of these resolve to no
        # file on disk, which is intended: graph.json sets hideUnresolved=false, so they
        # render as nodes and are what visually connects tasks that share an issue or branch.
        repo_node = cls._node("Repo", task.repo_name)
        target_node = cls._node("Branch", task.repo_name, target_branch) if target_branch else None
        base_node = cls._node("Branch", task.repo_name, task.base_branch) if task.base_branch else None
        source_node = None
        if task.issue_number:
            source_node = cls._node("Issue", task.platform.value, task.repo_name, task.issue_number)
        elif task.pr_number:
            source_node = cls._node("PR", task.platform.value, task.repo_name, task.pr_number)

        linked_md = f"- **Repository Node:** [[{repo_node}]]\n"
        if source_node:
            linked_md += f"- **Source Work Item:** [[{source_node}]]\n"
        if target_node:
            linked_md += f"- **Target Branch Node:** [[{target_node}]]\n"
        if base_node and base_node != target_node:
            linked_md += f"- **Base Branch Node:** [[{base_node}]]\n"
        linked_md += f"- **Visual Process Graph:** [[{CANVAS_FILENAME}]]\n"
        if (task_dir / "PLAN.md").exists():
            linked_md += "- **Architectural Plan:** [[PLAN]]\n"

        # Lessons this task contributed to the cross-task knowledge vault. These resolve
        # to real notes, so the graph shows which work produced which institutional memory.
        try:
            from issueforge.vault.knowledge_vault import KnowledgeVault

            lesson_nodes = KnowledgeVault().notes_for_task(task.id)
            if lesson_nodes:
                linked_md += "- **Lessons Learned:** " + " ".join(f"[[{n}]]" for n in lesson_nodes) + "\n"
        except Exception:
            pass

        people_nodes = []
        for person in [author] + list(assignees) + list(participants):
            if not person or person == "Unknown":
                continue
            node = cls._node("Person", str(person).lstrip("@"))
            if node not in people_nodes:
                people_nodes.append(node)
        if people_nodes:
            linked_md += "- **People:** " + " ".join(f"[[{n}]]" for n in people_nodes) + "\n"

        content = f"""{cls._frontmatter(task)}
# Task Dossier: {task.title}

> **Task ID:** `{task.id}`  
> **Status:** `{task.status.value}`  
> **Created:** `{created_str}`  

---

## 📌 Overview

- **Repository:** [{task.repo_name}]({task.repo_url})
- **Platform:** `{task.platform.value}` ({task.task_type.value})
- **Base Branch:** `{task.base_branch}`
- **Target Branch:** `{target_branch}`
- **Working Branch:** `{task.working_branch}`
- **Pull/Merge Request:** {task.pr_url or "_Pending_"}

### Description
{cls._escape_wikilinks(task.description)}

---

## 🔗 Linked Nodes

{linked_md}
---

## 👥 People & Collaborators

- **Author / Reporter:** `{author}`
- **Assignees:** {assignees_str}
- **Discussion Participants:** {participants_str}

---
{subtasks_md}
---
{runs_md}
---
{sandboxes_md}
"""

        readme_file.write_text(content, encoding="utf-8")
        return readme_file

    @classmethod
    def write_plan_file(cls, task: Task) -> Optional[Path]:
        """Mirror the task plan into the dossier as a linkable PLAN.md note."""
        if not task.plan:
            return None
        task_dir = cls.ensure_task_directory(task)
        plan_file = task_dir / "PLAN.md"
        header = yaml.safe_dump(
            {"task_id": task.id, "tags": ["issueforge/plan"]},
            sort_keys=False, allow_unicode=True, default_flow_style=False,
        )
        plan_file.write_text(
            f"---\n{header}---\n\n# Architectural Plan: {task.title}\n\n{task.plan}\n",
            encoding="utf-8",
        )
        return plan_file

    @classmethod
    def ensure_canvas(cls, task: Task) -> Optional[Path]:
        """Create the task's execution DAG canvas if it does not exist yet.

        Uses ensure_initialized rather than initialize_task_dag: this runs on every dossier
        sync (many times per run) and a rebuild would wipe operator-added steering cards.
        """
        if not settings.forge_canvas_enabled:
            return None
        try:
            from issueforge.vault.canvas_builder import CanvasDAGBuilder

            task_dir = cls.ensure_task_directory(task)
            builder = CanvasDAGBuilder(task_dir / CANVAS_FILENAME, task.id)
            builder.ensure_initialized(task.title)
            return builder.canvas_path
        except Exception as exc:
            logger.debug("Canvas bootstrap skipped for %s: %s", task.id, exc)
            return None

    @classmethod
    def sync_dossier(cls, task: Task) -> Path:
        """Ensure folder exists, write summary JSON, PLAN.md, canvas and markdown README.

        Stays synchronous and filesystem-only. The engine calls this at roughly sixteen
        points per run, so the async graph indexing lives in sync_dossier_async instead.
        """
        cls.ensure_task_directory(task)
        cls.write_task_summary(task)
        cls.write_plan_file(task)
        cls.ensure_canvas(task)
        cls.write_task_readme(task)
        return cls.get_task_dir(task.id)

    @classmethod
    async def sync_dossier_async(cls, task: Task) -> Path:
        """sync_dossier plus wikilink graph indexing. Awaited only at real state changes."""
        task_dir = cls.sync_dossier(task)
        from issueforge.vault.linker import sync_task_graph

        await sync_task_graph(task)
        return task_dir

    @classmethod
    def get_task_dossier(cls, task_id: str) -> Optional[Dict[str, Any]]:
        """Load the task summary JSON if it exists."""
        summary_file = cls.get_task_dir(task_id) / "task_summary.json"
        if summary_file.exists():
            try:
                return json.loads(summary_file.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    @classmethod
    def get_task_readme(cls, task_id: str) -> Optional[str]:
        """Return raw content of README.md for this task folder."""
        readme_file = cls.get_task_dir(task_id) / "README.md"
        if readme_file.exists():
            try:
                return readme_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    @classmethod
    def list_run_files(cls, task_id: str, run_id: str, max_files: int = 100) -> List[str]:
        """List files in the specific sandbox run workspace."""
        run_dir = cls.get_task_dir(task_id) / "sandboxes" / safe_path_id(run_id)
        if not run_dir.exists():
            return []
        import os
        ignore_dirs = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
        files = []
        for root, dirs, filenames in os.walk(run_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            rel_root = Path(root).relative_to(run_dir)
            for f in filenames:
                if f.startswith("."):
                    continue
                rel_path = (rel_root / f).as_posix()
                if rel_path != ".":
                    files.append(rel_path)
                if len(files) >= max_files:
                    return files
        return files

    @classmethod
    def get_run_file_tree(cls, task_id: str, run_id: str) -> List[Dict[str, Any]]:
        """Return structured tree with metadata and git status for all files in the sandbox."""
        run_dir = (cls.get_task_dir(task_id) / "sandboxes" / safe_path_id(run_id)).resolve()
        if not run_dir.exists():
            return []

        import os
        import subprocess

        # Check git status if git repo exists to detect modified / added files
        modified_files = set()
        untracked_files = set()
        try:
            git_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=5
            )
            if git_status.returncode == 0:
                for line in git_status.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    status_code = line[:2].strip()
                    file_name = line[3:].strip()
                    if status_code in ("M", "MM", "AM"):
                        modified_files.add(file_name)
                    elif status_code in ("??", "A"):
                        untracked_files.add(file_name)
        except Exception:
            pass

        ignore_dirs = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
        file_items = []

        for root, dirs, filenames in os.walk(run_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
            rel_root = Path(root).relative_to(run_dir)
            for f in sorted(filenames):
                if f.startswith("."):
                    continue
                full_path = Path(root) / f
                rel_path = (rel_root / f).as_posix() if str(rel_root) != "." else f
                file_stat = full_path.stat()

                status = "clean"
                if rel_path in untracked_files:
                    status = "added"
                elif rel_path in modified_files:
                    status = "modified"

                file_items.append({
                    "path": rel_path,
                    "name": f,
                    "extension": full_path.suffix.lstrip(".").lower() or "txt",
                    "size": file_stat.st_size,
                    "modified_at": file_stat.st_mtime,
                    "status": status,
                })
        return file_items

    @classmethod
    def get_sandbox_file_content(cls, task_id: str, run_id: str, relative_path: str) -> Dict[str, Any]:
        """Safely fetch file content ensuring no directory traversal."""
        run_dir = (cls.get_task_dir(task_id) / "sandboxes" / safe_path_id(run_id)).resolve()
        target = (run_dir / relative_path).resolve()
        if not target.is_relative_to(run_dir):
            raise PermissionError("Access outside sandbox directory is prohibited.")
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"File '{relative_path}' not found in sandbox.")

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            return {
                "path": relative_path,
                "name": target.name,
                "content": content,
                "size": target.stat().st_size
            }
        except Exception as e:
            raise RuntimeError(f"Failed to read file: {e}")

    @classmethod
    def save_sandbox_file_content(cls, task_id: str, run_id: str, relative_path: str, content: str) -> bool:
        """Safely write updated file content ensuring no directory traversal."""
        run_dir = (cls.get_task_dir(task_id) / "sandboxes" / safe_path_id(run_id)).resolve()
        target = (run_dir / relative_path).resolve()
        if not target.is_relative_to(run_dir):
            raise PermissionError("Access outside sandbox directory is prohibited.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True


    @classmethod
    def list_all_dossiers(cls) -> List[Dict[str, Any]]:
        """List summary dossiers for all task folders stored on disk."""
        settings.ensure_directories()
        tasks_root = settings.forge_tasks_root.resolve()
        dossiers = []
        if not tasks_root.exists():
            return dossiers

        for item in sorted(tasks_root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            if item.is_dir() and not item.name.startswith("."):
                dossier = cls.get_task_dossier(item.name)
                if dossier:
                    dossiers.append(dossier)
                else:
                    dossiers.append({
                        "id": item.name,
                        "title": item.name,
                        "status": "UNKNOWN",
                        "folder_path": str(item),
                        "runs": []
                    })
        return dossiers

