from dataclasses import replace
import asyncio
import json
from pathlib import Path

import pytest
from dialt.relay import TextTurnRelay

from dialt_recipes.cli import collect_cases, push
from dialt_recipes.simulation import (
    SimulationCase,
    assistant_turns,
    _complete_text_relay,
    SimulationReport,
    _fixture_result,
    evaluate_checks,
    run_simulation,
)

SAMPLE = Path(__file__).resolve().parents[1] / "examples/simulations/appointment_booking.json"


@pytest.mark.parametrize("side", ["target", "simulator"])
def test_text_relay_withholds_a_tool_bridge_until_its_final_for_either_side(side):
    """A bridge is cover while a tool is unresolved, so it cannot prompt the other agent.

    The later final replaces the pending bridge before the one relay flush. A plain completed
    reply still flushes immediately.
    """
    class Relay:
        def __init__(self):
            self.pending = None
            self.forwarded = []

        def utterance(self, text):
            self.pending = text

        def done(self):
            self.forwarded.append(self.pending)

    from types import SimpleNamespace

    relay = Relay()
    bridge = SimpleNamespace(data={"turn_id": f"{side}-tool-bridge"})
    final = SimpleNamespace(data={"turn_id": f"{side}-tool-final"})
    ordinary = SimpleNamespace(data={"turn_id": f"{side}-ordinary"})

    relay.utterance("I will check that.")
    _complete_text_relay(relay, bridge)
    assert relay.forwarded == []
    relay.utterance("Your appointment is September 21.")
    _complete_text_relay(relay, final)
    assert relay.forwarded == ["Your appointment is September 21."]
    relay.utterance("Anything else?")
    _complete_text_relay(relay, ordinary)
    assert relay.forwarded == ["Your appointment is September 21.", "Anything else?"]


def test_text_relay_keeps_a_tool_bridge_pending_across_the_idle_gate():
    """Only the final completion flushes after tool work settles."""
    from types import SimpleNamespace

    async def run():
        forwarded, delivered = [], asyncio.Event()

        async def forward(text):
            forwarded.append(text)
            delivered.set()

        relay = TextTurnRelay(forward, settle_s=0)
        bridge = SimpleNamespace(data={"turn_id": "turn-1-bridge"})
        final = SimpleNamespace(data={"turn_id": "turn-1-final"})
        relay.working(True)
        relay.utterance("I will check that.")
        _complete_text_relay(relay, bridge)
        relay.working(False)
        assert forwarded == []

        relay.working(True)
        relay.utterance("Your appointment is September 21.")
        _complete_text_relay(relay, final)
        await asyncio.sleep(0)  # Let the zero-settle relay reach its active-work gate.
        assert forwarded == []
        relay.working(False)
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert forwarded == ["Your appointment is September 21."]
        await relay.close()

    asyncio.run(run())


def test_case_uses_the_hosted_document_shape():
    case = SimulationCase.from_dict(json.loads(SAMPLE.read_text()))
    assert case.name.startswith("physiotherapy")
    assert case.target_tools[0]["name"] == "check_availability"
    assert case.simulator_instructions.startswith("You are Maya")
    assert case.max_turns == 10 and case.timeout_s == 240 and case.silence_s == 35
    assert [check["type"] for check in case.checks] == [
        "tool_called", "tool_called", "contains", "judge"]

    with pytest.raises(ValueError, match="target.instructions"):
        SimulationCase.from_dict({"name": "old", "starter": "hi", "target_instructions": "x"})
    with pytest.raises(ValueError, match="unsupported check type"):
        SimulationCase.from_dict({"name": "bad", "starter": "hi", "checks": [{"type": "vibes", "value": 1}]})
    with pytest.raises(ValueError, match="needs a value or criterion"):
        SimulationCase.from_dict({"name": "bad", "starter": "hi", "checks": [{"type": "contains"}]})
    with pytest.raises(ValueError, match="starter must contain"):
        SimulationCase.from_dict({"name": "no starter"})
    assert case.target["end_call"] is True
    assert "end_call" not in SimulationCase.from_dict({"name": "n", "starter": "hi"}).target
    with pytest.raises(ValueError, match="target.end_call must be true, false"):
        SimulationCase.from_dict({"name": "n", "starter": "hi", "target": {"end_call": "no"}})


class _FakeSession:
    def __init__(self, events):
        self._events = events
        self.sent = []

    async def events(self):
        for event in self._events:
            yield event
        await asyncio.sleep(30)

    async def send_text(self, text):
        self.sent.append(text)

    async def send_tool_result(self, *_args, **_kwargs):
        pass

    async def send_client_event(self, event, **fields):
        self.client_events.append((event, fields))

    async def close(self):
        pass


def test_voice_mics_run_for_the_whole_call_and_answer_a_barge_like_a_client(monkeypatch):
    """Both virtual mics start before anyone speaks and have no turn API for the runner to
    call. This side's `interrupted` is answered with the mic's playback_stopped report on this
    side's own socket, `canceled` reaches the mic, and the broker's corrected utterance (the
    barged reply re-truncated to what was heard) replaces the transcript entry instead of
    adding a second turn."""
    from types import SimpleNamespace
    target = _FakeSession([
        SimpleNamespace(type="utterance", t_ms=1,
                        data={"text": "I can process the refund [interrupted]", "turn_id": "turn-2"}),
        SimpleNamespace(type="interrupted", t_ms=2,
                        data={"turn_id": "turn-2", "barge_seq": 1, "clear": True}),
        SimpleNamespace(type="utterance", t_ms=3,
                        data={"text": "I can process the [interrupted]", "turn_id": "turn-2",
                              "corrected": True, "barge_seq": 1}),
        SimpleNamespace(type="canceled", t_ms=4, data={"turn_id": "turn-3"}),
        SimpleNamespace(type="session_end_requested", t_ms=5, data={}),
    ])
    simulator = _FakeSession([
        SimpleNamespace(type="working", t_ms=2, data={"active": True}),
        SimpleNamespace(type="done", t_ms=3, data={"turn_id": "turn-1"}),
    ])
    target.client_events, simulator.client_events = [], []
    sessions = iter([target, simulator])
    relays = []

    class RelaySpy:
        def __init__(self, destination, **_kwargs):
            self.destination = destination
            self.started = False
            self.interrupts = []
            self.cancels = 0
            relays.append(self)

        def start(self):
            self.started = True

        async def audio(self, _chunk):
            pass

        def interrupted(self, event):
            self.interrupts.append(event)
            return {"remaining_ms": 0, "discarded_ms": 480, "barge_seq": event["barge_seq"]}

        def canceled(self):
            self.cancels += 1

        async def close(self):
            pass

    async def connect(*_args, **_kwargs):
        return next(sessions)

    monkeypatch.setattr("dialt_recipes.simulation.DialtSession.connect", connect)
    monkeypatch.setattr("dialt_recipes.simulation.VoiceTurnRelay", RelaySpy)
    case = SimulationCase.from_dict({
        "name": "n", "starter": "Hello", "limits": {"timeout_s": 10, "max_turns": 2}})
    report = asyncio.run(run_simulation("ws://test", "key", case, modality="voice"))

    assert report.termination_reason == "completed"
    assert [relay.destination for relay in relays] == [simulator, target]
    assert [relay.started for relay in relays] == [True, True]
    assert [relay.interrupts for relay in relays] == [
        [{"turn_id": "turn-2", "barge_seq": 1, "clear": True}], []]
    assert [relay.cancels for relay in relays] == [1, 0]
    assert target.client_events == [
        ("playback_stopped", {"remaining_ms": 0, "discarded_ms": 480, "barge_seq": 1})]
    assert report.transcript == [
        {"role": "assistant", "text": "I can process the [interrupted]", "turn": "turn-2"}]


def test_end_call_is_a_recorded_tool_call_and_the_simulator_always_has_it(monkeypatch):
    """The broker's session_end_requested means the model called end_call(farewell). It is
    recorded as a tool call, as hosted runs record it; the target's end_call flag follows the
    case and the simulated user always has the tool."""
    from types import SimpleNamespace
    target = _FakeSession([
        SimpleNamespace(type="utterance", t_ms=1, data={"text": "Goodbye."}),
        SimpleNamespace(type="done", t_ms=2, data={}),
        SimpleNamespace(type="session_end_requested", t_ms=3, data={"farewell": "Goodbye."}),
    ])
    simulator = _FakeSession([])
    sessions = iter([target, simulator])
    modes = []

    async def connect(*_args, mode, **_kwargs):
        modes.append(mode)
        return next(sessions)

    monkeypatch.setattr("dialt_recipes.simulation.DialtSession.connect", connect)
    case = SimulationCase.from_dict({
        "name": "n", "starter": "Hello", "target": {"end_call": False},
        "checks": [{"type": "tool_called", "value": "end_call"}], "limits": {"timeout_s": 10},
    })
    report = asyncio.run(run_simulation("ws://test", "key", case))

    assert [mode.end_call for mode in modes] == [False, True]
    assert report.termination_reason == "completed"
    assert [e for e in report.events if e["type"] == "tool_call"] == [{
        "side": "target", "type": "tool_call", "t_ms": 3, "name": "end_call",
        "id": "end_call", "args": {"farewell": "Goodbye."},
    }]
    assert report.check_results[0]["pass"] is True and report.passed


def test_checks_match_the_hosted_runner_and_skip_judges():
    case = SimulationCase.from_dict({
        "name": "booking", "starter": "Hello",
        "target": {"instructions": "Book safely.", "tools": [
            {"name": "lookup"},
            {"name": "intake", "parameters": {"type": "object", "properties": {"field": {}, "value": {}}}}]},
        "simulator": {"instructions": "Want a slot."},
        "fixtures": {"intake": {"fixture_type": "field_store", "field_arg": "field",
                                "value_arg": "value", "fields": [{"key": "name"}, {"key": "phone"}]}},
        "checks": [
            {"type": "contains", "value": "3:30"},
            {"type": "not_contains", "value": "refund"},
            {"type": "regex", "value": r"assistant: .*confirmation PT-\d+"},
            {"type": "tool_called", "value": "lookup", "name": "used the lookup"},
            {"type": "fixture_complete", "value": "intake"},
            {"type": "max_turns", "value": 2},
            {"type": "judge", "criterion": "Polite throughout."},
        ],
    })
    report = SimulationReport(
        "booking", "text", "target", "simulator",
        transcript=[{"role": "user", "text": "hi"},
                    {"role": "assistant", "text": "3:30 is available; confirmation PT-1."}],
        events=[{"side": "target", "type": "tool_call", "name": "lookup"}],
        fixture_state={"intake": {"name": "Maya"}},
        termination_reason="completed",
    )
    results = evaluate_checks(case, report)
    assert [(r["name"], r["pass"]) for r in results] == [
        ("contains-1", True), ("not_contains-2", True), ("regex-3", True),
        ("used the lookup", True), ("fixture_complete-5", False), ("max_turns-6", True),
        ("judge-7", None),
    ]
    assert results[4]["detail"] == "missing required fields: phone"
    assert results[6]["skipped"] is True and results[6]["detail"] == "judge checks run hosted"
    report.check_results = results
    assert report.passed is False                       # the field store is incomplete
    report.fixture_state["intake"]["phone"] = "555"
    report.check_results = evaluate_checks(case, report)
    assert report.passed is True                        # the skipped judge does not block
    report.termination_reason = "simulator_ended"       # the simulated user hung up: checks decide
    assert report.passed is True
    report.termination_reason = "silence_guard"         # the agent went quiet: a failure
    assert report.passed is False


def test_fixtures_answer_like_hosted_runs():
    async def run():
        fixed = await _fixture_result({"lookup": {"result": {"slot": "3:30"}}}, "lookup", {})
        callback = await _fixture_result({"lookup": lambda args: {"slot": args["wanted"]}},
                                         "lookup", {"wanted": "4:00"})
        missing = await _fixture_result({}, "delete", {})

        async def switch(args, session):      # a tool that acts on the call it came from
            return {"voice": session, "to": args["voice"]}

        aware = await _fixture_result({"switch": switch}, "switch", {"voice": "warm"}, None,
                                      session="live-target")
        state = {}
        store = {"fixture_type": "field_store", "field_arg": "field", "value_arg": "value",
                 "fields": [{"key": "name"}, {"key": "phone", "required": False}]}
        recorded = await _fixture_result({"intake": store}, "intake",
                                         {"field": "name", "value": " Maya "}, state)
        rejected = await _fixture_result({"intake": store}, "intake",
                                         {"field": "age", "value": "40"}, state)
        return fixed, callback, missing, aware, recorded, rejected, state

    fixed, callback, missing, aware, recorded, rejected, state = asyncio.run(run())
    assert fixed == ({"slot": "3:30"}, "succeeded", True)
    assert callback == ({"slot": "4:00"}, "succeeded", True)
    assert aware == ({"voice": "live-target", "to": "warm"}, "succeeded", True)
    assert missing[0]["error"] == "unhandled_tool" and missing[1:] == ("failed", False)
    assert recorded == ({"recorded": "name", "missing_required": [], "complete": True},
                        "succeeded", True)
    assert rejected[0]["error"] == "invalid_fixture_input" and rejected[1] == "failed"
    assert state == {"intake": {"name": "Maya"}}


def test_collect_cases_reads_files_and_directories(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "starter": "hi"}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "b", "starter": "hi"}))
    assert [case.name for case in collect_cases([tmp_path, SAMPLE])] == [
        "a", "b", "physiotherapy appointment with constraints and permission"]
    (tmp_path / "c.json").write_text(json.dumps({"name": "c", "target_instructions": "old"}))
    with pytest.raises(SystemExit, match="target.instructions"):
        collect_cases([tmp_path])
    (tmp_path / "c.json").write_text(json.dumps({"name": "c"}))
    with pytest.raises(SystemExit, match="starter must contain"):
        collect_cases([tmp_path])


def test_push_upserts_then_starts_one_run(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "starter": "hi"}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "b", "starter": "hi"}))

    class FakeClient:
        def __init__(self):
            self.calls = []

        def upsert_cases(self, documents):
            self.calls.append(("upsert", [d["name"] for d in documents]))
            return [{"id": f"id-{d['name']}", "name": d["name"]} for d in documents]

        def start_run(self, case_ids, *, modality, repetitions):
            self.calls.append(("run", case_ids, modality, repetitions))
            return {"id": "run-1234abcd", "status": "queued"}

        def dashboard_url(self, run_id):
            return f"https://example.test/evals/{run_id}"

        def wait(self, run_id):
            return {"id": run_id, "status": "passed", "attempts": [
                {"status": "passed", "case_name": "a", "termination_reason": "completed"},
                {"status": "passed", "case_name": "b", "termination_reason": "max_turns"},
            ]}

    lines, client = [], FakeClient()
    (tmp_path / "z.json").write_text(json.dumps({"name": "z", "starter": "hi", "checks": [{"type": "contains"}]}))
    with pytest.raises(ValueError, match="z: each check needs"):
        push(client, [tmp_path], modality="text", repetitions=1, wait=False, out=lines.append)
    assert client.calls == []                                   # nothing upserted on a bad file
    (tmp_path / "z.json").unlink()
    run = push(client, [tmp_path], modality="voice", repetitions=2, wait=True, out=lines.append)
    assert client.calls == [("upsert", ["a", "b"]), ("run", ["id-a", "id-b"], "voice", 2)]
    assert run["status"] == "passed"
    assert lines[0] == "2 cases pushed: a, b"
    assert lines[1] == "run run-1234 started (voice): https://example.test/evals/run-1234abcd"
    assert lines[-1] == "run run-1234 passed"


def test_bridge_and_final_are_one_turn_and_voice_starters_are_checked():
    transcript = [{"role": "user", "text": "hi"},
                  {"role": "assistant", "text": "Let me check.", "turn": "turn1"},
                  {"role": "assistant", "text": "Tuesday.", "turn": "turn1"},
                  {"role": "assistant", "text": "Else?", "turn": "turn2"},
                  {"role": "assistant", "text": "legacy entry"}]
    assert assistant_turns(transcript) == 3
    case = SimulationCase.from_dict({"name": "n", "starter": "I need help. " * 40})
    assert case.max_turns == 20
    with pytest.raises(ValueError, match="300 characters"):
        SimulationCase.from_dict({"name": "n", "starter": "I need help. " * 40}, modality="voice")


def test_dialt_sim_checks_voice_starters_before_running(tmp_path):
    (tmp_path / "long.json").write_text(json.dumps({"name": "long", "starter": "I need help. " * 40}))
    assert collect_cases([tmp_path], "text")[0].name == "long"
    with pytest.raises(SystemExit, match="300 characters"):
        collect_cases([tmp_path], "voice")


def test_session_mode_passes_options_through_and_keeps_the_run_owned_ones() -> None:
    """Every session option in the case reaches the session; the run only overrides who opens,
    the simulator's tools and end_call, and the voice silence policy when the case is silent."""
    from dialt.relay import SIMULATION_SILENCE_END_S, SIMULATION_SILENCE_NUDGE_S
    from dialt_recipes.simulation import session_mode

    assert session_mode({}, "text").end_call is True
    assert session_mode({"end_call": False}, "text").end_call is False
    conditioned = session_mode({"end_call": {"when": "the caller says goodbye"}}, "text")
    assert conditioned.end_call is True and conditioned.end_call_when == "the caller says goodbye"
    tuned = session_mode({"turn_end_threshold": 0.2, "silence_nudge_s": 8, "silence_end_s": 20,
                          "tools": [{"name": "book"}], "tool_choice": {"tool": "book"}}, "voice")
    assert tuned.turn_end_threshold == 0.2 and (tuned.silence_nudge_s, tuned.silence_end_s) == (8, 20)
    assert tuned.tool_choice == {"tool": "book"} and tuned.greeting is False
    default = session_mode({}, "voice")
    assert (default.silence_nudge_s, default.silence_end_s) == (
        SIMULATION_SILENCE_NUDGE_S, SIMULATION_SILENCE_END_S)
    caller = session_mode({"instructions": "Act like a caller", "voice": "ember"}, "voice",
                          simulator=True, greeting="Hello?")
    assert caller.voice == "ember" and caller.tools is None and caller.end_call is True
    assert caller.greeting == "Hello?"
    with pytest.raises(ValueError, match="unexpected field: persona"):
        session_mode({"persona": "x"}, "text")
    with pytest.raises(ValueError, match="simulator.tools is set by the run"):
        SimulationCase.from_dict({"name": "n", "starter": "hi", "simulator": {"tools": []}})


def test_callable_fixture_rejection_is_a_failed_tool_result() -> None:
    """A callback that raises (an incomplete qualification) is a failed result the model can
    work with, not a connection error that ends the run."""
    import asyncio
    from dialt_recipes.simulation import _fixture_result

    def reject(args):
        raise ValueError("qualification incomplete")

    result, outcome, verified = asyncio.run(_fixture_result({"start": reject}, "start", {}, None))
    assert result == {"error": "qualification incomplete"}
    assert (outcome, verified) == ("failed", False)


@pytest.mark.parametrize("modality", ["text", "voice"])
def test_target_greeting_opens_the_conversation_and_nothing_is_sent_first(monkeypatch, modality):
    """With target.greeting the target session gets the greeting and the simulated user opens
    silent: no starter is sent in text mode and the voice simulator has no greeting of its own.
    The broker plays the target greeting at ready, so the relays forward it like any turn."""
    target, simulator = _FakeSession([]), _FakeSession([])
    sessions = iter([target, simulator])
    modes = []

    async def connect(*_args, **kwargs):
        modes.append(kwargs["mode"])
        return next(sessions)

    class RelayStub:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr("dialt_recipes.simulation.DialtSession.connect", connect)
    monkeypatch.setattr("dialt_recipes.simulation.VoiceTurnRelay", RelayStub)
    case = SimulationCase.from_dict({
        "name": "n", "target": {"greeting": "Hi, how old are you?"}, "limits": {"timeout_s": 10}})
    assert case.starter == "" and case.target["greeting"] == "Hi, how old are you?"
    report = asyncio.run(run_simulation("ws://test", "key", case, modality=modality))

    assert report.termination_reason == "timeout"
    assert target.sent == []
    assert modes[0].greeting == "Hi, how old are you?"
    assert modes[1].greeting is False
    with pytest.raises(ValueError, match="not both"):
        SimulationCase.from_dict({"name": "n", "starter": "hi", "target": {"greeting": "Hello"}})


@pytest.mark.parametrize('ending', ['settled', 'error', 'incomplete'])
def test_policy_drain_keeps_observers_and_replaces_corrected_speech(monkeypatch, ending):
    from types import SimpleNamespace

    def event(kind, **data):
        return SimpleNamespace(type=kind, t_ms=1, data=data)

    class DelayedSession(_FakeSession):
        async def events(self):
            for item in self._events:
                if isinstance(item, tuple):
                    delay, item = item
                    await asyncio.sleep(delay)
                yield item
            await asyncio.sleep(30)

    tail = [
        (0.025, event('policy_flag', rule='r', occurrence_id='one:r', revision=1,
                      status='raised', delivered=True)),
        event('utterance', text='I will get the manager now.', turn_id='turn-2', policy_version=2),
        event('utterance', text='I will get the manager.', turn_id='turn-2',
              corrected=True, policy_version=2),
    ]
    if ending == 'settled':
        tail.append(event('policy_settled', policy_version=2, checked_version=2, healthy=True))
    elif ending == 'error':
        tail.append(event('error', code='broken'))
    target = DelayedSession([event('asr', text='I want to complain', policy_version=1), *tail])
    simulator = DelayedSession([(0.01, event('session_end_requested', farewell='bye'))])
    sessions = iter([target, simulator])
    observed = []

    async def connect(*args, **kwargs):
        return next(sessions)

    async def observe(item, session):
        observed.append(item.type)

    monkeypatch.setattr('dialt_recipes.simulation.DialtSession.connect', connect)
    case = SimulationCase.from_dict({
        'name': 'drain', 'starter': 'Hello',
        'target': {'policy': {'report_checks': True, 'rules': [
            {'id': 'r', 'when': 'A complaint', 'do': 'Escalate', 'action': 'next_turn'}]}},
        'limits': {'timeout_s': 10},
        'checks': [{'type': 'policy_flag', 'value': 'r', 'delivered': True}],
    })
    case = replace(case, timeout_s=0.1)  # Keep the incomplete-monitoring timeout test short.
    report = asyncio.run(run_simulation('ws://test', 'key', case, on_target_event=observe))
    assert report.check_results[0]['pass'] is (ending == 'settled')
    assert 'policy_flag' in observed
    assert [turn['text'] for turn in report.transcript if turn['role'] == 'assistant'] == [
        'I will get the manager.']
    assert simulator.sent == []


@pytest.mark.parametrize('during_drain', [False, True])
def test_policy_asr_revision_updates_its_source_not_the_latest_user(monkeypatch, during_drain):
    from types import SimpleNamespace
    def event(kind, **data):
        return SimpleNamespace(type=kind, t_ms=1, data=data)
    class DelayedSession(_FakeSession):
        async def events(self):
            for item in self._events:
                if isinstance(item, tuple):
                    delay, item = item
                    await asyncio.sleep(delay)
                yield item
            await asyncio.sleep(30)
    target = DelayedSession([
        event('asr', text='wrong record', utterance_id='one', policy_version=1),
        event('asr', text='next question', utterance_id='two', policy_version=2),
        (0.025, event('asr_correction', text='correct record', utterance_id='one', policy_version=3)),
        event('policy_settled', policy_version=3, checked_version=3, healthy=True),
    ])
    simulator = DelayedSession([(0.01 if during_drain else 0.05,
                                event('session_end_requested', farewell='bye'))])
    sessions = iter([target, simulator])
    async def connect(*args, **kwargs):
        return next(sessions)
    monkeypatch.setattr('dialt_recipes.simulation.DialtSession.connect', connect)
    case = SimulationCase.from_dict({
        'name': 'revision', 'starter': 'Hello',
        'target': {'policy': {'report_checks': True, 'rules': [
            {'id': 'r', 'when': 'A complaint', 'do': 'Escalate', 'action': 'next_turn'}]}},
        'limits': {'timeout_s': 10},
        'checks': [{'type': 'policy_flag', 'value': 'r', 'min_count': 0, 'max_count': 0}],
    })
    report = asyncio.run(run_simulation('ws://test', 'key', case))
    assert [turn['text'] for turn in report.transcript] == ['correct record', 'next question']
    assert report.check_results[0]['pass']
