import asyncio,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfers as ct, get_tool_func
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES


@pytest.fixture(autouse=True)
def no_live_ancestors(monkeypatch):
    monkeypatch.setattr(ct, "check_level_rules", AsyncMock(return_value={"complete":False,"checks":[]}))
    monkeypatch.setattr(ct, "ancestor_chain", AsyncMock(return_value={"complete":False,"nodes":[],"cycle":False}))


def snap(name,qty=10,parent=None):
    return {'id':name,'container':{'Name':name},'status':{'Qty':qty,'Product':{'Name':'P','Revision':'1'},'ContainerLevel':{'Name':'L'},'UOM':None,'UOM2':None,'Spec':None},'family_complete':True,'parent_known':True,'parent_id':parent}


def run(coro):
    r=json.loads(asyncio.run(coro));assert not r['executable'];return r


def test_split_flag_effects(monkeypatch):
    monkeypatch.setattr(ct,'snapshot',AsyncMock(return_value=snap('A')))
    r=run(ct.preview_container_split('A','B',3,reduce_qty=False))
    assert r['quantity_effect']['source_after_if_accepted']=='10'
    assert r['new_name_available'] is None and 'request_preview' not in r
    r=run(ct.preview_container_split('A','B',3))
    assert r['quantity_effect']['source_after_if_accepted']=='7'
    r=run(ct.preview_container_split('A','B',11))
    assert r['quantity_effect']['source_after_if_accepted'] is None


@pytest.mark.parametrize('qty',[0,-1,True,float('nan'),float('inf')])
def test_bad_quantity_no_read(monkeypatch,qty):
    mock=AsyncMock();monkeypatch.setattr(ct,'snapshot',mock)
    assert 'error' in run(ct.preview_container_split('A','B',qty));mock.assert_not_called()


def test_combine_balance_unknown_unit(monkeypatch):
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap('B',2),snap('A',10)]))
    r=run(ct.preview_container_combine('B','A',3))
    assert r['quantity_effect']['target_after_if_accepted']=='5'
    assert r['quantity_effect']['source_after_if_accepted']=='7'
    assert r['comparisons']['UOM']=='unknown'


def test_self_combine_rejected(monkeypatch):
    mock=AsyncMock();monkeypatch.setattr(ct,'snapshot',mock)
    assert 'error' in run(ct.preview_container_combine('A','A',1));mock.assert_not_called()


@pytest.mark.parametrize('children',[[],['A'],['B','B'],['']])
def test_bad_family_input(monkeypatch,children):
    mock=AsyncMock();monkeypatch.setattr(ct,'snapshot',mock)
    assert 'error' in run(ct.preview_container_associate('A',children));mock.assert_not_called()


def test_disassociate_only_explicit_direct_children(monkeypatch):
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap('A'),snap('B',parent='A')]))
    r=run(ct.preview_container_disassociate('A',['B']))
    assert r['disassociate_all'] is False
    assert r['relationships'][0]['parent_before']=='A'
    assert r['relationships'][0]['parent_after_if_accepted'] is None


def test_associate_existing_parent_blocked(monkeypatch):
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap('A'),snap('B',parent='Other')]))
    r=run(ct.preview_container_associate('A',['B']))
    assert any('已有父容器' in b for b in r['blockers'])


def test_missing_parent_is_unknown(monkeypatch):
    child=snap('B');child['parent_known']=False
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap('A'),child]))
    r=run(ct.preview_container_disassociate('A',['B']))
    assert r['relationships'][0]['parent_before']=='unknown'


def test_snapshot_identity_and_missing_family(monkeypatch):
    cid='0000000000000001'
    monkeypatch.setattr(ct,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'ContainerStatusDetails':{'Container':{'Name':'A','InstanceId':cid},'Qty':1}}})))
    monkeypatch.setattr(ct,'get_container_trace',AsyncMock(return_value='{"sections":{}}'))
    r=asyncio.run(ct.snapshot('A'));assert not r['parent_known'] and not r['family_complete']
    with pytest.raises(ValueError):asyncio.run(ct.snapshot('Wrong'))


def test_registry_and_chinese_routing():
    names={'preview_container_'+n for n in ['split','combine','associate','disassociate']}
    assert all(get_tool_func(n) for n in names)
    selected=select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[{'role':'user','content':'预览装箱和拆批'}])
    assert names <= {t['function']['name'] for t in selected}

@pytest.mark.parametrize('operation',['Associate','Disassociate','Combine'])
def test_server_preview_fixed_no_execute(monkeypatch,operation):
    container={'name':'P','instanceId':'p'}
    child={'name':'C','instanceId':'c'}
    body={'container':container}
    data={'Container':{'Name':'P','InstanceId':'p'}}
    if operation=='Combine':
        body.update(closeWhenEmpty=False,fromContainerDetails=[{'fromContainer':child,'qty':2,'combineAllQty':False,'closeWhenEmpty':False}])
        data.update(CloseWhenEmpty='false',FromContainerDetails=[{'FromContainer':{'Name':'C','InstanceId':'c'},'Qty':'2.0','CombineAllQty':'false','CloseWhenEmpty':'false'}])
    else:
        body['childContainers']=[child];data['ChildContainers']=[{'Name':'C','InstanceId':'c'}]
        if operation=='Disassociate':body['disassociateAll']=False;data['DisassociateAll']='false'
    req=AsyncMock(return_value=json.dumps({'data':data}));monkeypatch.setattr(ct,'request',req)
    out=ct.base(operation)
    asyncio.run(ct.service_preview(out,body))
    assert out['server_echo_verified'] and not out['executable']
    assert req.call_args.kwargs['params']['execute']=='false'
    assert req.call_args.args==('POST','/api/'+operation)
    assert 'retry_read_only' not in req.call_args.kwargs


@pytest.mark.parametrize('failure',['missing','wrong','duplicate','all','error','next'])
def test_service_preview_rejects_bad_family_echo(monkeypatch,failure):
    body={'container':{'name':'P','instanceId':'p'},'childContainers':[{'name':'C','instanceId':'c'}],'disassociateAll':False}
    data={'Container':{'Name':'P','InstanceId':'p'},'ChildContainers':[{'Name':'C','InstanceId':'c'}],'DisassociateAll':False}
    if failure=='missing':data.pop('ChildContainers')
    if failure=='wrong':data['ChildContainers'][0]['InstanceId']='wrong'
    if failure=='duplicate':data['ChildContainers']*=2
    if failure=='all':data['DisassociateAll']=True
    if failure=='error':data['error']='denied'
    if failure=='next':data['ChildContainers@odata.nextLink']='next'
    req=AsyncMock(return_value=json.dumps(data));monkeypatch.setattr(ct,'request',req)
    out=ct.base('Disassociate');asyncio.run(ct.service_preview(out,body))
    assert not out['server_echo_verified'] and 'server_preview_error' in out
    assert req.call_count==1


def test_verify_service_opt_in(monkeypatch):
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap('P'),snap('C')]))
    req=AsyncMock();monkeypatch.setattr(ct,'request',req)
    run(ct.preview_container_associate('P',['C']))
    req.assert_not_called()
