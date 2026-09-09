"""The small application seam around Dialt's atomic agent hand-off."""
from __future__ import annotations

from typing import Any

from dialt import DialtSession

HANDOFF_COMPLETE_CONTEXT = "The agent handoff is complete."


async def pass_call_to(session: DialtSession, *, instructions: str, tools: list[dict[str, Any]],
                       context: str, voice: str | None = None,
                       operation_id: str | None = None,
                       opener: str | None = HANDOFF_COMPLETE_CONTEXT) -> dict[str, Any]:
    """Replace the active agent, then ask the replacement to reply.

    ``handoff_agent`` owns the outgoing-turn boundary and atomically folds history with the
    incoming configuration, including the incoming agent's context. A separate acknowledged
    lifecycle injection asks the new agent to reply. Its rejection, for example because the
    caller claimed the floor, never changes the fact that the handoff applied and is not retried.
    This helper is stateless, so a session may be passed through more than one agent in sequence.
    """
    handoff = await session.handoff_agent(
        instructions=instructions, tools=tools, voice=voice, context=context,
        operation_id=operation_id,
    )
    if not handoff.get("accepted") or handoff.get("status") != "applied":
        return {"accepted": False, "status": "rejected", "switched": False,
                "handoff": handoff, "reply": None, "reply_started": False}
    result = {"accepted": True, "status": "applied", "switched": True,
              "handoff": handoff, "reply": None, "reply_started": False}
    if opener is None:
        return result
    try:
        reply = await session.inject_context(opener, role="context", reply=True)
    except Exception as exc:  # The switch is applied even if the optional opener cannot be sent.
        result["opener_error"] = str(exc) or type(exc).__name__
        return result
    result["reply"] = reply
    result["reply_started"] = bool(reply.get("reply_started"))
    return result
