import asyncio

from dialt_recipes import pass_call_to


def test_pass_call_to_can_handoff_agents_sequentially() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls = []

        async def handoff_agent(self, **kwargs):
            self.calls.append(("handoff_agent", kwargs))
            return {"accepted": True, "status": "applied"}

        async def inject_context(self, note, *, role, reply):
            self.calls.append(("inject_context", note, role, reply))
            return {"accepted": True}

    session = Session()
    asyncio.run(pass_call_to(session, instructions="first", tools=[], voice="one", context="one",
                             operation_id="one"))
    asyncio.run(pass_call_to(session, instructions="second", tools=[], voice="two", context="two",
                             operation_id="two"))
    assert [call[0] for call in session.calls] == [
        "handoff_agent", "inject_context", "handoff_agent", "inject_context"]
    assert session.calls[2][1]["operation_id"] == "two"
    assert session.calls[0][1]["context"] == "one"


def test_pass_call_to_does_not_inject_after_rejection() -> None:
    class Session:
        async def handoff_agent(self, **kwargs):
            return {"accepted": False}

        async def inject_context(self, *args, **kwargs):
            raise AssertionError("rejected handoff must not request a reply")

    assert asyncio.run(pass_call_to(Session(), instructions="next", tools=[], context="hello")) == {
        "accepted": False, "status": "rejected", "switched": False,
        "handoff": {"accepted": False}, "reply": None, "reply_started": False}


def test_opener_exception_does_not_hide_an_applied_handoff() -> None:
    class Session:
        async def handoff_agent(self, **kwargs):
            return {"accepted": True, "status": "applied"}

        async def inject_context(self, *args, **kwargs):
            raise RuntimeError("connection closed")

    ack = asyncio.run(pass_call_to(Session(), instructions="next", tools=[], context="details"))
    assert ack["accepted"] and ack["status"] == "applied" and ack["switched"]
    assert ack["reply"] is None and ack["opener_error"] == "connection closed"
