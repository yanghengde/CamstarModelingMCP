import asyncio
from unittest.mock import AsyncMock
import pytest
import httpx
from scripts import verify_split_contract as v

def body():return v.build_request('S','0000000000000001','T',1,0)
def echo():return {'Container':{'Name':'S','InstanceId':'0000000000000001'},'CloseWhenEmpty':'false','ReduceQty':'true','AutoNumber':'false','ToContainerDetails':[{'ToContainerName':'T','Qty':'1.0','Qty2':'0'}]}

def test_echo_not_execution():assert v.inspect_echo(echo(),body())==[]

@pytest.mark.parametrize('field,value',[('Qty','2'),('Qty2',None),('Qty',True),('Qty','NaN'),('ToContainerName','Other')])
def test_reject_detail(field,value):
 data=echo();data['ToContainerDetails'][0][field]=value
 assert v.inspect_echo(data,body())

@pytest.mark.parametrize('field,value',[('CloseWhenEmpty',True),('ReduceQty',False),('AutoNumber',None),('ToContainerDetails',[]),('Container',{})])
def test_flags_identity(field,value):
 data=echo();data[field]=value;assert v.inspect_echo(data,body())

@pytest.mark.parametrize('qty,qty2',[(0,0),(True,0),(1,-1),(float('nan'),0),(1,float('inf'))])
def test_invalid(qty,qty2):
 with pytest.raises(ValueError):v.build_request('S','0000000000000001','T',qty,qty2)


def test_fixed_execute_false_no_retry(monkeypatch):
 client=AsyncMock();client.__aenter__.return_value=client
 client.post.return_value=httpx.Response(200,json=echo())
 monkeypatch.setattr(v.httpx,'AsyncClient',lambda **kw:client)
 monkeypatch.setattr(v,'get_headers',lambda:{})
 result=asyncio.run(v.verify(body()))
 assert result['serialization_verified'] and not result['transaction_verified']
 assert client.post.call_args.kwargs['params']['execute']=='false'
 assert client.post.call_count==1


def test_connection_failure(monkeypatch):
 client=AsyncMock();client.__aenter__.return_value=client;client.post.side_effect=httpx.ConnectTimeout('timeout')
 monkeypatch.setattr(v.httpx,'AsyncClient',lambda **kw:client)
 monkeypatch.setattr(v,'get_headers',lambda:{})
 result=asyncio.run(v.verify(body()))
 assert result['error_type']=='ConnectTimeout' and not result['serialization_verified']
 assert client.post.call_count==1
