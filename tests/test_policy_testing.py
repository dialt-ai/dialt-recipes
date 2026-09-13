from types import SimpleNamespace
import pytest
from dialt_recipes.policy_testing import check_policy_at_hangup


@pytest.mark.parametrize('ending,events,unknown', [
    ('completed', [], True),
    ('simulator_ended', [], False),
    ('completed', [{'type':'policy_error','reason':'judge_failed'}], False),
    ('completed', [{'type':'policy_evidence_overflow'}], False),
    ('completed', [{'type':'policy_settled','policy_version':2,'checked_version':2,'healthy':True}], False),
    ('completed', [{'type':'policy_flag','rule':'forbidden','occurrence_id':'one:forbidden',
                    'revision':1,'status':'raised','delivered':True}], False),
])
def test_hangup_only_accepts_explicit_incomplete_monitoring(ending,events,unknown):
    checks=[{'type':'policy_flag','value':'required','pass':False},
            {'type':'policy_flag','value':'forbidden','pass':False},
            {'type':'tool_called','value':'record','pass':True}]
    case=SimpleNamespace(checks=[{'type':'policy_flag','value':'required'},
                                {'type':'policy_flag','value':'forbidden','max_count':0}])
    timeline=[{'type':'asr','policy_version':2}, {'type':'tool_call','name':'end_call'},
              *events, {'type':'policy_error','reason':'connection_closed'}]
    report=SimpleNamespace(check_results=checks,termination_reason=ending,error='',
                           events=[{'side':'target',**e} for e in timeline])
    result=check_policy_at_hangup(case,report)
    if unknown:
        assert result[0]['pass'] is None and result[0]['skipped']
        assert result[1]['pass'] is None and result[1]['skipped']
        assert result[-1]['status']=='incomplete_at_hangup' and result[-1]['pass']
        assert result[2]==checks[2]
    else:
        assert result==checks
    assert report.check_results==checks


def test_complete_monitoring_keeps_the_normal_rule_verdicts():
    checks=[{'type':'policy_flag','value':'r','pass':True}]
    case=SimpleNamespace(checks=checks)
    report=SimpleNamespace(check_results=checks,termination_reason='completed',error='',events=[
        {'side':'target','type':'tool_call','name':'end_call'},
        {'side':'target','type':'policy_settled','policy_version':1,'checked_version':1,'healthy':True}])
    assert check_policy_at_hangup(case,report)==checks


@pytest.mark.parametrize("missing", ["asr", "tool_call"])
def test_hangup_requires_a_monitored_source_and_target_end_call(missing):
    checks = [{"type": "policy_flag", "value": "required", "pass": False}]
    case = SimpleNamespace(checks=checks)
    events = [
        {"type": "asr", "policy_version": 2},
        {"type": "tool_call", "name": "end_call"},
        {"type": "policy_error", "reason": "connection_closed"},
    ]
    report = SimpleNamespace(
        check_results=checks, termination_reason="completed", error="",
        events=[{"side": "target", **event} for event in events if event["type"] != missing],
    )
    assert check_policy_at_hangup(case, report) == checks
