from typing import Dict, Optional
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from issueforge.bot.messages import (
    build_branch_selection_keyboard,
    build_new_task_keyboard,
    build_question_keyboard,
    build_review_confirmation_keyboard,
    format_branch_selection_message,
    format_new_task_message,
    format_question_message,
    format_review_confirmation_message,
)
from issueforge.core.database import get_task, get_task_questions, list_learnings, list_tasks
from issueforge.core.models import PlatformType, QuestionStatus, TaskStatus, TaskType
from issueforge.core.poller import poller_service
from issueforge.core.queue import task_queue
from issueforge.agents.orchestrator import SupervisoryOrchestrator

router = Router()

# State memory for waiting on user input (task_id -> action)
_user_waiting_state: Dict[int, Dict[str, str]] = {}


@router.message(CommandStart())
async def handle_start(message: Message):
    welcome_text = (
        "🤖 <b>Issueforge: Jetson Nano Orin Git Agent Orchestrator</b>\n\n"
        "I monitor your GitHub/GitLab repositories for Issues, PRs, and Sprint items. "
        "When a new task arrives, I will ask for your approval here, deploy a native codespace on your Jetson Orin, "
        "run our multi-LLM engineering team (Planner, Coder, Tester, Reviewer), and ask for your final confirmation before pushing to Git.\n\n"
        "<b>Available Commands:</b>\n"
        "/tasks - View recent tasks & status\n"
        "/steer - Inject mid-flight directive into active task\n"
        "/learnings - View harness skills & memory\n"
        "/sync - Sync assigned GitLab Todos & GitHub tasks\n"
        "/status - View system status\n"
        "/help - Help & documentation"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@router.message(Command("help"))
async def handle_help(message: Message):
    help_text = (
        "📖 <b>Issueforge Help & Guide</b>\n\n"
        "<b>Workflow:</b>\n"
        "1. Webhook, Poller (/sync), or Manual Task -> Issueforge Ingestion\n"
        "2. Telegram alert with [🚀 Approve & Start] button\n"
        "3. Supervisory Orchestrator triages requirements & poses questions if needed\n"
        "4. Multi-LLM executes inside native Jetson sandbox (~/.issueforge/workspaces)\n"
        "5. Test suite executed & auto-repaired until passing\n"
        "6. Telegram confirmation card with diff and test report\n"
        "7. Dynamic target branch selection -> Pushed to selected branch\n\n"
        "Use /sync to check for new work items.\n"
        "Use /tasks to see current queue.\n"
        "Use /steer &lt;directive&gt; to steer active agent mid-flight.\n"
        "Use /learnings to inspect persistent harness skills."
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("learnings"))
async def handle_learnings(message: Message):
    learnings = await list_learnings(limit=8)
    if not learnings:
        await message.answer("ℹ️ No learned skills recorded in harness memory yet.", parse_mode="HTML")
        return

    lines = ["📚 <b>Harness Memory (Evolved Skills & Learned Patterns):</b>\n"]
    for l in learnings:
        tags_str = f" [<i>{', '.join(l.tags)}</i>]" if l.tags else ""
        lines.append(f"• <b>{l.topic}</b>{tags_str}\n  {l.summary[:120]}...\n")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("tasks"))
async def handle_tasks(message: Message):
    tasks = await list_tasks(limit=10)
    if not tasks:
        await message.answer("ℹ️ No tasks in queue yet.", parse_mode="HTML")
        return

    lines = ["📋 <b>Recent Tasks:</b>\n"]
    for t in tasks:
        icon = {
            TaskStatus.PENDING_APPROVAL: "⏳",
            TaskStatus.PLANNING: "🧠",
            TaskStatus.CODING: "💻",
            TaskStatus.TESTING: "🧪",
            TaskStatus.AWAITING_INPUT: "❓",
            TaskStatus.AWAITING_BRANCH_SELECTION: "🌿",
            TaskStatus.AWAITING_CONFIRMATION: "🔍",
            TaskStatus.COMPLETED: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.REJECTED: "🚫",
        }.get(t.status, "⚙️")

        lines.append(f"{icon} <code>{t.id}</code> - {t.title[:30]}... [<b>{t.status.value}</b>]")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("status"))
async def handle_status(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        task_id = args[1].strip()
        task = await get_task(task_id)
        if not task:
            await message.answer(f"❌ Task <code>{task_id}</code> not found.", parse_mode="HTML")
            return
        from issueforge.bot.messages import format_task_status_message
        await message.answer(format_task_status_message(task), parse_mode="HTML")
        return

    tasks = await list_tasks(limit=100)
    pending = sum(1 for t in tasks if t.status == TaskStatus.PENDING_APPROVAL)
    in_progress = sum(1 for t in tasks if t.status in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.TESTING))
    awaiting_input = sum(1 for t in tasks if t.status in (TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_BRANCH_SELECTION, TaskStatus.AWAITING_CONFIRMATION))
    completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)

    status_text = (
        "⚡ <b>Issueforge System Status (Jetson Orin)</b>\n\n"
        f"⏳ Pending Approvals: <b>{pending}</b>\n"
        f"⚙️ Running in Sandbox: <b>{in_progress}</b>\n"
        f"❓ Awaiting Operator Input / Review: <b>{awaiting_input}</b>\n"
        f"✅ Total Completed: <b>{completed}</b>\n"
        f"📁 Total Tasks Tracked: <b>{len(tasks)}</b>\n\n"
        f"<i>Tip: Use <code>/status &lt;task_id&gt;</code> to view full dossier & run history.</i>"
    )
    await message.answer(status_text, parse_mode="HTML")



@router.message(Command("sync"))
async def handle_sync(message: Message):
    status_msg = await message.answer("🔄 <b>Syncing GitLab Todos & GitHub tasks...</b>", parse_mode="HTML")
    res = await poller_service.sync_all()
    if res.get("success"):
        total = res.get("total_new", 0)
        gl = res.get("gitlab_count", 0)
        gh = res.get("github_count", 0)
        await status_msg.edit_text(
            f"✅ <b>Sync Completed!</b>\n\n"
            f"• Ingested <b>{total}</b> new task(s)\n"
            f"  - GitLab Todos: {gl}\n"
            f"  - GitHub Issues: {gh}\n\n"
            f"Use /tasks to view pending approvals.",
            parse_mode="HTML"
        )
    else:
        err = res.get("error", "Unknown error")
        await status_msg.edit_text(f"❌ <b>Sync Failed:</b> {err}", parse_mode="HTML")


@router.message(Command("steer"))
async def handle_steer(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=2)
    directive = ""
    target_task_id = None

    if len(parts) < 2:
        await message.answer(
            "ℹ️ <b>Usage:</b> <code>/steer &lt;directive&gt;</code> or <code>/steer &lt;task_id&gt; &lt;directive&gt;</code>\n"
            "<i>Example: /steer focus on auth endpoints first</i>",
            parse_mode="HTML"
        )
        return

    recent_tasks = await list_tasks(limit=10)
    first_arg = parts[1]
    matched = next((t for t in recent_tasks if t.id == first_arg or first_arg in t.id), None)
    if matched and len(parts) > 2:
        target_task_id = matched.id
        directive = parts[2].strip()
    else:
        directive = text.split(maxsplit=1)[1].strip()
        active = [t for t in recent_tasks if t.status in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.TESTING)]
        if active:
            target_task_id = active[0].id
        elif recent_tasks:
            target_task_id = recent_tasks[0].id

    if not target_task_id:
        await message.answer("❌ No active or recent tasks to steer.", parse_mode="HTML")
        return

    steered = await task_queue.steer_task(target_task_id, directive)
    if steered:
        await message.answer(
            f"🎯 <b>Co-Pilot Directive Injected!</b>\n\n"
            f"• <b>Task:</b> <code>{steered.id}</code> ({steered.title[:40]}...)\n"
            f"• <b>Directive:</b> <i>{directive}</i>\n\n"
            f"Broadcasted to live terminal and injected into agent memory.",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Failed to inject directive.", parse_mode="HTML")


@router.callback_query(F.data.startswith("approve:"))
async def handle_approve_callback(query: CallbackQuery):
    task_id = query.data.split("approve:")[1]
    task = await get_task(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    await task_queue.approve_task(task_id)
    await query.answer("Task approved! Codespace launched.")
    if query.message:
        await query.message.edit_text(
            f"✅ <b>Task Approved & Launched:</b> <code>{task.id}</code>\n"
            f"<b>Title:</b> {task.title}\n"
            f"Supervisory Orchestrator & agent team executing in Jetson sandbox...",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("reject:"))
async def handle_reject_callback(query: CallbackQuery):
    task_id = query.data.split("reject:")[1]
    task = await get_task(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    await task_queue.reject_task(task_id)
    await query.answer("Task rejected.")
    if query.message:
        await query.message.edit_text(
            f"❌ <b>Task Rejected:</b> <code>{task.id}</code>\n"
            f"<b>Title:</b> {task.title}",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("custom_prompt:"))
async def handle_custom_prompt_callback(query: CallbackQuery):
    task_id = query.data.split("custom_prompt:")[1]
    user_id = query.from_user.id
    _user_waiting_state[user_id] = {"action": "custom_prompt", "task_id": task_id}

    await query.answer()
    if query.message:
        await query.message.reply(
            f"✏️ <b>Custom Instructions:</b>\nPlease reply with your specific instructions for task <code>{task_id}</code>. "
            f"Issueforge will pass them directly to the Orchestrator, Planner & Coder agents.",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirm_callback(query: CallbackQuery):
    task_id = query.data.split("confirm:")[1]
    task = await get_task(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    target = task.selected_target_branch or task.base_branch or "main"
    await query.answer(f"Pushing changes targeting {target}...")
    if query.message:
        await query.message.edit_text(
            f"🚀 <b>Confirming & Pushing:</b> <code>{task.id}</code>\n"
            f"Target branch: <code>{target}</code>\n"
            f"Committing changes, pushing branch <code>{task.working_branch}</code> and creating Pull Request...",
            parse_mode="HTML"
        )

    await task_queue.confirm_and_push(task_id)


@router.callback_query(F.data.startswith("pick_branch:"))
async def handle_pick_branch_callback(query: CallbackQuery):
    task_id = query.data.split("pick_branch:")[1]
    task = await get_task(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    await query.answer()
    candidates = task.target_branch_candidates or ["main", "develop", "master", "staging"]
    kb = build_branch_selection_keyboard(task_id, candidates)
    if query.message:
        await query.message.reply(
            format_branch_selection_message(task),
            reply_markup=kb,
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("branch:"))
async def handle_set_branch_callback(query: CallbackQuery):
    parts = query.data.split(":")
    task_id = parts[1]
    branch = parts[2]
    task = await task_queue.set_target_branch(task_id, branch)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    await query.answer(f"Target branch set to {branch}")
    if query.message:
        # A task parked at the branch gate resumes on this pick, so there is no review to
        # confirm yet — only offer the confirmation card when one actually exists.
        awaiting_review = task.status == TaskStatus.AWAITING_CONFIRMATION
        if awaiting_review:
            await query.message.edit_text(
                f"🌿 <b>Target Branch Updated:</b> <code>{branch}</code> for task <code>{task_id}</code>\n"
                f"You can now confirm and push when ready.",
                parse_mode="HTML"
            )
            kb = build_review_confirmation_keyboard(task_id)
            msg_text = format_review_confirmation_message(task)
            await query.message.reply(msg_text, reply_markup=kb, parse_mode="HTML")
        else:
            await query.message.edit_text(
                f"🌿 <b>Target Branch Set:</b> <code>{branch}</code> for task <code>{task_id}</code>\n"
                f"▶️ The engineering pipeline is now running against this branch.",
                parse_mode="HTML"
            )


@router.callback_query(F.data.startswith("custom_branch:"))
async def handle_custom_branch_callback(query: CallbackQuery):
    task_id = query.data.split("custom_branch:")[1]
    user_id = query.from_user.id
    _user_waiting_state[user_id] = {"action": "custom_branch", "task_id": task_id}
    await query.answer()
    if query.message:
        await query.message.reply(
            f"🌿 <b>Custom Target Branch:</b>\nPlease reply with the exact branch name you want task <code>{task_id}</code> to target.",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("q_ans:"))
async def handle_question_answer_option(query: CallbackQuery):
    parts = query.data.split(":")
    task_id = parts[1]
    q_id = parts[2]
    opt_idx = int(parts[3])

    questions = await get_task_questions(task_id)
    target_q = next((q for q in questions if q.id == q_id), None)
    if not target_q or opt_idx >= len(target_q.options):
        await query.answer("Question or option not found.", show_alert=True)
        return

    chosen = target_q.options[opt_idx]
    await task_queue.submit_question_answer(task_id, q_id, chosen, chosen)
    await query.answer(f"Answer recorded: {chosen}")
    if query.message:
        await query.message.edit_text(
            f"💡 <b>Question Answered:</b> <i>{chosen}</i>\n"
            f"Supervisory Orchestrator resumed pipeline execution...",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("q_custom:"))
async def handle_question_custom_prompt(query: CallbackQuery):
    parts = query.data.split(":")
    task_id = parts[1]
    q_id = parts[2]
    user_id = query.from_user.id
    _user_waiting_state[user_id] = {"action": "question_custom", "task_id": task_id, "question_id": q_id}
    await query.answer()
    if query.message:
        await query.message.reply(
            f"✏️ <b>Custom Answer:</b>\nPlease reply with your answer/guidance for question <code>{q_id}</code> on task <code>{task_id}</code>.",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("revise:"))
async def handle_revise_callback(query: CallbackQuery):
    task_id = query.data.split("revise:")[1]
    user_id = query.from_user.id
    _user_waiting_state[user_id] = {"action": "revise", "task_id": task_id}

    await query.answer()
    if query.message:
        await query.message.reply(
            f"🔄 <b>Revision Notes:</b>\nPlease reply with what you would like the agent team to change or fix for task <code>{task_id}</code>.",
            parse_mode="HTML"
        )


@router.message()
async def handle_user_text_reply(message: Message):
    user_id = message.from_user.id
    user_text = message.text.strip() if message.text else ""
    if not user_text:
        return

    # 1. If user was in an explicit prompt waiting state (e.g. custom branch / revision note)
    if user_id in _user_waiting_state:
        state = _user_waiting_state.pop(user_id)
        action = state.get("action")
        task_id = state.get("task_id")

        if action == "custom_prompt":
            await task_queue.approve_task(task_id, custom_instructions=user_text)
            await message.reply(
                f"✅ <b>Custom Instructions Recorded!</b>\nStarting task <code>{task_id}</code> with your instructions:\n<i>{user_text}</i>",
                parse_mode="HTML"
            )
        elif action == "custom_branch":
            task = await task_queue.set_target_branch(task_id, user_text)
            if task:
                await message.reply(
                    f"🌿 <b>Target Branch Set to:</b> <code>{user_text}</code> for task <code>{task_id}</code>.",
                    parse_mode="HTML"
                )
                if task.status == TaskStatus.AWAITING_CONFIRMATION:
                    kb = build_review_confirmation_keyboard(task_id)
                    msg_text = format_review_confirmation_message(task)
                    await message.reply(msg_text, reply_markup=kb, parse_mode="HTML")
                else:
                    await message.reply(
                        "▶️ The engineering pipeline is now running against this branch.",
                        parse_mode="HTML"
                    )
        elif action == "question_custom":
            q_id = state.get("question_id")
            await task_queue.submit_question_answer(task_id, q_id, user_text)
            await message.reply(
                f"💡 <b>Answer Recorded!</b>\nResuming task <code>{task_id}</code> with guidance:\n<i>{user_text}</i>",
                parse_mode="HTML"
            )
        elif action == "revise":
            await task_queue.request_revision(task_id, revision_notes=user_text)
            await message.reply(
                f"🔄 <b>Revision In Progress!</b>\nAgent team is adjusting code for <code>{task_id}</code> based on:\n<i>{user_text}</i>",
                parse_mode="HTML"
            )
        return

    # 2. Conversational Orchestrator Instruction ("Takes Over" Mode)
    typing_msg = await message.answer("🧠 <b>Supervisory Orchestrator analyzing instruction...</b>", parse_mode="HTML")

    recent_tasks = await list_tasks(limit=10)
    decision = await SupervisoryOrchestrator.interpret_operator_instruction(
        instruction=user_text,
        recent_tasks=recent_tasks
    )

    intent = decision.get("intent", "CREATE_AND_RUN_TASK")
    reply_summary = decision.get("reply_summary", "")

    if intent == "CREATE_AND_RUN_TASK":
        # Resolve target repo & default branch from recent tasks or settings
        default_repo_url = "https://gitlab.com/anujdeulkar/issueforge.git"
        default_platform = PlatformType.GITLAB
        default_base = "main"
        default_candidates = ["main", "v1"]

        if recent_tasks:
            default_repo_url = recent_tasks[0].repo_url
            default_platform = recent_tasks[0].platform
            default_base = recent_tasks[0].base_branch or "main"
            default_candidates = recent_tasks[0].target_branch_candidates or [default_base]

        target_branch = decision.get("target_branch") or default_base
        title = decision.get("task_title") or (user_text[:80] + ("..." if len(user_text) > 80 else ""))
        description = decision.get("task_description") or user_text

        # Create and AUTO-APPROVE task to take over execution!
        new_task = await task_queue.create_and_enqueue_task(
            title=title,
            description=description,
            repo_url=default_repo_url,
            base_branch=default_base,
            selected_target_branch=target_branch,
            target_branch_candidates=default_candidates,
            task_type=TaskType.ISSUE,
            platform=default_platform,
            sender=f"telegram:{message.from_user.username or message.from_user.id}",
            custom_instructions=user_text,
            auto_approve=True
        )

        response_html = (
            f"🚀 <b>Supervisory Orchestrator: Task Initiated & Taking Over!</b>\n\n"
            f"• <b>Task ID:</b> <code>{new_task.id}</code>\n"
            f"• <b>Title:</b> {new_task.title}\n"
            f"• <b>Repository:</b> <code>{new_task.repo_name}</code>\n"
            f"• <b>Target Branch:</b> <code>{new_task.selected_target_branch}</code>\n\n"
            f"🛠️ <i>Codespace workspace deployed on Jetson Orin. Multi-agent team (Planner ➔ Coder ➔ Tester ➔ Reviewer) has taken over execution!</i>\n\n"
            f"{reply_summary}"
        )
        await typing_msg.edit_text(response_html.strip(), parse_mode="HTML")

    elif intent == "TASK_ACTION":
        ref_id = decision.get("referenced_task_id")
        matched_task = None
        if ref_id:
            for t in recent_tasks:
                if t.id == ref_id or ref_id in t.id:
                    matched_task = t
                    break
        if not matched_task and recent_tasks:
            matched_task = recent_tasks[0]

        sub_action = decision.get("task_action")
        if matched_task and sub_action == "APPROVE":
            await task_queue.approve_task(matched_task.id)
            await typing_msg.edit_text(
                f"✅ <b>Orchestrator:</b> Approved and launched codespace for task <code>{matched_task.id}</code> ({matched_task.title}).",
                parse_mode="HTML"
            )
        elif matched_task and sub_action == "CANCEL":
            await task_queue.reject_task(matched_task.id)
            await typing_msg.edit_text(
                f"🛑 <b>Orchestrator:</b> Cancelled task <code>{matched_task.id}</code>.",
                parse_mode="HTML"
            )
        elif matched_task and sub_action == "RETRY":
            await task_queue.retry_task(matched_task.id)
            await typing_msg.edit_text(
                f"🔄 <b>Orchestrator:</b> Triggered new execution run for task <code>{matched_task.id}</code>.",
                parse_mode="HTML"
            )
        elif matched_task and sub_action == "PUSH":
            t_branch = decision.get("target_branch") or matched_task.selected_target_branch
            await task_queue.confirm_and_push(matched_task.id, target_branch=t_branch)
            await typing_msg.edit_text(
                f"🚀 <b>Orchestrator:</b> Pushing changes and opening PR targeting <code>{t_branch}</code> for task <code>{matched_task.id}</code>.",
                parse_mode="HTML"
            )
        else:
            await typing_msg.edit_text(
                f"ℹ️ <b>Orchestrator:</b> {reply_summary or 'No active task found matching the requested action.'}",
                parse_mode="HTML"
            )

    elif intent == "STEER_TASK":
        ref_id = decision.get("referenced_task_id")
        directive = decision.get("steering_directive") or user_text
        matched_task = None
        if ref_id:
            for t in recent_tasks:
                if t.id == ref_id or ref_id in t.id:
                    matched_task = t
                    break
        if not matched_task:
            active = [t for t in recent_tasks if t.status in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.TESTING)]
            if active:
                matched_task = active[0]
            elif recent_tasks:
                matched_task = recent_tasks[0]

        if matched_task:
            steered = await task_queue.steer_task(matched_task.id, directive)
            await typing_msg.edit_text(
                f"🎯 <b>Co-Pilot Steering Directive Injected!</b>\n\n"
                f"• <b>Task:</b> <code>{matched_task.id}</code> ({matched_task.title[:35]}...)\n"
                f"• <b>Directive:</b> <i>{directive}</i>\n\n"
                f"Broadcasted to live terminal and injected into running agent memory.\n"
                f"{reply_summary}",
                parse_mode="HTML"
            )
        else:
            await typing_msg.edit_text(
                f"ℹ️ <b>Orchestrator:</b> No active task found to steer. {reply_summary}",
                parse_mode="HTML"
            )

    elif intent == "STATUS_QUERY":
        active = [t for t in recent_tasks if t.status in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.TESTING)]
        pending = [t for t in recent_tasks if t.status == TaskStatus.PENDING_APPROVAL]
        summary = (
            f"⚡ <b>Orchestrator Status Report</b>\n\n"
            f"• <b>Active in Sandbox:</b> {len(active)}\n"
            f"• <b>Pending Approvals:</b> {len(pending)}\n\n"
            f"{reply_summary}"
        )
        await typing_msg.edit_text(summary.strip(), parse_mode="HTML")

    else:
        await typing_msg.edit_text(
            f"🤖 <b>Supervisory Orchestrator:</b>\n\n{reply_summary or 'I am standing by to receive engineering instructions and deploy codespaces on Jetson Orin.'}",
            parse_mode="HTML"
        )

