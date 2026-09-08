"""Passing a call from one agent to another on the same Dialt session.

One session, one conversation, no second connection. The first agent finishes its own turn;
then the host declares the second agent in one move (`pass_call_to`): `set_instructions` with
`new_speaker`, which makes the broker fold the call so far into a transcript the new agent
holds rather than turns it spoke; `set_tools`; `set_voice`; and an `inject_context` note that
makes the new agent speak first. The broker refuses the fold while a reply is in flight, so
the pass must land between replies: `HandoffBoundary` reads the session's own `turn`, `done`
and `working` events and says when that moment has come.

Passing the caller to a human is a different operation and never uses this module: a
permission-gated client tool whose host moves the call leg, then `request_wrap_up`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dialt import DialtSession, SessionEvent


async def pass_call_to(session: DialtSession, *, instructions: str, tools: list[dict[str, Any]],
                       note: str, voice: str | None = None) -> dict[str, Any]:
    """Hand the live call to a new agent. Returns the broker's acknowledgement of the note that
    makes the new agent speak; `accepted` false means the note was dropped (a reply was still
    in flight) and the host should send it again after the next `done`."""
    await session.set_instructions(instructions, new_speaker=True)
    await session.set_tools(tools)
    if voice:
        await session.set_voice(voice)
    return await session.inject_context(note, role="context", reply=True)


@dataclass
class HandoffBoundary:
    """When has the turn that carried the hand-off closed?

    Feed every target-session event to `observe`. Set `landed` once the host has resolved the
    hand-off tool. `observe` returns True exactly once: on the first event after which no reply
    is open (`turn` without its `done`) and no tool wait is running (`working`), which covers a
    tool turn that speaks a bridge and then an answer, one that closes after its bridge, and
    one whose answer arrives without a bridge.
    """

    landed: bool = False
    passed: bool = False
    reply_open: bool = False
    working: bool = False
    done_seen: bool = False

    def observe(self, event: SessionEvent) -> bool:
        if event.type == "turn":
            self.reply_open = True
        elif event.type == "done":
            self.reply_open = False
            self.done_seen = self.landed
        elif event.type == "working":
            self.working = bool(event.data.get("active"))
        if not self.landed or self.passed:
            return False
        ready = ((event.type == "done" and not self.working)
                 or (event.type == "working" and not self.working
                     and not self.reply_open and self.done_seen))
        if ready:
            self.passed = True
        return ready
