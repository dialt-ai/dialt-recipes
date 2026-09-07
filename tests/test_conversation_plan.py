import asyncio

from dialt import SessionEvent

from dialt_recipes import ConversationPlan, GuidedAssistant, PlanField


def plan():
    return ConversationPlan(
        name="a research interview", objective="Understand one recent incident.",
        fields=(PlanField("incident", "A specific event."),
                PlanField("impact", "Its observable consequence.")),
        completion="Reflect the account back and ask for corrections.",
    )


def test_plan_records_host_owned_evidence_and_completion():
    current, spec = {}, plan()
    first = spec.record(current, {"field": "incident", "value": "Checkout failed Friday"})
    assert first == {"recorded": "incident", "missing_required": ["impact"], "complete": False}
    second = spec.record(current, {"field": "impact", "value": "Two hours lost"})
    assert second["complete"] is True
    assert spec.tool()["parameters"]["properties"]["field"]["enum"] == ["incident", "impact"]
    assert "Do not read the field list aloud" in spec.instructions()


def test_guided_controller_resolves_record_tool_with_verified_state():
    class FakeSession:
        def __init__(self):
            self.results = []

        async def events(self):
            yield SessionEvent(
                "tool_call", 10, data={"id": "call-1", "name": "record_plan_field",
                                       "args": {"field": "incident", "value": "A real event"}})

        async def send_tool_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    async def run():
        session = FakeSession()
        assistant = GuidedAssistant(session=session, plan=plan(), modality="text")
        assert [event.type async for event in assistant.events()] == ["tool_call"]
        return assistant, session

    assistant, session = asyncio.run(run())
    assert assistant.answers == {"incident": "A real event"}
    assert session.results[0][1] == {"outcome": "succeeded", "verified": True}


def test_recording_can_be_disabled_without_losing_evidence_instructions():
    from dataclasses import replace
    current = replace(plan(), record_as_you_go=False)
    assert current.tools() == []
    assert current.tool_name not in current.instructions()
    assert 'clarify' in current.instructions().lower()
    assert 'incident (required)' in current.instructions()
    assert plan().tools() == [plan().tool()]


def test_complete_snapshot_rejects_missing_blank_and_unknown_details():
    import pytest
    current = plan()
    for values in ({'incident': 'outage'}, {'incident': 'outage', 'impact': ' '},
                   {'incident': 'outage', 'impact': 'two hours', 'guess': 'x'}):
        with pytest.raises(ValueError):
            current.validate_evidence(values)
    assert current.validate_evidence({'incident': ' outage ', 'impact': 'two hours'}) == {
        'incident': 'outage', 'impact': 'two hours'}


def test_plan_rejects_string_boolean():
    from dataclasses import replace
    import pytest
    with pytest.raises(ValueError, match='boolean'):
        replace(plan(), record_as_you_go='false')
