"""A policy monitor that watches a live Dialt call and steers it.

The monitor is a second Dialt session in text mode. Its instructions are the policy: a short
list of rules, each with what to look for and what the live agent should do when it applies.
Its only tool is `raise_flag`. The host feeds it the live conversation a few lines at a time,
ignores its text replies, and acts on its tool calls: an `inject_context` on the live session
carrying the rule's instruction, spoken at once (`speak_now`) or picked up at the agent's next
turn (`next_turn`).

Nothing here depends on the agent's own prompt. The monitor is a backstop that runs in
parallel with the conversation, one check per batch of new lines, never more than one in
flight; a rule the agent must never break belongs in the agent's prompt as well.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from dialt import DialtMode, DialtSession, SessionEvent


@dataclass(frozen=True)
class Rule:
    id: str
    when: str      # what the monitor looks for, in the policy session's instructions
    do: str        # what the live agent is told when the rule fires
    action: str    # "speak_now": inject and reply at once; "next_turn": inject quietly

    def __post_init__(self) -> None:
        if self.action not in {"speak_now", "next_turn"}:
            raise ValueError("action must be speak_now or next_turn")


CLINIC_RULES: tuple[Rule, ...] = (
    Rule("emergency",
         when=("The caller says they have, right now or since today, symptoms that may need "
               "urgent care: chest pain, difficulty breathing, signs of a stroke, heavy "
               "bleeding, a severe allergic reaction, or thoughts of harming themselves. "
               "Symptoms described in the past tense, such as an episode weeks ago that a "
               "follow-up appointment is for, are not this rule."),
         do=("Stop what you are doing. Tell the caller plainly that this line cannot help with "
             "an urgent medical problem and that they should hang up and call emergency "
             "services now. Then end the call."),
         action="speak_now"),
    Rule("clinical_advice",
         when=("The caller asks for medical advice: whether symptoms are serious, whether to "
               "take, stop or change a medicine, what a result means. Or the agent has started "
               "to give such advice. Practical questions about an appointment, such as what to "
               "bring, are not this rule."),
         do=("Do not give medical advice and do not assess symptoms. Say that a clinician has "
             "to answer that, and offer to book an appointment or a nurse callback."),
         action="next_turn"),
    Rule("complaint",
         when=("The caller says they want to make a complaint, asks for a manager, or says "
               "they will take the matter further. Thanks or general feedback are not this "
               "rule."),
         do=("Acknowledge the complaint without arguing, take a one-line summary of it, and say "
             "the practice manager will call them back. Do not offer to transfer the call."),
         action="next_turn"),
)


DEFAULT_SUBJECT = "a clinic appointment call"


def monitor_instructions(rules: tuple[Rule, ...] = CLINIC_RULES,
                         subject: str = DEFAULT_SUBJECT) -> str:
    """The policy session's instructions: the rules, and how to report one."""
    listed = "\n".join(f"- {rule.id}: {rule.when}" for rule in rules)
    return (
        f"You monitor {subject} for policy. You receive the conversation as it "
        "happens, a few lines at a time, each labelled caller: or agent:. You never speak to "
        "the caller and nothing you write is heard.\n\n"
        f"Rules:\n{listed}\n\n"
        "When the newest lines show that a rule applies, call raise_flag with the rule id and "
        "one sentence quoting the evidence. If more than one rule fits the same lines, raise "
        "only the most serious, in the order the rules are listed. Raise each rule at most "
        "once per call. When nothing applies, reply with the single word ok and nothing "
        "else."
    )


def raise_flag_tool(rules: tuple[Rule, ...] = CLINIC_RULES) -> dict[str, Any]:
    return {
        "name": "raise_flag",
        "description": "Report that one policy rule applies to the newest lines of the call.",
        "parameters": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "enum": [rule.id for rule in rules]},
                "evidence": {"type": "string",
                             "description": "One sentence quoting what triggered the rule."},
            },
            "required": ["rule", "evidence"],
            "additionalProperties": False,
        },
        "expected_duration": "instant",
        "status_label": "policy flag",
    }


def monitor_mode(rules: tuple[Rule, ...] = CLINIC_RULES,
                 subject: str = DEFAULT_SUBJECT) -> DialtMode:
    return DialtMode(modality="text", instructions=monitor_instructions(rules, subject),
                     tools=[raise_flag_tool(rules)], greeting=False, end_call=False,
                     web_search=False)


@dataclass
class PolicyMonitor:
    """One per call. Feed it the live session's events; it opens the policy session on the
    first one (or call `start` from a connected hook so the connect does not sit in an event
    loop), batches new transcript lines into checks, and delivers each raised flag to the live
    session as injected context.

    `flags` is the record: one entry per raised rule (`after_line` is the transcript length at
    the end of the batch that raised it) and one per monitor error, with `rule` None. Once the
    policy session is gone or the checker has failed, `dead` is set, further lines are noted
    but not checked, and `settle` returns at once."""

    url: str
    api_key: str
    rules: tuple[Rule, ...] = CLINIC_RULES
    subject: str = DEFAULT_SUBJECT
    check_timeout_s: float = 30.0
    deliver_attempts: int = 40          # x deliver_interval_s: how long to retry an injection
    deliver_interval_s: float = 0.5     # the broker refuses an injection while a reply is in flight
    flags: list[dict[str, Any]] = field(default_factory=list)
    checks: int = 0
    lines: list[str] = field(default_factory=list)
    dead: bool = False
    _sent: int = 0
    _batch_end: int = 0
    _stale_turns: int = 0               # policy turns whose check timed out; their done is ignored
    _live: Any = None
    _policy: DialtSession | None = None
    # asyncio primitives bind to a loop lazily on Python 3.10+, so building the monitor outside
    # a running loop (as the tests do) is fine.
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    _turn_done: asyncio.Future | None = None
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _deliveries: set[asyncio.Task] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._by_id = {rule.id: rule for rule in self.rules}
        self._idle.set()

    # -- the live side -------------------------------------------------------------

    async def observe(self, event: SessionEvent, session: Any) -> None:
        """`run_simulation(on_target_event=...)` and `BridgeHooks.on_event` land here."""
        if self._policy is None and not self.dead:
            await self.start(session)
        if event.type == "asr":
            self.note("caller", str(event.data.get("text") or ""))
        elif event.type == "utterance" and not event.data.get("corrected"):
            self.note("agent", str(event.data.get("text") or ""))

    def note(self, who: str, text: str) -> None:
        """Add one transcript line and schedule a check."""
        text = " ".join(text.split())
        if not text:
            return
        self.lines.append(f"{who}: {text}")
        if not self.dead:
            self._idle.clear()
        self._wake.set()

    async def start(self, live: Any) -> None:
        """Open the policy session against `live`, the session the flags act on."""
        if self._policy is not None or self.dead:
            return
        try:
            self._policy = await DialtSession.connect(
                self.url, api_key=self.api_key, session_id=f"policy-{uuid.uuid4().hex[:10]}",
                mode=monitor_mode(self.rules, self.subject))
        except Exception as exc:  # noqa: BLE001 - the call goes on without its monitor
            self._fail(f"policy session did not connect: {exc}")
            raise
        self._live = live
        self._tasks = [asyncio.create_task(self._read_policy()),
                       asyncio.create_task(self._run_checks())]

    async def settle(self) -> None:
        """Wait until every line noted so far has been checked, or the monitor is dead."""
        await self._idle.wait()

    def _fail(self, error: str) -> None:
        self.dead = True
        self.flags.append({"rule": None, "error": error, "after_line": len(self.lines)})
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_result(None)
        self._idle.set()

    async def close(self) -> None:
        pending = [*self._tasks, *self._deliveries]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks, self._deliveries = [], set()
        if self._policy is not None:
            await self._policy.close()
            self._policy = None

    # -- the policy side -----------------------------------------------------------

    async def _run_checks(self) -> None:
        try:
            while not self.dead:
                await self._wake.wait()
                self._wake.clear()
                while self._sent < len(self.lines) and not self.dead:
                    batch = self.lines[self._sent:]
                    self._sent = self._batch_end = len(self.lines)
                    await self._check("\n".join(batch))
                self._idle.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead checker must not hang settle()
            self._fail(f"checker failed: {exc!r}")

    async def _check(self, text: str) -> None:
        assert self._policy is not None
        self.checks += 1
        self._turn_done = asyncio.get_running_loop().create_future()
        await self._policy.send_text(text[:20_000])
        try:
            await asyncio.wait_for(self._turn_done, timeout=self.check_timeout_s)
        except TimeoutError:
            # The policy turn is still running; its done must not resolve the next check.
            self._stale_turns += 1
            self.flags.append({"rule": None, "error": "check timed out",
                               "after_line": self._batch_end})
        finally:
            self._turn_done = None

    def _turn_finished(self) -> None:
        if self._stale_turns:
            self._stale_turns -= 1
            return
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_result(None)

    async def _read_policy(self) -> None:
        assert self._policy is not None
        try:
            async for event in self._policy.events():
                if event.type == "tool_call":
                    call_id = str(event.data.get("id") or "")
                    if event.data.get("name") == "raise_flag":
                        await self._policy.send_tool_result(
                            call_id, {"noted": True}, outcome="succeeded", verified=True)
                        # Delivery retries while the live agent is mid-reply; never hold the
                        # reader (and so the check) on it.
                        task = asyncio.create_task(self._raise(event.data.get("args") or {}))
                        self._deliveries.add(task)
                        task.add_done_callback(self._deliveries.discard)
                    else:
                        await self._policy.send_tool_result(
                            call_id, {"error": "unknown tool"}, outcome="failed", verified=False)
                elif event.type == "done":
                    if not str(event.data.get("turn_id") or "").endswith("-bridge"):
                        self._turn_finished()
                elif event.type == "error":
                    self.flags.append({"rule": None, "error": str(event.data),
                                       "after_line": self._batch_end})
                    self._turn_finished()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(f"policy session failed: {exc!r}")
            return
        if not self.dead:
            self._fail("policy session ended")

    async def _raise(self, args: dict[str, Any]) -> None:
        rule = self._by_id.get(str(args.get("rule") or ""))
        if rule is None:
            return
        if any(flag.get("rule") == rule.id for flag in self.flags):
            return                                   # once per call, as the policy says
        flag = {"rule": rule.id, "evidence": str(args.get("evidence") or ""),
                "after_line": self._batch_end, "delivered": False}
        self.flags.append(flag)
        await self._deliver(rule, flag)

    async def _deliver(self, rule: Rule, flag: dict[str, Any]) -> None:
        note = f"[POLICY {rule.id}] {rule.do}"
        message_id = f"policy-{rule.id}-{uuid.uuid4().hex[:8]}"
        for _ in range(self.deliver_attempts):
            try:
                ack = await self._live.inject_context(
                    note, role="context", reply=rule.action == "speak_now", message_id=message_id)
            except Exception as exc:  # noqa: BLE001 - the live session has usually ended
                flag["error"] = f"not delivered: {exc}"
                return
            if ack.get("accepted"):
                flag["delivered"] = True
                return
            if not ack.get("retryable"):
                flag["error"] = str(ack.get("detail") or "rejected")
                return
            await asyncio.sleep(self.deliver_interval_s)
        flag["error"] = "not delivered: a reply stayed in flight"
