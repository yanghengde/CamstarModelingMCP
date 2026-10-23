import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from tools import collection_reconciliation as cr, collection_submission as cs, get_tool_func

CID='0004108000000052'
DID='001c618000000001'
PID='001c678000000001'
TID='0000000000000001'


def setup(monkeypatch,tmp_path,rows=None,state='unknown'):
    monkeypatch.setattr(cs,'PLAN_DB',tmp_path/'plans.sqlite')
    snap={'environment':cs.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'body':{'container':{'name':'C','instanceId':CID},'dataCollectionDef':{'instanceId':DID},'parametricData':{'dataPointDetails':[{'dataPoint':{'name':'X','instanceId':PID},'dataValue':'1'}]}},'validation':{'checks':[{'point_instance_id':PID,'data_type':3}]}}
    with cs.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',('p',cs.digest(snap),json.dumps({'snapshot':snap}),state,0,None))
    item={'DataPointHistoryDetailId':'detail1','DataPointHistoryId':'group1','HistoryMainlineId':'main1','ContainerId':CID,'TxnId':TID,'DataCollectionDefId':DID,'DataPointId':PID,'DataName':'X','DataType':'3','DataValue':'1.000'}
    mock=AsyncMock(return_value=json.dumps({'page_complete':True,'rows':[item] if rows is None else rows,'has_more':False}))
    monkeypatch.setattr(cr,'get_container_collection_history',mock)
    return item,mock


def run(txn=TID):return json.loads(asyncio.run(cr.reconcile_collection_submission('p',txn)))


def test_match_keeps_unknown(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    result=run()
    assert result['comparison']=='values_match' and result['comparison_complete']
    assert not result['verified_success'] and not result['retry_allowed'] and not result['state_changed']
    with cs.db_connect() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='unknown'
    assert get_tool_func('reconcile_collection_submission')


@pytest.mark.parametrize('field,value,reason',[('DataValue','2','value_mismatch'),('DataCollectionDefId','other','definition_mismatch'),('DataType','4','type_mismatch'),('DataName','Y','point_name_mismatch'),('DataPointId','other','unexpected_point'),('HistoryMainlineId',None,'ambiguous_history_group')])
def test_mismatch(monkeypatch,tmp_path,field,value,reason):
    item,mock=setup(monkeypatch,tmp_path);item[field]=value
    mock.return_value=json.dumps({'page_complete':True,'rows':[item],'has_more':False})
    result=run()
    assert result['comparison']=='mismatch'
    assert reason in {r for d in result['differences'] for r in d['reasons']}


def test_empty_not_failure(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path,rows=[])
    r=run();assert r['comparison']=='no_records' and not r['retry_allowed']


@pytest.mark.parametrize('state',['ready','completed'])
def test_unsubmitted_rejected(monkeypatch,tmp_path,state):
    _,mock=setup(monkeypatch,tmp_path,state=state)
    assert 'error' in run();mock.assert_not_called()


def test_bad_id_no_io(monkeypatch,tmp_path):
    _,mock=setup(monkeypatch,tmp_path)
    assert 'error' in run("x' OR 1=1")
    mock.assert_not_called()


def test_duplicate_rows_rejected(monkeypatch,tmp_path):
    item,mock=setup(monkeypatch,tmp_path)
    mock.return_value=json.dumps({'page_complete':True,'rows':[item,item],'has_more':False})
    assert 'error' in run()


def test_environment_and_integrity(monkeypatch,tmp_path):
    _,mock=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(cs,'CAMSTAR_SHOPFLOOR_BASE_URL','https://other/Shopfloor')
    assert 'error' in run();mock.assert_not_called()


def test_numeric_and_string_comparison():
    assert cr.same_value('1','1.00',3)
    assert not cr.same_value('1','1.00',4)
    assert not cr.same_value('1',True,3)
    assert not cr.same_value('NaN','NaN',3)

def test_page_limit_is_not_match(monkeypatch,tmp_path):
    item,mock=setup(monkeypatch,tmp_path)
    pages=[]
    for offset in (0,100,200):
        rows=[dict(item,DataPointHistoryDetailId=str(i)) for i in range(offset,offset+100)]
        pages.append(json.dumps({'page_complete':True,'rows':rows,'has_more':True,'next_offset':offset+100}))
    mock.side_effect=pages
    result=run()
    assert result['comparison']=='incomplete' and not result['verified_success']
    assert mock.call_count==3


def test_query_failure_and_wrong_transaction(monkeypatch,tmp_path):
    item,mock=setup(monkeypatch,tmp_path)
    mock.return_value=json.dumps({'error':'denied'})
    assert 'error' in run()
    item['TxnId']='other'
    mock.return_value=json.dumps({'page_complete':True,'rows':[item],'has_more':False})
    assert 'error' in run()
