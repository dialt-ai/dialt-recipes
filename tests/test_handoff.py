from dialt import SessionEvent

from dialt_recipes import HandoffBoundary


def _ev(type_, **data):
    return SessionEvent(type=type_, t_ms=0.0, data=data)


def _phases(events, landed_after):
    """(index, phase) for every event the boundary acts on, with `landed` set after
    `landed_after` events."""
    boundary = HandoffBoundary()
    out = []
    for index, event in enumerate(events):
        if index == landed_after:
            boundary.landed = True
        phase = boundary.observe(event)
        if phase:
            out.append((index, phase))
    return out


FIRST_REPLY = [_ev("working", active=True), _ev("turn", turn_id="s-bridge"),
               _ev("done", turn_id="s-bridge"), _ev("turn", turn_id="s-final"),
               _ev("working", active=False), _ev("done", turn_id="s-final")]


def test_bridge_then_answer_passes_on_the_answers_done_and_releases_after_the_first_reply():
    events = [_ev("working", active=True), _ev("tool_call", id="fc1"),
              _ev("turn", turn_id="t-bridge"), _ev("done", turn_id="t-bridge"),
              _ev("turn", turn_id="t-final"), _ev("working", active=False),
              _ev("done", turn_id="t-final"), *FIRST_REPLY, _ev("done", turn_id="later")]
    assert _phases(events, landed_after=2) == [(6, "pass"), (12, "release")]


def test_a_turn_that_closes_after_its_bridge_passes_when_the_wait_ends():
    events = [_ev("working", active=True), _ev("tool_call", id="fc1"),
              _ev("turn", turn_id="t-bridge"), _ev("done", turn_id="t-bridge"),
              _ev("working", active=False), _ev("done", turn_id="later")]
    assert _phases(events, landed_after=2) == [(4, "pass"), (5, "release")]


def test_an_answer_without_a_bridge_waits_for_its_done_whichever_order_it_starts():
    working_first = [_ev("working", active=True), _ev("tool_call", id="fc1"),
                     _ev("working", active=False), _ev("turn", turn_id="t-final"),
                     _ev("done", turn_id="t-final")]
    turn_first = [_ev("working", active=True), _ev("tool_call", id="fc1"),
                  _ev("turn", turn_id="t-final"), _ev("working", active=False),
                  _ev("done", turn_id="t-final")]
    assert _phases(working_first, landed_after=2) == [(4, "pass")]
    assert _phases(turn_first, landed_after=2) == [(4, "pass")]


def test_the_release_waits_for_a_whole_first_reply_not_its_bridge():
    """A forced first tool makes the first reply a tool turn: bridge, then answer. The release
    comes after the answer, while the tool wait covers the bridge's done."""
    pass_then_reply = [_ev("done", turn_id="t"), *FIRST_REPLY]
    assert _phases(pass_then_reply, landed_after=0) == [(0, "pass"), (6, "release")]


def test_nothing_fires_before_the_handoff_lands_or_after_the_release():
    events = [_ev("turn", turn_id="a"), _ev("done", turn_id="a"), _ev("done", turn_id="b")]
    assert _phases(events, landed_after=99) == []
    boundary = HandoffBoundary(landed=True)
    assert boundary.observe(_ev("done", turn_id="a")) == "pass"
    assert boundary.observe(_ev("done", turn_id="b")) == "release"
    assert boundary.observe(_ev("done", turn_id="c")) is None
