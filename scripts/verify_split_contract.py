"""Diagnostic only: fixed Split execute=false request, never an execution endpoint.

Run with explicit source identity, diagnostic target name, qty and qty2.
Successful echo verifies serialization, NOT transaction or business behavior.
"""
import argparse
import asyncio
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from config import CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import get_headers


def build_request(source, source_id, target, qty, qty2):
    if not source.strip() or not target.strip() or source.strip()==target.strip():
        raise ValueError('Source and diagnostic target names must be nonempty and distinct.')
    if not re.fullmatch(r'[0-9a-fA-F]{16}', source_id):
        raise ValueError('Source ID must be 16 hexadecimal characters.')
    for value, positive in [(qty,True),(qty2,False)]:
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or (value<=0 if positive else value<0):
            raise ValueError('Qty must be positive; Qty2 must be nonnegative; both must be finite.')
    return {'container':{'name':source.strip(),'instanceId':source_id},'closeWhenEmpty':False,
            'reduceQty':True,'autoNumber':False,
            'toContainerDetails':[{'toContainerName':target.strip(),'qty':qty,'qty2':qty2}]}


def inspect_echo(response, body):
    from decimal import Decimal, InvalidOperation
    errors=[]
    data=response.get('data',response) if isinstance(response,dict) else {}
    if not isinstance(data,dict) or data.get('error') or data.get('ErrorCode'):
        return ['service_error_or_invalid_response']
    container=data.get('Container') or {}
    if (not isinstance(container,dict) or container.get('Name')!=body['container']['name']
            or (container.get('InstanceId') or container.get('InstanceID'))!=body['container']['instanceId']):
        errors.append('container_mismatch')
    for field, expected in [('CloseWhenEmpty',False),('ReduceQty',True),('AutoNumber',False)]:
        value=data.get(field)
        if not (type(value) is bool and value==expected or value==str(expected).lower()):
            errors.append(field+'_missing_or_mismatch')
    details=data.get('ToContainerDetails')
    if not isinstance(details,list) or len(details)!=1 or not isinstance(details[0],dict):
        return errors+['details_missing_or_ambiguous']
    actual, expected=details[0],body['toContainerDetails'][0]
    if actual.get('ToContainerName')!=expected['toContainerName']:
        errors.append('target_name_mismatch')
    for field in ('Qty','Qty2'):
        try:
            value=actual.get(field)
            parsed=Decimal(str(value))
            if isinstance(value,bool) or not parsed.is_finite() or parsed!=Decimal(str(expected[field[0].lower()+field[1:]])):
                errors.append(field+'_mismatch')
        except (InvalidOperation,ValueError):
            errors.append(field+'_missing_or_invalid')
    from tools.modeling_details import _has_next_link
    if _has_next_link(response):
        errors.append('incomplete_response')
    return errors


async def verify(body):
    params={'execute':'false','$select':'Container,CloseWhenEmpty,ReduceQty,AutoNumber','$expand':'ToContainerDetails'}
    report={'date':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'transaction_execution_requested':False,
            'transaction_verified':False,'serialization_verified':False,'body':body,'params':params,
            'source_conflict':'Designer describes Split service details; Swagger warns system-generated. Echo alone cannot resolve Execute semantics.'}
    try:
        async with httpx.AsyncClient(verify=False,timeout=12) as client:
            response=await client.post(CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')+'/api/Split',headers=get_headers(),json=body,params=params)
        report['http_status']=response.status_code
        try:
            report['response']=response.json()
        except ValueError:
            report['error_type']='NonJsonResponse'
            return report
        report['echo_errors']=inspect_echo(report['response'],body)
        report['serialization_verified']=response.is_success and not report['echo_errors']
    except httpx.HTTPError as error:
        report['error_type']=type(error).__name__
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('source','source-id','target','output'):
        parser.add_argument('--'+arg,required=True)
    parser.add_argument('--qty',type=float,required=True)
    parser.add_argument('--qty2',type=float,required=True)
    args=parser.parse_args()
    body=build_request(args.source,args.source_id,args.target,args.qty,args.qty2)
    result=asyncio.run(verify(body))
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:result.get(k) for k in ('serialization_verified','transaction_verified','http_status','error_type')},ensure_ascii=True))


if __name__=='__main__':
    main()
