"""Fixed execute=false ComponentIssue serialization diagnostic. No execution flag."""
import argparse
import asyncio
import json
import math
import re
import sys
from decimal import Decimal,InvalidOperation
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.component_issue import get_component_requirement
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request,close_client
from config import CAMSTAR_SHOPFLOOR_BASE_URL


def ref(value,revisioned=False,parent=False):
    if not isinstance(value,dict):raise ValueError('Missing reference.')
    cid=value.get('InstanceId') or value.get('InstanceID')
    if not isinstance(value.get('Name'),str) or not value['Name'] or not isinstance(cid,str) or not re.fullmatch('[0-9a-fA-F]{16}',cid):
        raise ValueError('Reference identity incomplete.')
    result={'name':value['Name'],'instanceId':cid}
    if revisioned:
        ror=value.get('UseROR',value.get('UseRor'))
        if not value.get('Revision') or not (ror is None or ror is False or type(ror) is str and ror=='false'):
            raise ValueError('Explicit product revision required.')
        result['revision']=value['Revision']
    if parent:
        owner=(value.get('Parent') or {}).get('InstanceId')
        if not isinstance(owner,str) or not re.fullmatch('[0-9a-fA-F]{16}',owner):raise ValueError('BOM line owner is not known.')
        result['parent']={'instanceId':owner}
    return result


def number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Finite nonnegative quantity required.')
    return value


def echo_number(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):raise ValueError('Invalid echo quantity.')
    try:result=Decimal(str(value))
    except InvalidOperation:raise ValueError('Invalid echo quantity.') from None
    if not result.is_finite() or result<0:raise ValueError('Invalid echo quantity.')
    return result


def build(requirement,qty):
    if requirement.get('status')!='matched' or requirement.get('complete') is not True:raise ValueError('Complete matched requirement required.')
    qty=number(qty)
    if qty<=0:raise ValueError('Positive issue quantity required.')
    data=requirement['data'];detail=data['IssueDetails']
    if type(detail.get('IssueControl')) is not int or detail['IssueControl']!=5:raise ValueError('Only quantity-only mode 5 is supported.')
    required=number(detail.get('QtyRequired'));remaining=number(detail.get('NetQtyRequired'))
    number(detail.get('QtyIssued'));number(detail.get('ActualQtyIssued'))
    if qty>remaining or qty>required:raise ValueError('Requested quantity exceeds current requirement.')
    if any(number(detail.get(k))!=0 for k in ('Qty2Required','Qty2Issued','ActualQty2Issued','NetQty2Required')):
        raise ValueError('Nonzero or unknown secondary quantity is not supported.')
    target=ref(data['ParentContainer']);product=ref(detail['Product'],True);line=ref(detail['BOMLineItem'],parent=True);unit=ref(detail['UOM'])
    return {'container':target,'preventOverIssue':True,
            'serviceDetails':[{'bomLineItem':line,'product':product,'qtyRequired':required,'issueControl':5,'uom':unit}],
            'issueActualDetails':[{'bomLineItem':line,'product':product,'toContainer':target,'qtyIssued':qty,'qty2Issued':0}]}


async def verify(container,product,revision,qty,output):
    report={'serialization_verified':False,'transaction_verified':False,'execute':False,'request_sent':False}
    try:
        requirement=json.loads(await get_component_requirement(container,product))
        report['requirement_before']=requirement
        body=build(requirement,qty)
        if body['issueActualDetails'][0]['product']['name']!=product or body['issueActualDetails'][0]['product']['revision']!=revision:
            raise ValueError('Substitution or product revision mismatch is not supported.')
        report['request']={'path':'/api/ComponentIssue','params':{'execute':'false','$select':'Container,PreventOverIssue',
                           '$expand':'ServiceDetails,IssueActualDetails'},'body':body}
        report['business_blockers']=['Execute business semantics are not verified.']
        if requirement['data']['IssueDetails'].get('ValidSequence') is not True:
            report['business_blockers'].append('ValidSequence is not explicitly true; no execution is allowed.')
        report['request_sent']=True
        raw=await request('POST','/api/ComponentIssue',body=body,params=report['request']['params'],
                          base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
        response=json.loads(raw.split('Response: ',1)[-1]);report['response']=response
        data=response.get('data',response)
        if response.get('error') or response.get('ErrorCode') or data.get('ErrorCode') or _has_next_link(response):raise ValueError('Service echo error/incomplete.')
        flag=data.get('PreventOverIssue')
        if ref(data['Container'])!=body['container'] or not (flag is True or type(flag) is str and flag=='true'):
            raise ValueError('Target or flags changed in echo.')
        for key,expected in [('ServiceDetails',body['serviceDetails'][0]),('IssueActualDetails',body['issueActualDetails'][0])]:
            rows=data.get(key)
            if not isinstance(rows,list) or len(rows)!=1:raise ValueError('Unique service detail not echoed.')
            row=rows[0]
            if ref(row['BOMLineItem'],parent=True)!=expected['bomLineItem'] or ref(row['Product'],True)!=expected['product']:
                raise ValueError('BOM line/product identity changed in echo.')
            if key=='ServiceDetails':
                if echo_number(row.get('QtyRequired'))!=Decimal(str(expected['qtyRequired'])) or echo_number(row.get('IssueControl'))!=5 or ref(row['UOM'])!=expected['uom']:
                    raise ValueError('Requirement/unit not echoed.')
            elif (ref(row['ToContainer'])!=body['container'] or echo_number(row.get('QtyIssued'))!=Decimal(str(qty)) or echo_number(row.get('Qty2Issued'))!=0):
                raise ValueError('Actual issue intent not echoed.')
        report['serialization_verified']=True
    except Exception as error:report['error']=type(error).__name__+': '+str(error)
    finally:
        try:
            report['requirement_after']=json.loads(await get_component_requirement(container,product))
            before=report.get('requirement_before',{});after=report['requirement_after']
            if before.get('complete') is True and after.get('complete') is True:
                fields=('QtyRequired','QtyIssued','ActualQtyIssued','NetQtyRequired','Qty2Issued','ActualQty2Issued')
                report['quantity_state_unchanged']=all(number(before['data']['IssueDetails'].get(k))==number(after['data']['IssueDetails'].get(k)) for k in fields)
        except Exception as error:report['after_read_error']=type(error).__name__+': '+str(error)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        await close_client()
    print(json.dumps({k:report.get(k) for k in ('serialization_verified','transaction_verified','request_sent','error')}))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container',required=True);parser.add_argument('--product',required=True)
    parser.add_argument('--revision',required=True);parser.add_argument('--qty',required=True,type=float)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();result=asyncio.run(verify(args.container,args.product,args.revision,args.qty,args.output))
    sys.exit(0 if result['serialization_verified'] else 1)
