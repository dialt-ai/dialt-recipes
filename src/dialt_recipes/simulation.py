from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

import httpx
from dialt import DialtMode, DialtSession, SessionEvent
from dialt.evals import EvalsError, validate_case
from dialt.policy import PolicyEvidence
from dialt.relay import (
    SIMULATION_SILENCE_END_S,
    SIMULATION_SILENCE_NUDGE_S,
    TextTurnRelay,
    VoiceTurnRelay,
)

Fixture = Any | Callable[[dict[str, Any]], Any | Awaitable[Any]]

# The same case document the hosted evals API accepts: run it here, push it unchanged.
# A tool turn can commit as a bridge line and then its final answer; both carry the same turn id
# root (suffixes -bridge / -final / -degraded / -recovery-final). One conversational turn.
_TURN_SUFFIX = re.compile(r"-(?:bridge-)?(?:bridge|final|degraded|recovery-final)$")


def turn_root(turn_id) -> str | None:
    if not isinstance(turn_id, str) or not turn_id:
        return None
    return _TURN_SUFFIX.sub("", turn_id)


def _complete_text_relay(relay: TextTurnRelay, event: SessionEvent) -> None:
    """Flush only completed text replies, not a client-tool bridge's cover line.

    The following final utterance replaces the relay's pending bridge text and its ``done``
    flushes that final to the other simulated participant. This is deliberately side-agnostic:
    either participant can use a client tool.
    """
    if str(event.data.get("turn_id") or "").endswith("-bridge"):
        return
    relay.done()


def assistant_turns(transcript: list[dict]) -> int:
    """Conversational assistant turns: entries sharing a turn root count once."""
    roots, count = set(), 0
    for turn in transcript:
        if turn.get("role") != "assistant":
            continue
        root = turn.get("turn")
        if root is None:
            count += 1
        elif root not in roots:
            roots.add(root)
            count += 1
    return count


LEGACY_KEYS = ("target_instructions", "simulator_instructions", "target_tools", "expected")
MAX_FIXTURE_FIELD_VALUE_CHARS = 20_000


@dataclass(frozen=True)
class SimulationCase:
    name: str
    starter: str                 # "" when target.greeting opens the conversation
    # Session mode documents, exactly as a start frame carries them (DialtMode.to_wire), so
    # every session option is a case option. The run owns a few fields: see session_mode.
    target: dict[str, Any] = field(default_factory=dict)
    simulator: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Fixture] = field(default_factory=dict)
    checks: tuple[dict[str, Any], ...] = ()
    max_turns: int = 20
    timeout_s: float = 600.0
    silence_s: float = 30.0

    @property
    def target_instructions(self) -> str:
        return str(self.target.get("instructions") or "")

    @property
    def target_tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.target.get("tools") or ())

    @property
    def simulator_instructions(self) -> str:
        return str(self.simulator.get("instructions") or "")

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, modality: str | None = None) -> "SimulationCase":
        """Build a case from the hosted case document.

        ``name``, ``starter``, ``target`` and ``simulator`` (each a session mode document:
        ``instructions``, ``greeting``, ``tools``, ``voice``, ``web_search``, ``end_call`` and
        every other session option), ``fixtures``, ``checks`` and ``limits`` (``max_turns``,
        ``timeout_s``, ``silence_s``). Validation is the SDK's ``validate_case``: the hosted
        rules and messages.

        A case opens with exactly one of ``starter`` (the simulated user speaks first) and
        ``target.greeting`` (the target opens, as a deployed agent with a fixed greeting does).
        ``target.end_call`` (default true) gives the agent the managed ``end_call`` tool, the
        only way it can end the conversation. The simulated user always has it.
        """
        stale = [key for key in LEGACY_KEYS if key in value]
        if stale:
            raise ValueError(
                f"{', '.join(stale)}: cases use the hosted shape (target.instructions, "
                "target.tools, simulator.instructions, checks, limits); see the evals guide")
        value = validate_case(value, modality=modality)   # the hosted rules and messages
        limits = value["limits"]
        return cls(
            name=str(value["name"]),
            starter=str(value["starter"]),
            target=dict(value.get("target") or {}),
            simulator=dict(value.get("simulator") or {}),
            fixtures=dict(value.get("fixtures") or {}),
            checks=tuple(value["checks"]),
            max_turns=int(limits["max_turns"]),
            timeout_s=float(limits["timeout_s"]),
            silence_s=float(limits["silence_s"]),
        )


@dataclass
class SimulationReport:
    case_name: str
    modality: str
    target_session_id: str
    simulator_session_id: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    fixture_state: dict[str, dict[str, str]] = field(default_factory=dict)
    termination_reason: str = "completed"
    error: str = ""
    check_results: list[dict[str, Any]] = field(default_factory=list)

    def assistant_text(self) -> str:
        return "\n".join(
            turn["text"] for turn in self.transcript if turn["role"] == "assistant")

    @property
    def passed(self) -> bool:
        """No error, an end the agent is not to blame for, and every check that ran here
        passed. The simulated user ending the conversation or going quiet is named in the
        termination reason, but the checks decide. Judge checks only run hosted (they need the
        judge model); they are reported as skipped, not as failures."""
        return (
            not self.error
            and self.termination_reason in {
                "completed", "max_turns", "simulator_ended", "simulator_silent"}
            and all(result["pass"] for result in self.check_results if not result.get("skipped"))
        )


def evaluate_checks(case: SimulationCase, report: SimulationReport) -> list[dict[str, Any]]:
    """The deterministic checks, with the hosted runner's semantics. Judge checks are skipped."""
    results: list[dict[str, Any]] = []
    assistant = report.assistant_text()
    transcript = "\n".join(f"{turn['role']}: {turn['text']}" for turn in report.transcript)
    called = {
        str(event.get("name"))
        for event in report.events
        if event.get("side") == "target" and event.get("type") == "tool_call"
    }
    policy_evidence = PolicyEvidence()
    for event in report.events:
        policy_evidence.record(event)
    for index, check in enumerate(case.checks):
        kind = check.get("type")
        name = check.get("name") or f"{kind}-{index + 1}"
        if kind == "judge":
            results.append({
                "type": kind, "name": name, "criterion": check.get("criterion"),
                "pass": None, "skipped": True, "detail": "judge checks run hosted",
            })
            continue
        value = str(check.get("value") or "")
        passed, detail = False, ""
        if kind == "contains":
            passed = value.casefold() in assistant.casefold()
        elif kind == "not_contains":
            passed = value.casefold() not in assistant.casefold()
        elif kind == "regex":
            try:
                passed = re.search(value, transcript, re.IGNORECASE) is not None
            except re.error as exc:
                detail = f"invalid regex: {exc}"
        elif kind == "tool_called":
            passed = value in called
        elif kind == "tool_not_called":
            passed = value not in called
        elif kind == "policy_flag":
            passed, detail = policy_evidence.evaluate(check)
        elif kind == "fixture_complete":
            fixture = case.fixtures.get(value, {})
            stored = report.fixture_state.get(value, {})
            missing = [
                item["key"] for item in (fixture.get("fields", []) if isinstance(fixture, dict) else [])
                if item.get("required", True) and item["key"] not in stored
            ]
            passed = (isinstance(fixture, dict) and fixture.get("fixture_type") == "field_store"
                      and not missing)
            if missing:
                detail = f"missing required fields: {', '.join(missing)}"
        elif kind == "max_turns":
            passed = assistant_turns(report.transcript) <= int(check.get("value", 20))
        results.append({"type": kind, "name": name, "value": value, "pass": passed,
                        "detail": detail})
    return results


def session_mode(config: dict[str, Any], modality: str, *, simulator: bool = False,
                 greeting: str | bool = False) -> DialtMode:
    """The case's mode document, passed through as a start frame carries it. The run owns four
    things, as the hosted runner does: the simulated user has no tools and always has end_call,
    so it can hang up when its instructions say to (reported as simulator_ended); the target has
    end_call unless the case says otherwise; who opens (greeting); and, in voice, the silence
    policy when the case is silent on it, so the watchdog rather than the production
    nudge/sign-off ends a quiet call."""
    overrides: dict[str, Any] = {"greeting": greeting}
    if simulator:
        overrides.update(tools=None, tool_choice=None, web_search=False,
                         end_call=True, end_call_when=None)
    elif "end_call" not in config:
        overrides["end_call"] = True
    if modality == "voice":
        for key, default in (("silence_nudge_s", SIMULATION_SILENCE_NUDGE_S),
                             ("silence_end_s", SIMULATION_SILENCE_END_S)):
            if config.get(key) is None:
                overrides[key] = default
    return replace(DialtMode.from_wire(config, modality=modality), **overrides)


def _wants_session(fixture: Callable[..., Any]) -> bool:
    try:
        return "session" in inspect.signature(fixture).parameters
    except (TypeError, ValueError):
        return False


async def _fixture_result(fixtures: dict[str, Fixture], name: str, args: dict[str, Any],
                          state: dict[str, dict[str, str]] | None = None,
                          session: DialtSession | None = None) -> tuple[Any, str, bool]:
    """Answer a target tool call: a Python callable, a field_store, or a fixed value.

    An undeclared tool fails closed, exactly as hosted runs do, so a local pass means the
    hosted run will not be answering tools the case forgot to declare.

    A callable that declares a `session` parameter receives the live target session, so the
    tool can act on the call it was called from (switch the voice, inject context, restrict
    tools), as a real host does. Hosted runs answer the same tool with the case's fixed
    result; the session-side effect is the host's, and only a local run exercises it.
    """
    if name not in fixtures:
        return ({"error": "unhandled_tool", "tool": name,
                 "instruction": "The case declares no fixture for this tool; fail closed."},
                "failed", False)
    value = fixtures[name]
    if callable(value):
        try:
            value = value(args, session=session) if _wants_session(value) else value(args)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:  # noqa: BLE001 - an application rejection is a tool failure
            # The callback rejected the call (an incomplete qualification, a bad value). That is
            # a failed tool result for the model to work with, not the end of the run.
            return {"error": str(exc) or type(exc).__name__}, "failed", False
        return value, "succeeded", True
    if isinstance(value, dict) and value.get("fixture_type") == "field_store":
        state = state if state is not None else {}
        field_arg, value_arg = value["field_arg"], value["value_arg"]
        key, recorded = args.get(field_arg), args.get(value_arg)
        known = [item["key"] for item in value["fields"]]
        if (key not in known or not isinstance(recorded, str) or not recorded.strip()
                or len(recorded) > MAX_FIXTURE_FIELD_VALUE_CHARS):
            return ({"error": "invalid_fixture_input", "tool": name,
                     "instruction": (f"{field_arg} must name a configured field and {value_arg} "
                                     f"must be 1 to {MAX_FIXTURE_FIELD_VALUE_CHARS} characters of text.")},
                    "failed", False)
        stored = state.setdefault(name, {})
        stored[str(key)] = recorded.strip()
        missing = [item["key"] for item in value["fields"]
                   if item.get("required", True) and item["key"] not in stored]
        return {"recorded": key, "missing_required": missing, "complete": not missing}, "succeeded", True
    if isinstance(value, dict) and "result" in value:
        value = value["result"]
    return value, "succeeded", True


Observer = Callable[[SessionEvent, DialtSession], Awaitable[None]]
ToolResultObserver = Callable[[str, dict[str, Any], Any, str, bool, DialtSession], Awaitable[None]]


async def run_simulation(url: str, api_key: str, case: SimulationCase, *,
                         modality: str = "text",
                         on_target_event: Observer | None = None,
                         on_target_tool_result: ToolResultObserver | None = None) -> SimulationReport:
    """Run target and simulated user as two ordinary Dialt sessions.

    ``on_target_event(event, session)`` sees every event from the target session as it
    arrives, with the live session, so a host-side component that watches the call and acts on
    it (a policy monitor injecting context, a recorder) runs against the simulation exactly as
    it would on a real call. An exception from it ends the run as an error.

    ``on_target_tool_result`` runs in a tracked background task after a target tool result has
    been sent. It is for host work that can wait, such as an acknowledged agent hand-off;
    receive and relay processing continue while it waits.

    In voice mode each session gets a virtual microphone into the other: a paced stream that
    runs for the whole call, carrying the other side's audio at real time and line noise in
    between, so each broker endpoints on trailing silence as on a phone line. Audio is never
    opened on a sound device or written to an output file.
    """
    if modality not in {"text", "voice"}:
        raise ValueError("modality must be text or voice")
    suffix = uuid.uuid4().hex[:10]
    target_id, simulator_id = f"recipe-target-{suffix}", f"recipe-user-{suffix}"
    # Whoever has the opener speaks first: the target with target.greeting, otherwise the
    # simulated user with the starter. The other side opens silent.
    target = await DialtSession.connect(
        url, api_key=api_key, session_id=target_id,
        mode=session_mode(case.target, modality,
                          greeting=str(case.target.get("greeting") or "").strip() or False),
    )
    try:
        simulator = await DialtSession.connect(
            url, api_key=api_key, session_id=simulator_id,
            mode=session_mode(case.simulator, modality, simulator=True,
                              greeting=case.starter if modality == "voice" and case.starter else False),
        )
    except Exception:
        await target.close()
        raise

    report = SimulationReport(case.name, modality, target_id, simulator_id)
    policy_evidence = PolicyEvidence()
    policy_changed = asyncio.Event()
    policy_checks = any(c.get("type") == "policy_flag" for c in case.checks)
    attempt_started = time.monotonic()
    stop = asyncio.Event()
    last_activity = {"at": time.monotonic()}
    target_turns = {"count": 0}
    target_turn_roots: set[str] = set()
    last_speaker = {"side": None}    # who spoke last: a silence is the other side's to explain
    repetition = {"text": "", "count": 0}
    observer_tasks: set[asyncio.Task[None]] = set()

    def start_tool_result_observer(name: str, args: dict[str, Any], value: Any, outcome: str,
                                   verified: bool, session: DialtSession) -> None:
        if on_target_tool_result is None:
            return

        async def observe() -> None:
            try:
                await on_target_tool_result(name, args, value, outcome, verified, session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the observer is the host's code
                if not stop.is_set():
                    report.termination_reason = "observer_error"
                    report.error = f"on_target_tool_result: {exc!r}"[:4000]
                    stop.set()

        task = asyncio.create_task(observe())
        observer_tasks.add(task)
        task.add_done_callback(observer_tasks.discard)

    async def forward_text(destination: DialtSession, text: str) -> None:
        normalized = " ".join(text.casefold().split())
        if normalized and normalized == repetition["text"]:
            repetition["count"] += 1
        else:
            repetition.update(text=normalized, count=1)
        if repetition["count"] >= 3:
            report.termination_reason = "repetition_guard"
            stop.set()
            return
        await destination.send_text(text)

    def relay_failed(error: BaseException) -> None:
        if stop.is_set():
            return
        report.termination_reason = "connection_error"
        report.error = repr(error)[:4000]
        stop.set()

    relays = {
        "target": TextTurnRelay(
            lambda text: forward_text(simulator, text), on_error=relay_failed),
        "simulator": TextTurnRelay(
            lambda text: forward_text(target, text), on_error=relay_failed),
    }
    # Each side's virtual microphone into the other session: a paced stream that runs for the
    # whole attempt, like a phone line, so each broker's own endpointer closes turns on trailing
    # silence. It knows nothing about turns (see the SDK README, "Relaying two sessions").
    voice_relays = {} if modality != "voice" else {
        "target": VoiceTurnRelay(simulator, on_error=relay_failed),
        "simulator": VoiceTurnRelay(target, on_error=relay_failed),
    }

    async def consume(side: str, source: DialtSession) -> None:
        try:
            async for event in source.events():
                last_activity["at"] = time.monotonic()
                record = {"side": side, "type": event.type, "t_ms": event.t_ms, **event.data}
                if policy_checks:
                    policy_evidence.record(record)
                    policy_changed.set()
                if len(report.events) < 2000:
                    report.events.append(record)
                elif len(report.events) == 2000:
                    report.events.append({"type": "policy_evidence_overflow", "side": "target"})
                if side == "target" and on_target_event is not None:
                    try:
                        await on_target_event(event, source)
                    except Exception as exc:  # noqa: BLE001 - the observer is the host's code
                        report.termination_reason = "observer_error"
                        report.error = f"on_target_event: {exc!r}"[:4000]
                        stop.set()
                        return
                if side == "target" and event.type in {"asr", "asr_correction"}:
                    source_id = event.data.get("utterance_id")
                    if event.type == "asr":
                        entry = {"role": "user", "text": event.data.get("text", "")}
                        if source_id:
                            entry["utterance_id"] = source_id
                        report.transcript.append(entry)
                    else:
                        for turn in reversed(report.transcript):
                            if turn.get("role") == "user" and (not source_id or turn.get("utterance_id") == source_id):
                                turn["text"] = event.data.get("text", "")
                                break
                if stop.is_set():
                    if side == "target" and event.type == "error":
                        failure = {"type": "policy_error", "side": "target", "reason": "connection_error"}
                        policy_evidence.record(failure)
                        report.events.append(failure)
                    elif side == "target" and event.type == "utterance" and event.data.get("text"):
                        entry = {"role": "assistant", "text": event.data["text"]}
                        root = turn_root(event.data.get("turn_id"))
                        if root is not None:
                            entry["turn"] = root
                        last = report.transcript[-1] if report.transcript else None
                        if event.data.get("corrected") and last and last.get("role") == "assistant" and last.get("turn") == root:
                            report.transcript[-1] = entry
                        else:
                            report.transcript.append(entry)
                    continue
                if event.type == "asr":
                    pass
                elif side == "target" and event.type == "utterance":
                    text = str(event.data.get("text") or "")
                    root = turn_root(event.data.get("turn_id"))
                    entry = {"role": "assistant", "text": text}
                    if root is not None:
                        entry["turn"] = root
                    if event.data.get("corrected"):
                        # The broker re-truncated a barged reply to what was heard, answering
                        # the mic's playback_stopped report: same turn, replaced, not a new one.
                        last = report.transcript[-1] if report.transcript else None
                        if last and last.get("role") == "assistant" and last.get("turn") == root:
                            report.transcript[-1] = entry
                        continue
                    report.transcript.append(entry)
                    last_speaker["side"] = "target"
                    if modality == "text":
                        relays[side].utterance(text)
                    if root is None or root not in target_turn_roots:   # bridge + final: one turn
                        if root is not None:
                            target_turn_roots.add(root)
                        target_turns["count"] += 1
                    if target_turns["count"] >= case.max_turns:
                        report.termination_reason = "max_turns"
                        stop.set()
                elif side == "simulator" and event.type == "utterance":
                    text = str(event.data.get("text") or "")
                    last_speaker["side"] = "simulator"
                    if modality == "text" and text:
                        relays[side].utterance(text)
                elif event.type == "working" and modality == "text":
                    relays[side].working(bool(event.data.get("active")))
                elif event.type == "audio" and modality == "voice" and event.audio is not None:
                    await voice_relays[side].audio(event.audio)
                elif event.type == "done" and modality == "text":
                    _complete_text_relay(relays[side], event)
                elif event.type == "interrupted" and modality == "voice":
                    # This side's reply was barged: report how much of its audio the other side
                    # never heard, so its committed text is truncated to match.
                    await source.send_client_event(
                        "playback_stopped", **voice_relays[side].interrupted(event.data))
                elif event.type == "canceled" and modality == "voice":
                    voice_relays[side].canceled()
                elif side == "target" and event.type == "tool_call":
                    call_id = str(event.data.get("id") or "")
                    tool_name = str(event.data.get("name") or "")
                    value, outcome, verified = await _fixture_result(
                        case.fixtures, tool_name, event.data.get("args") or {},
                        report.fixture_state, session=source,
                    )
                    await source.send_tool_result(call_id, value, outcome=outcome, verified=verified)
                    start_tool_result_observer(tool_name, event.data.get("args") or {}, value,
                                               outcome, verified, source)
                    fixture = case.fixtures.get(tool_name)
                    if len(report.events) < 2000:
                        report.events.append({
                            "side": "target", "type": "tool_result", "t_ms": event.t_ms,
                            "id": call_id, "name": tool_name, "outcome": outcome,
                            "verified": verified,
                            "fixture": ("unhandled" if fixture is None else "callable"
                                        if callable(fixture) else fixture.get("fixture_type", "fixed")
                                        if isinstance(fixture, dict) else "fixed"),
                        })
                elif event.type == "session_end_requested":
                    # The broker sends this when the model called end_call(farewell). Record it
                    # as a tool call, as hosted runs do, so tool_called checks and the run page
                    # see it. The simulated user hanging up is a harness event, not the agent's.
                    if len(report.events) < 2000:
                        report.events.append({
                            "side": side, "type": "tool_call", "t_ms": event.t_ms,
                            "name": "end_call", "id": "end_call",
                            "args": {"farewell": event.data.get("farewell", "")},
                        })
                    if not stop.is_set():
                        report.termination_reason = (
                            "completed" if side == "target" else "simulator_ended")
                        stop.set()
                elif event.type == "error":
                    if event.data.get("retryable"):
                        # A refused frame the host may resend (a pass landing on a reply still
                        # in flight, a busy injection) is recorded, not the end of the call.
                        continue
                    report.termination_reason = "connection_error"
                    report.error = str(event.data.get("detail") or event.data.get("code") or "error")
                    stop.set()
            if side == "target" and policy_checks and not policy_evidence.complete:
                failure = {"type": "policy_error", "side": "target", "reason": "connection_closed"}
                policy_evidence.record(failure)
                report.events.append(failure)
                policy_changed.set()
            if not stop.is_set():
                report.termination_reason = "connection_closed"
                stop.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report.termination_reason = "connection_error"
            report.error = repr(exc)[:4000]
            stop.set()

    async def watchdog() -> None:
        while not stop.is_set():
            await asyncio.sleep(1)
            if time.monotonic() - last_activity["at"] >= case.silence_s:
                # The agent spoke last and the simulated user never answered: simulator_silent.
                report.termination_reason = (
                    "simulator_silent" if last_speaker["side"] == "target" else "silence_guard")
                stop.set()

    tasks = [
        asyncio.create_task(consume("target", target)),
        asyncio.create_task(consume("simulator", simulator)),
        asyncio.create_task(watchdog()),
    ]
    try:
        if modality == "text" and case.starter:
            await target.send_text(case.starter)
        for mic in voice_relays.values():
            mic.start()          # both lines are live from the first moment, before anyone speaks
        try:
            await asyncio.wait_for(stop.wait(), timeout=case.timeout_s)
        except TimeoutError:
            report.termination_reason = "timeout"
            stop.set()
    finally:
        if policy_checks and report.termination_reason not in {"connection_error", "timeout", "observer_error"}:
            await asyncio.gather(*(relay.close() for relay in relays.values()))
            await asyncio.gather(*(relay.close() for relay in voice_relays.values()))
            remaining = max(0.0, case.timeout_s - (time.monotonic() - attempt_started))
            try:
                async with asyncio.timeout(min(remaining, 55.0)):
                    while not policy_evidence.complete and not policy_evidence.failed:
                        policy_changed.clear()
                        await policy_changed.wait()
            except TimeoutError:
                pass
        for task in tasks:
            task.cancel()
        for task in observer_tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*observer_tasks, return_exceptions=True)
        await asyncio.gather(*(relay.close() for relay in relays.values()))
        await asyncio.gather(*(relay.close() for relay in voice_relays.values()))
        await asyncio.gather(target.close(), simulator.close(), return_exceptions=True)
    report.check_results = evaluate_checks(case, report)
    return report


async def report_attempt(base_url: str, api_key: str, run_id: str, case_id: str,
                         report: SimulationReport, *, repetition: int = 1,
                         idempotency_key: str | None = None) -> dict[str, Any]:
    """Post a local result into a hosted run created with execution="local"."""
    status = "passed" if report.passed else "failed"
    payload = {
        "idempotency_key": idempotency_key or f"recipe-{report.target_session_id}",
        "case_id": case_id, "repetition": repetition, "status": status,
        "target_session_id": report.target_session_id,
        "simulator_session_id": report.simulator_session_id,
        "transcript": report.transcript, "events": report.events,
        "check_results": [r for r in report.check_results if not r.get("skipped")],
        # A judge the local run could not evaluate stays visible on the run page as Skipped.
        "judge_results": [r for r in report.check_results if r.get("skipped")],
        "termination_reason": report.termination_reason, "error": report.error,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/api/app/evals/runs/{run_id}/report",
            headers={"Authorization": f"Bearer {api_key}"}, json=payload,
        )
        if response.is_error:
            try:
                detail = response.json().get("error") or response.json().get("detail")
            except ValueError:
                detail = response.text[:500]
            raise EvalsError(response.status_code, str(detail or "report rejected"))
        return response.json()
