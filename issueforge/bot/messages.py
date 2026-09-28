from typing import List, Optional
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from issueforge.core.models import Task, TaskQuestion, TaskStatus


def format_new_task_message(task: Task) -> str:
    desc_preview = task.description.strip()
    if len(desc_preview) > 300:
        desc_preview = desc_preview[:300] + "..."

    return (
        f"🚨 <b>New Work Item Ingested!</b>\n\n"
        f"<b>Repository:</b> <code>{task.repo_name}</code>\n"
        f"<b>Title:</b> {task.title}\n"
        f"<b>Type:</b> <code>{task.task_type.value}</code> | <b>Platform:</b> <code>{task.platform.value}</code>\n"
        f"<b>Base Branch:</b> <code>{task.base_branch}</code>\n"
        f"<b>Task ID:</b> <code>{task.id}</code>\n\n"
        f"<b>Description:</b>\n<i>{desc_preview}</i>\n\n"
        f"Would you like Issueforge to launch the codespace on your Jetson Orin and implement this?"
    )


def build_new_task_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Approve & Start", callback_data=f"approve:{task_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{task_id}")
            ],
            [
                InlineKeyboardButton(text="✏️ Custom Instructions", callback_data=f"custom_prompt:{task_id}")
            ]
        ]
    )


def format_question_message(task: Task, question: TaskQuestion) -> str:
    ctx = f"\n\n<b>Context:</b> <i>{question.context}</i>" if question.context else ""
    return (
        f"❓ <b>Supervisory Orchestrator Question:</b> <code>{task.id}</code>\n\n"
        f"<b>Task:</b> {task.title}\n"
        f"<b>Question:</b>\n<b>{question.question}</b>"
        f"{ctx}\n\n"
        f"Please select an option below or reply with a custom message:"
    )


def build_question_keyboard(task_id: str, question: TaskQuestion) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(question.options[:4]):
        short_text = opt if len(opt) <= 30 else opt[:27] + "..."
        buttons.append([InlineKeyboardButton(text=f"👉 {short_text}", callback_data=f"q_ans:{task_id}:{question.id}:{idx}")])

    buttons.append([InlineKeyboardButton(text="✏️ Custom Text Reply", callback_data=f"q_custom:{task_id}:{question.id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_branch_selection_message(task: Task) -> str:
    target = task.selected_target_branch or task.base_branch or "main"
    return (
        f"🌿 <b>Select Target Branch:</b> <code>{task.id}</code>\n\n"
        f"<b>Current Target:</b> <code>{target}</code>\n"
        f"Which branch should these changes merge into?"
    )


def build_branch_selection_keyboard(task_id: str, candidates: List[str]) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for b in candidates[:6]:
        row.append(InlineKeyboardButton(text=f"🌿 {b}", callback_data=f"branch:{task_id}:{b}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✏️ Custom Branch Name", callback_data=f"custom_branch:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_review_confirmation_message(task: Task) -> str:
    summary_text = task.review_summary.summary if task.review_summary else "Code changes completed."
    risk = task.review_summary.risk_assessment if task.review_summary else "Low"
    test_verification = (
        task.review_summary.test_verification
        if (task.review_summary and task.review_summary.test_verification)
        else (task.test_summary or "Tests executed.")
    )

    diff_stat = task.diff_stat or "No stat available"
    if len(diff_stat) > 400:
        diff_stat = diff_stat[:400] + "..."

    diff_snippet = ""
    if task.diff_content:
        clean_diff = task.diff_content.strip()
        if len(clean_diff) > 500:
            clean_diff = clean_diff[:500] + "\n... (diff truncated)"
        diff_snippet = f"\n\n<b>Diff Snippet:</b>\n<pre>{clean_diff}</pre>"

    target_branch = task.selected_target_branch or task.base_branch or "main"

    return (
        f"🔍 <b>Ready for Confirmation:</b> <code>{task.id}</code>\n\n"
        f"<b>Task:</b> {task.title}\n"
        f"<b>Repo:</b> <code>{task.repo_name}</code> (Branch: <code>{task.working_branch}</code>)\n"
        f"<b>Target Branch:</b> <code>{target_branch}</code>\n"
        f"<b>Risk Level:</b> <code>{risk}</code>\n\n"
        f"<b>Executive Summary:</b>\n{summary_text}\n\n"
        f"<b>Test Verification:</b>\n{test_verification}\n\n"
        f"<b>Diff Stats:</b>\n<code>{diff_stat}</code>"
        f"{diff_snippet}\n\n"
        f"Approve to commit, push to remote, and open Pull Request targeting <code>{target_branch}</code>?"
    )


def build_review_confirmation_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm & Push PR", callback_data=f"confirm:{task_id}"),
                InlineKeyboardButton(text="🌿 Change Branch", callback_data=f"pick_branch:{task_id}")
            ],
            [
                InlineKeyboardButton(text="🔄 Request Revision", callback_data=f"revise:{task_id}"),
                InlineKeyboardButton(text="🛑 Discard Changes", callback_data=f"reject:{task_id}")
            ]
        ]
    )


def format_task_status_message(task: Task) -> str:
    target = task.selected_target_branch or task.base_branch or "main"
    author = (task.collaborators.author if task.collaborators and task.collaborators.author else task.sender) or "Unknown"
    created_str = task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "N/A"

    # Subtasks
    subtasks_str = "None"
    if task.subtasks:
        completed = sum(1 for s in task.subtasks if s.completed)
        subtasks_str = f"{completed}/{len(task.subtasks)} completed"

    # Runs History
    runs_lines = []
    if task.runs:
        for r in task.runs:
            icon = "✅" if r.status == TaskStatus.COMPLETED else ("❌" if r.status == TaskStatus.FAILED else "⏳")
            dur = f"{r.duration_seconds:.1f}s" if r.duration_seconds is not None else "Active"
            runs_lines.append(f"  • Run #{r.attempt_number} (<code>{r.run_id}</code>): {icon} <b>{r.status.value}</b> ({dur})")
    runs_str = "\n".join(runs_lines) if runs_lines else "  _No runs recorded yet._"

    return (
        f"📋 <b>Task Dossier:</b> <code>{task.id}</code>\n\n"
        f"<b>Title:</b> {task.title}\n"
        f"<b>Status:</b> <code>{task.status.value}</code>\n"
        f"<b>Author:</b> <code>{author}</code>\n"
        f"<b>Target Branch:</b> <code>{target}</code>\n"
        f"<b>Checklist:</b> <code>{subtasks_str}</code>\n"
        f"<b>Created:</b> <code>{created_str}</code>\n\n"
        f"<b>⏱️ Pipeline Runs ({len(task.runs)}):</b>\n"
        f"{runs_str}"
    )


def format_task_completed_message(task: Task) -> str:
    target = task.selected_target_branch or task.base_branch or "main"
    return (
        f"🎉 <b>Task Completed & Pushed!</b>\n\n"
        f"<b>Task ID:</b> <code>{task.id}</code>\n"
        f"<b>Title:</b> {task.title}\n"
        f"<b>Target Branch:</b> <code>{target}</code>\n"
        f"<b>Pull/Merge Request:</b> <a href=\"{task.pr_url}\">{task.pr_url}</a>\n\n"
        f"All test suites passed and code review criteria met."
    )


def format_task_failure_message(task: Task) -> str:
    active_run = task.active_run_id or "run-1"
    runs_count = len(task.runs)
    return (
        f"❌ <b>Task Pipeline Execution Failed</b>\n\n"
        f"<b>Task ID:</b> <code>{task.id}</code> (Attempt #{runs_count}, <code>{active_run}</code>)\n"
        f"<b>Title:</b> {task.title}\n"
        f"<b>Error:</b> <code>{task.error_message or 'Unknown error occurred'}</code>\n\n"
        f"The failed sandbox run is preserved for inspection. Inspect the web dashboard or use /status {task.id}."
    )


