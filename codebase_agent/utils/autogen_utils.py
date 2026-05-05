"""
Utility functions for handling AutoGen responses and common operations.
"""

import asyncio
import inspect

from autogen_core import CancellationToken


def extract_text_from_autogen_response(response) -> str:
    """
    Extract text content from various AutoGen response objects.

    Handles TaskResult, ChatMessage, and other AutoGen response types uniformly.

    Args:
        response: AutoGen response object (TaskResult, ChatMessage, etc.) or string

    Returns:
        str: Extracted text content
    """
    if isinstance(response, str):
        return response

    # Handle TaskResult object
    if hasattr(response, "messages") and len(response.messages) > 0:
        last_message = response.messages[-1]
        if hasattr(last_message, "content"):
            return last_message.content
        else:
            return str(last_message)

    # Handle ChatMessage or other response objects
    if hasattr(response, "chat_message"):
        chat_msg = response.chat_message
        if hasattr(chat_msg, "content"):
            return chat_msg.content
        elif hasattr(chat_msg, "to_text"):
            return chat_msg.to_text()
        else:
            return str(chat_msg)

    # Handle direct content attribute
    if hasattr(response, "content"):
        return response.content

    # Fallback to string conversion
    return str(response)


def run_assistant_single_turn(agent, task: str):
    """
    Run one AssistantAgent task after clearing accumulated chat context.

    AutoGen's AssistantAgent stores prior user/assistant messages in ``model_context``
    across ``run()`` calls. This analyzer passes prior tool output inside each new
    prompt, so letting history accumulate duplicates content and exhausts context
    windows.

    Returns:
        TaskResult from ``agent.run``.
    """

    async def _once():
        on_reset = getattr(agent, "on_reset", None)
        if callable(on_reset) and inspect.iscoroutinefunction(on_reset):
            await on_reset(CancellationToken())
        return await agent.run(task=task)

    return asyncio.run(_once())
