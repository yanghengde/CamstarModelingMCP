"""Replay three real MES rejections; never infer failure from absence alone."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tools import quality_inspection_event_rejection as r,quality_inspection_event as e,quality_plans as p

CAPTURE=json.loads(Path('tests/fixtures/inspection_event_rejections.json').read_text(encoding='utf8'))['cases']


def setup(monkeypatch,name):
    case=copy.deepcopy(CAPTURE[name]);snap=case['snapshot'];evidence=case['evidence']
    monkeypatch.setattr(e,'source',AsyncMock(return_value=snap['source']))
    monkeypatch.setattr(e.triage,'asset',AsyncMock(return_value=snap['organization']))
    monkeypatch.setattr(e,'roots',AsyncMock(return_value=([],False)))
    monkeypatch.setattr(e,'duplicates',AsyncMock(return_value=[]))
    monkeypatch.setattr(e.lifecycle,'numbering',AsyncMock(return_value={k:v for k,v in snap['numbering'].items()if k!='persistent_mapping'}))
    state={'events':[],'history':[],'maps':evidence['numbering_maps']}
    async def sql(query,maximum):
        if ' FROM NumberingRuleMap 'in query:return copy.deepcopy(state['maps'])
        if ' FROM Event WHERE 'in query:return copy.deepcopy(state['events'])
        if ' FROM CreateEventHistory WHERE 'in query:return copy.deepcopy(state['history'])
        raise AssertionError(query)
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(side_effect=sql))
    return case,state


@pytest.mark.parametrize('name',CAPTURE)
def test_real_rejection_with_unchanged_source_and_no_event_matches(monkeypatch,name):
    case,_=setup(monkeypatch,name)
    proof=asyncio.run(r.proof(case['snapshot'],case['outcome']))
    # The first capture predates the explanatory label/native_error_code key;
    # immutable response, configuration and business evidence remain identical.
    assert {k:v for k,v in proof.items()if k not in ('basis','native_error_code')}=={k:v for k,v in case['evidence'].items()if k not in ('basis','native_error_code')}
    assert proof['native_error_code']==json.loads(case['outcome']['raw_response'])['ErrorCode']
    assert proof['exact_native_validation_rejection']and proof['no_native_creation_history']


@pytest.mark.parametrize('change',['http','truncated','timeout','interrupted','no_submission','unknown_code','wrong_service','extra_error','changed_source','changed_org','event','history','root','duplicate','mapping','rule'])
def test_unknown_partial_or_changed_evidence_never_unlocks(monkeypatch,change):
    case,state=setup(monkeypatch,'authorization');out=case['outcome']
    if change=='http':out['http_status']=500
    elif change=='truncated':out['response_truncated']=True
    elif change=='timeout':out['error']='Timeout'
    elif change=='interrupted':out['interrupted']=True
    elif change=='no_submission':out['submission_attempted']=False
    elif change in ('unknown_code','wrong_service','extra_error'):
        reply=json.loads(out['raw_response'])
        if change=='unknown_code':reply['ErrorCode']='9999'
        elif change=='wrong_service':reply['Details'][0]['Message']=reply['Details'][0]['Message'].replace('Create Production Event','Other Service')
        else:reply['Details'].append(reply['Details'][0])
        out['raw_response']=json.dumps(reply)
    elif change=='changed_source':e.source.return_value=case['snapshot']['source']|{'samples':99}
    elif change=='changed_org':e.triage.asset.return_value=case['snapshot']['organization']|{'ChangeCount':99}
    elif change=='event':state['events']=[{}]
    elif change=='history':state['history']=[{}]
    elif change=='root':e.roots.return_value=([{}],False)
    elif change=='duplicate':e.duplicates.return_value=[{}]
    elif change=='mapping':state['maps'][0]['ChangeCount']='99'
    else:e.lifecycle.numbering.return_value={'rule':{}}
    with pytest.raises(ValueError):asyncio.run(r.proof(case['snapshot'],out))


def test_formal_rejection_is_audit_only_and_original_plan_is_never_reusable(monkeypatch,tmp_path):
    case,_=setup(monkeypatch,'authorization');snap=case['snapshot'];snap['environment']=p.environment();fingerprint=p.digest(snap);pid='d'*32
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite3')
    with p.db_connect()as db:
        db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?)',(pid,fingerprint,json.dumps(snap),'unknown',0,json.dumps(case['outcome'])))
        db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(pid,'one-attempt',0,1))
    result=asyncio.run(r.resolve(pid,fingerprint,'Exact native authorization rejection; no account changes.','native:13369354'))
    assert result['state']=='native_authorization_rejected'and result['mes_write_performed']is False
    assert result['verified_success']is False and result['retry_allowed']is False
    assert result['original_plan_reusable']is False
    with pytest.raises(ValueError):asyncio.run(r.resolve(pid,fingerprint,'Cannot replay the same rejection audit.','native:13369354'))
