import asyncio
import copy
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock

import httpx
import pytest
from core import carrier_factory_wcf as w

NAME='CDX-L4-TEST-FACTORY';ID='0000000000000001'


def run(coro):return asyncio.run(coro)


def desired():
    prefix=NAME.removesuffix('-FACTORY')
    names={'isCarrierContainerLevel':prefix+'-PARENT','isCarrierNumberingRule':prefix+'-CARRULE',
           'isCarrierOwner':'PRODUCTION','isCarrierStartReason':'NORMAL','isCarrierWorkflow':prefix+'-WF'}
    return w.changes(prefix,{k:{'Name':n,'ID':ID,**({'Revision':'1','RevisionOfRecord':False} if k=='isCarrierWorkflow' else {})}for k,n in names.items()})


def test_production_factory_and_inexact_references_rejected():
    with pytest.raises(ValueError):w.validate_target('PRODUCTION',ID)
    with pytest.raises(ValueError):w.validate_target(NAME,'not-an-id')
    d=desired();refs={k.removeprefix('FactoryChanges_'):v for k,v in d.items() if 'Name'in v}
    refs['isCarrierOwner']['Name']='OTHER'
    with pytest.raises(ValueError):w.changes(NAME.removesuffix('-FACTORY'),refs)


def test_atomic_commit_uses_documented_load_execute_and_configured_origin(monkeypatch):
    result='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:o="Camstar.WCF.ObjectStack"><s:Body><o:IsSuccess>true</o:IsSuccess></s:Body></s:Envelope>'
    client=type('Client',(),{'post':AsyncMock(return_value=httpx.Response(200,text=result))})()
    monkeypatch.setattr(w,'get_client',lambda:client);monkeypatch.setattr(w,'CAMSTAR_BASE_URL','https://mes.example/Modeling')
    monkeypatch.setattr(w,'CAMSTAR_USERNAME','test-user');monkeypatch.setattr(w,'CAMSTAR_PASSWORD','test-secret')
    run(w.commit(NAME,ID,desired()))
    assert client.post.await_count==1
    call=client.post.call_args;assert call.args[0]=='https://mes.example/CamstarWCFServices/FactoryMaintService.svc'
    assert call.kwargs['follow_redirects'] is False
    root=ET.fromstring(call.kwargs['content']);ns={'o':'Camstar.WCF.ObjectStack'}
    assert [x.text for x in root.findall('.//o:ServiceMethod',ns)]==['Load','ExecuteTransaction']
    assert root.find('.//o:FactoryMaint_ObjectToChange/o:ID',ns).text==ID
    assert root.find('.//o:FactoryChanges_isCarrierNumberingRule/o:Name',ns).text=='CDX-L4-TEST-CARRULE'


def test_failed_commit_never_retries_or_returns_credentials(monkeypatch):
    client=type('Client',(),{'post':AsyncMock(return_value=httpx.Response(503,text='private content'))})()
    monkeypatch.setattr(w,'get_client',lambda:client)
    with pytest.raises(ValueError,match='503') as error:run(w.commit(NAME,ID,desired()))
    assert client.post.await_count==1 and 'private content' not in str(error.value)


def test_configuration_change_or_extra_fields_prevents_submission(monkeypatch):
    monkeypatch.setattr(w,'load',AsyncMock(return_value={'selected':{'changed':True}}))
    commit=AsyncMock();monkeypatch.setattr(w,'commit',commit)
    with pytest.raises(ValueError):run(w.configure(NAME,ID,{},desired()))
    assert not commit.called
    monkeypatch.setattr(w,'load',AsyncMock(return_value={}))
    d=desired();d['FactoryChanges_isCarrierCapacity']={'Value':99}
    with pytest.raises(ValueError):run(w.configure(NAME,ID,{},d))
    assert not commit.called
