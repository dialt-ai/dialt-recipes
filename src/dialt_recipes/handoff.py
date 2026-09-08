"""Passing a call from one agent to another on the same Dialt session.

One session, one conversation, no second connection. The first agent finishes its own turn;
then the host declares the second agent in one move (`pass_call_to`): `set_instructions` with
`new_speaker`, which makes the broker fold the call so far into a transcript the new agent
holds rather than turns it spoke; `set_tools`; optionally a forced first tool; `set_voice`; and
an `inject_context` note that makes the new agent speak first. The broker refuses the fold while
a reply is in flight, so the pass must land between replies: `HandoffBoundary` reads the
session's own `turn`, `done` and `working` events and says when that moment has come, and when
the new agent's first reply has closed so a forced first tool can be released.

Passing the caller to a human is a different operation and never uses this module: a
permission-gated client tool whose host moves the call leg, then `request_wrap_up`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from dialt import DialtSession, SessionEvent

Phase = Literal["pass", "release"]


async def pass_call_to(session: DialtSession, *, instructions: str, tools: list[dict[str, Any]],
                       note: str, voice: str | None = None,
                       first_tool: str | None = None) -> dict[str, Any]:
    """Hand the live call to a new agent.

    `first_tool` names a tool the new agent's first turn must call (`tool_choice`), for an agent
    whose first act is a lookup that everything else depends on: without it the model sometimes
    stated account facts it had never fetched (loan-servicing dev runs, 2026-09-08). The choice
    is durable, so release it with `session.set_tool_choice("auto")` when `HandoffBoundary`
    reports the first reply closed. Returns the broker's acknowledgement of the note that makes
    the new agent speak; `accepted` false means it was dropped (a reply was still in flight) and
    the host should send it again after the next `done`."""
    await session.set_instructions(instructions, new_speaker=True)
    await session.set_tools(tools)
    if first_tool:
        await session.set_tool_choice({"tool": first_tool})
    if voice:
        await session.set_voice(voice)
    return await session.inject_context(note, role="context", reply=True)


@dataclass
class HandoffBoundary:
    """When has the turn that carried the hand-off closed, and when has the new agent's first
    reply closed?

    Feed every target-session event to `observe`. Set `landed` once the host has resolved the
    hand-off tool. `observe` returns "pass" exactly once: on the first event after which no reply
    is open (`turn` without its `done`) and no tool wait is running (`working`), which covers a
    tool turn that speaks a bridge and then an answer, one that closes after its bridge, and one
    whose answer arrives without a bridge. It returns "release" exactly once more, by the same
    rule, when the new agent's first reply has closed. Otherwise None.
    """

    landed: bool = False
    passed: bool = False
    released: bool = False
    reply_open: bool = False
    working: bool = False
    done_seen: bool = False

    def observe(self, event: SessionEvent) -> Phase | None:
        if event.type == "turn":
            self.reply_open = True
        elif event.type == "done":
            self.reply_open = False
            self.done_seen = self.landed
        elif event.type == "working":
            self.working = bool(event.data.get("active"))
        if not self.landed or self.released:
            return None
        settled = ((event.type == "done" and not self.working)
                   or (event.type == "working" and not self.working
                       and not self.reply_open and self.done_seen))
        if not settled:
            return None
        if not self.passed:
            self.passed = True
            self.done_seen = False       # the next closed turn is the new agent's first reply
            return "pass"
        self.released = True
        return "release"
