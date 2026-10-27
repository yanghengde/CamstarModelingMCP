import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from scripts import verify_component_issue_contract as v


@pytest.fixture
def recorded():
    # Captured execute=false response: this server represents Shopfloor scalar
    # booleans/quantities as strings, unlike the Query response JSON numbers.
    path=Path(__file__).resolve().parents[1]/'docs'/'t05-fixtures'/'component-serialization.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    return copy.deepcopy(data['requirement_before']),copy.deepcopy(data['response'])


@pytest.mark.parametrize('bad',[None,'qty','identity','flag','ror'])
def test_fixed_nonexecuting_diagnostic_and_typed_echo(recorded,tmp_path,monkeypatch,bad):
    req,response=recorded
    if bad=='qty':response['data']['IssueActualDetails'][0]['QtyIssued']='3'
    if bad=='identity':response['data']['IssueActualDetails'][0]['ToContainer']['InstanceId']='0000000000000001'
    if bad=='flag':response['data']['PreventOverIssue']='false'
    if bad=='ror':response['data']['IssueActualDetails'][0]['Product']['UseRor']='true'
    monkeypatch.setattr(v,'get_component_requirement',AsyncMock(return_value=json.dumps(req)))
    request=AsyncMock(return_value=json.dumps(response));monkeypatch.setattr(v,'request',request)
    monkeypatch.setattr(v,'close_client',AsyncMock())
    output=tmp_path/'result.json'
    asyncio.run(v.verify(req['data']['ParentContainer']['Name'],req['data']['IssueDetails']['Product']['Name'],'1',2,output))
    result=json.loads(output.read_text(encoding='utf-8'))
    assert result['serialization_verified']==(bad is None)
    assert not result['transaction_verified'] and result['quantity_state_unchanged']
    assert request.call_args.args==('POST','/api/ComponentIssue')
    assert request.call_args.kwargs['params']['execute']=='false' and request.call_count==1
    assert result['business_blockers']  # ValidSequence=false is not overridden.


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-1,'2'])
def test_invalid_input_quantity_not_sent(recorded,value):
    with pytest.raises(ValueError):v.build(recorded[0],value)


def test_insufficient_requirement_never_builds(recorded):
    with pytest.raises(ValueError):v.build(recorded[0],3)
