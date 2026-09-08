from dialt import SessionEvent

from dialt_recipes import HandoffBoundary


def _ev(type_, **data):
    return SessionEvent(type=type_, t_ms=0.0, data=data)


def _fires_on(events, landed_after):
    """Index of the event that says 'pass now', with `landed` set after `landed_after` events."""
    boundary = HandoffBoundary()
    fired = []
    for index, event in enumerate(events):
        if index == landed_after:
            boundary.landed = True
        if boundary.observe(event):
            fired.append(index)
    return fired


def test_bridge_then_answer_passes_on_the_answers_done():
    events = [_ev("working", active=True), _ev("tool_call", id="fc1"),
              _ev("turn", turn_id="t-bridge"), _ev("done", turn_id="t-bridge"),
              _ev("turn", turn_id="t-final"), _ev("working", active=False),
              _ev("done", turn_id="t-final"), _ev("done", turn_id="later")]
    assert _fires_on(events, landed_after=2) == [6]


def test_a_turn_that_closes_after_its_bridge_passes_when_the_wait_ends():
    events = [_ev("working", active=True), _ev("tool_call", id="fc1"),
              _ev("turn", turn_id="t-bridge"), _ev("done", turn_id="t-bridge"),
              _ev("working", active=False), _ev("done", turn_id="later")]
    assert _fires_on(events, landed_after=2) == [4]


def test_an_answer_without_a_bridge_waits_for_its_done_whichever_order_it_starts():
    working_first = [_ev("working", active=True), _ev("tool_call", id="fc1"),
                     _ev("working", active=False), _ev("turn", turn_id="t-final"),
                     _ev("done", turn_id="t-final")]
    turn_first = [_ev("working", active=True), _ev("tool_call", id="fc1"),
                  _ev("turn", turn_id="t-final"), _ev("working", active=False),
                  _ev("done", turn_id="t-final")]
    assert _fires_on(working_first, landed_after=2) == [4]
    assert _fires_on(turn_first, landed_after=2) == [4]


def test_nothing_fires_before_the_handoff_lands_or_after_the_pass():
    events = [_ev("turn", turn_id="a"), _ev("done", turn_id="a"), _ev("done", turn_id="b")]
    assert _fires_on(events, landed_after=99) == []
    boundary = HandoffBoundary(landed=True)
    assert boundary.observe(_ev("done", turn_id="a")) is True
    assert boundary.observe(_ev("done", turn_id="b")) is False
