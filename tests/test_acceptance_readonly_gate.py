import httpx
import pytest

from scripts.acceptance_readonly import ReadOnlyGate


@pytest.fixture
def gate():
    return ReadOnlyGate('https://mes.test/Modeling','https://mes.test/Query','https://mes.test/Shopfloor')


@pytest.mark.parametrize('method,path,body',[
    ('GET','/Modeling/api/Specs',None),
    ('GET','/Shopfloor/swagger/Split/swagger.json',None),
    ('POST','/Query/api/MfgOrderInquiry',{'mfgOrder':{'name':'MO'},'infoDepth':4}),
    ('POST','/Query/api/AdHoc',{'queryText':"SELECT ContainerId FROM Container WHERE ContainerName=N'DELETE; -- literal'"}),
])
def test_known_read_requests_allowed(gate,method,path,body):
    gate.validate(httpx.Request(method,'https://mes.test'+path,json=body))


@pytest.mark.parametrize('method,path,body',[
    ('POST','/Shopfloor/api/Split',{'execute':False}),
    ('POST','/Shopfloor/api/CollectData',{}),
    ('PUT','/Modeling/api/Specs/id',{}),
    ('DELETE','/Modeling/api/Specs/id',None),
    ('POST','/Query/api/MfgOrderInquiry/PerformEvent',{}),
    ('POST','/Query/api/AdHoc',{'queryText':'DELETE FROM Container'}),
    ('POST','/Query/api/AdHoc',{'queryText':'SELECT * INTO Copy FROM Container'}),
    ('POST','/Query/api/AdHoc',{'queryText':'SELECT 1; DROP TABLE Container'}),
    ('POST','/Query/api/AdHoc',{'queryText':'SELECT 1 -- comments are not accepted'}),
])
def test_mutations_and_dispatch_paths_blocked(gate,method,path,body):
    with pytest.raises(RuntimeError):
        gate.validate(httpx.Request(method,'https://mes.test'+path,json=body))


def test_external_document_locator_never_authorized(gate):
    with pytest.raises(RuntimeError):
        gate.validate(httpx.Request('GET','https://files.test/instructions.pdf'))
