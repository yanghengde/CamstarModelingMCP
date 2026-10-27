import asyncio
import json
import pytest
from scripts.prepare_component_issue_fixture import selection, prepare


@pytest.mark.parametrize('bad',['multiple','truncated','missing','paging'])
def test_start_references_must_be_unique_complete(bad):
    table={'RecordCount':1,'RowCount':1,'SelectionValuesEx':[{'Name':'P','InstanceId':'0000000000000001'}]}
    data={'Details':{'Owner':table}}
    if bad=='multiple':table['SelectionValuesEx']*=2
    if bad=='truncated':table['RecordCount']=2
    if bad=='missing':table['SelectionValuesEx'][0].pop('InstanceId')
    if bad=='paging':table['@odata.nextLink']='untrusted'
    with pytest.raises(ValueError):selection(json.dumps(data),'Owner')


def test_existing_mutation_journal_prevents_replay(tmp_path):
    prefix='CDX-T05-REPLAY'
    (tmp_path/(prefix+'.json')).write_text('{"state":"unknown"}')
    with pytest.raises(ValueError,match='Journal already exists'):
        asyncio.run(prepare(prefix,tmp_path,True))


@pytest.mark.parametrize('prefix',['REAL-PRODUCTION',"CDX-T05-X';DELETE",'CDX-T05-../../'])
def test_fixture_prefix_isolated_and_path_safe(tmp_path,prefix):
    with pytest.raises(ValueError):asyncio.run(prepare(prefix,tmp_path,True))


@pytest.mark.parametrize('response',['Request timed out','HTTP 400 Error Response: {"ErrorCode":"13369346","Details":[{"Message":"Other field error"}]}'])
def test_resume_does_not_replay_uncertain_or_undiagnosed_mutation(tmp_path,monkeypatch,response):
    from scripts import prepare_component_issue_fixture as script
    prefix='CDX-T05-UNCERTAIN'
    journal={'environment':script.CAMSTAR_BASE_URL.rstrip('/'),'state':'stopped_without_retry',
             'attempts':[{'kind':'MfgOrder','state':'unknown_or_failed','response':response}]}
    (tmp_path/(prefix+'.json')).write_text(json.dumps(journal),encoding='utf-8')
    with pytest.raises(ValueError):asyncio.run(prepare(prefix,tmp_path,True,True))


def test_continue_start_never_replays_prior_container_attempt(tmp_path):
    from scripts import prepare_component_issue_fixture as script
    prefix='CDX-T05-START-REPLAY'
    journal={'environment':script.CAMSTAR_BASE_URL.rstrip('/'),'attempts':[
        {'kind':'MfgOrder','response':'{"value":"MfgOrder added successfully"}'},
        {'kind':'Container','state':'unknown_or_failed'}]}
    (tmp_path/(prefix+'.json')).write_text(json.dumps(journal),encoding='utf-8')
    with pytest.raises(ValueError,match='no previous Container attempt'):
        asyncio.run(prepare(prefix,tmp_path,True,False,True))
