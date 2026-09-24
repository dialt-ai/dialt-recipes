"""Component / Small / Contract / CI: current SDK reporting and simulated caller opening."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from dialt.evals import EvalsClient, EvalsError

from dialt_recipes.simulation import SimulationCase, SimulationReport, report_attempt, run_simulation


def test_local_report_uses_sdk_versioned_route_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    observed = []

    def request(client, method, path, body=None):
        assert threading.get_ident() != main_thread
        observed.append((method, path, body))
        return {"status": body["status"]}

    monkeypatch.setattr(EvalsClient, "_request", request)
    report = SimulationReport("example", "text", "assistant", "caller", termination_reason="completed")
    result = asyncio.run(report_attempt("https://api.example.test", "fake-key", "run", "case", report,
                                        idempotency_key="stable-key"))
    assert result == {"status": "passed"}
    method, path, body = observed[0]
    assert (method, path) == ("POST", "/v1/evals/runs/run/report")
    assert body["idempotency_key"] == "stable-key"
    assert (body["target_session_id"], body["simulator_session_id"]) == ("assistant", "caller")

    def rejected(*args, **kwargs):
        raise EvalsError(400, "invalid report", code="invalid_request")

    monkeypatch.setattr(EvalsClient, "_request", rejected)
    with pytest.raises(EvalsError) as error:
        asyncio.run(report_attempt("https://api.example.test", "fake-key", "run", "case", report))
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("modality", ["text", "voice"])
def test_caller_owns_starter_in_both_modalities(monkeypatch, modality):
    modes = []

    class Session:
        async def events(self):
            yield SimpleNamespace(type="error", t_ms=0, data={"code": "test_stop"})

        async def send_text(self, text):
            pytest.fail("starter must be emitted by the caller greeting, not injected into the assistant")

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        modes.append(kwargs["mode"])
        return Session()

    class Relay:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr("dialt_recipes.simulation.DialtSession.connect", connect)
    monkeypatch.setattr("dialt_recipes.simulation.VoiceTurnRelay", Relay)
    case = SimulationCase.from_dict({"name": "caller opening", "starter": "Hello there."})
    asyncio.run(run_simulation("ws://example.test", "fake-key", case, modality=modality))
    assert modes[0].greeting is False
    assert modes[1].greeting == "Hello there."
    assert (modes[0].voice, modes[1].voice, modes[1].brain) == ("circuit", "classic", "smart")
