import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
import litellm

from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType

logger = logging.getLogger(__name__)


def setup_llm_api_keys() -> None:
    """Ensure API keys from settings are loaded into environment variables for litellm."""
    if settings.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    if settings.deepseek_api_key:
        os.environ["DEEPSEEK_API_KEY"] = settings.deepseek_api_key


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the first JSON object in an LLM response, fenced or bare.

    Returns None rather than raising: every caller treats "no JSON" as a
    recoverable outcome and falls back to its own heuristics.
    """
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except Exception:
        return None


async def call_llm_with_fallback(
    primary_model: str,
    messages: List[Dict[str, Any]],
    task_id: Optional[str] = None,
    role: Optional[str] = None,
    run_id: Optional[str] = None,
    **kwargs: Any
) -> Any:
    """
    Call litellm.acompletion with automatic sequential fallback across configured models.
    If the primary model is unavailable, deprecated, or rate-limited, it automatically
    tries fallback models and notifies the event bus.
    """
    setup_llm_api_keys()

    # Build candidate model list with primary first, followed by fallbacks without duplicates
    candidates: List[str] = [primary_model]
    for fb in settings.llm_fallback_models:
        if fb and fb not in candidates:
            candidates.append(fb)

    last_exception: Optional[Exception] = None

    valid_role = role if (role and role in [r.value for r in AgentRole]) else None

    for i, model in enumerate(candidates):
        try:
            logger.info("Calling LLM model: %s (attempt %d/%d)", model, i + 1, len(candidates))
            if task_id:
                clean_name = model.split("/")[-1]
                await event_bus.emit_log(
                    task_id=task_id,
                    message=f"🧠 Querying {clean_name}...",
                    role=valid_role,
                    event_type=EventType.LOG,
                    data={"model": model},
                    run_id=run_id
                )
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                **kwargs
            )
            if task_id:
                clean_name = model.split("/")[-1]
                if i > 0:
                    await event_bus.emit_log(
                        task_id=task_id,
                        message=f"🔄 Successfully recovered using fallback model: `{clean_name}`",
                        role=valid_role,
                        event_type=EventType.LOG,
                        data={"fallback_model": model, "original_model": primary_model},
                        run_id=run_id
                    )
                else:
                    await event_bus.emit_log(
                        task_id=task_id,
                        message=f"✨ Received response from {clean_name}",
                        role=valid_role,
                        event_type=EventType.LOG,
                        data={"model": model},
                        run_id=run_id
                    )
            return response
        except Exception as e:
            last_exception = e
            err_str = str(e)
            is_quota_exhausted = "429" in err_str or "quota" in err_str.lower() or "ResourceExhausted" in type(e).__name__
            
            # Check if all remaining candidate models belong to the same exhausted provider (e.g. gemini)
            provider_prefix = model.split("/")[0] if "/" in model else model.split("-")[0]
            remaining_candidates = candidates[i + 1:]
            same_provider_remaining = [m for m in remaining_candidates if m.startswith(provider_prefix) or provider_prefix in m]

            if is_quota_exhausted and len(same_provider_remaining) == len(remaining_candidates) and len(remaining_candidates) > 0:
                warning_msg = f"⚠️ Model `{model}` quota exhausted ({err_str[:120]}). Provider quota exhausted; aborting repetitive fallback cascade."
                logger.warning("Quota exhausted for %s. Aborting fallback cascade.", provider_prefix)
                if task_id:
                    await event_bus.emit_log(
                        task_id=task_id,
                        message=warning_msg,
                        role=valid_role,
                        event_type=EventType.LOG,
                        data={"failed_model": model, "error": err_str, "quota_exhausted": True},
                        run_id=run_id
                    )
                break

            next_model = candidates[i + 1] if i + 1 < len(candidates) else None
            warning_msg = f"⚠️ Model `{model}` failed: {err_str[:150]}"
            if next_model:
                warning_msg += f". Falling back to `{next_model}`..."
            
            logger.warning("LLM model %s failed: %s", model, err_str)
            if task_id:
                await event_bus.emit_log(
                    task_id=task_id,
                    message=warning_msg,
                    role=valid_role,
                    event_type=EventType.LOG,
                    data={"failed_model": model, "error": err_str, "next_model": next_model},
                    run_id=run_id
                )

    if last_exception:
        raise last_exception
    raise RuntimeError("No LLM models available to execute completion.")
