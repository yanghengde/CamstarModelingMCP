"""Restricted material split preparation using a verified non-executing serialization."""
import json
from decimal import Decimal
from tools.container_transfers import snapshot, amount, quantity, ref, echoed_bool, same_ref
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from tools.modeling_details import raw_json, _has_next_link


async def split_snapshot(container_name, new_container_name, qty, qty2, close_when_empty=False):
    amount(qty)
    if quantity({'status':{'Qty':qty2}}) is None or type(close_when_empty) is not bool:
        raise ValueError('第二数量须为有限非负数，关闭选项须明确布尔值。')
    if not new_container_name.strip() or new_container_name.strip()==container_name.strip() or len(new_container_name)>100:
        raise ValueError('新名称须明确、不同于来源且不超过100字符。')
    source=await snapshot(container_name)
    if (not source['family_complete'] or not source['parent_known'] or source['parent_id']
            or type(source['status'].get('Status')) is not int or source['status']['Status']!=1
            or any(r.get('ParentContainerId')==source['id'] for r in source['family'])):
        raise ValueError('仅支持无父子关系且Active的数量容器。')
    for field,value in [('Qty',qty),('Qty2',qty2)]:
        before=quantity(source,field)
        if before is None or before<Decimal(str(value)):
            raise ValueError('来源主/第二数量未知或余额不足。')
    for field in ('UOM','UOM2','Product','ContainerLevel','Spec'):
        value=source['status'].get(field)
        if not isinstance(value,dict) or not value.get('Name') or not (value.get('InstanceId') or value.get('InstanceID')):
            raise ValueError(f'{field} 未明确，不能生成执行计划。')
        if field in ('Product','Spec') and not value.get('Revision'):
            raise ValueError(f'{field} 版本未明确。')
    target=new_container_name.strip()
    escaped=target.replace("'","''")
    rows=json.loads(await request('POST','/api/AdHoc',base_url=CAMSTAR_QUERY_BASE_URL,
        body={'queryText':f"SELECT TOP 1 ContainerId,ContainerName FROM Container WHERE ContainerName=N'{escaped}'"},
        response_formatter=raw_json,retry_read_only=True))
    if not isinstance(rows,list) or rows:
        raise ValueError('新名称已存在或名称查询失败。')
    body={'container':ref(source),'autoNumber':False,'reduceQty':True,'closeWhenEmpty':close_when_empty,
          'toContainerDetails':[{'toContainerName':target,'qty':qty,'qty2':qty2}]}
    response=json.loads(await request('POST','/api/Split',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body=body,params={'execute':'false','$select':'Container,CloseWhenEmpty,ReduceQty,AutoNumber','$expand':'ToContainerDetails'},
        response_formatter=raw_json))
    data=response.get('data',response)
    if response.get('error') or data.get('error') or data.get('ErrorCode') or _has_next_link(response):
        raise ValueError('拆批预览错误或不完整。')
    if not same_ref(data.get('Container'),body['container']):
        raise ValueError('来源回显不匹配。')
    for field,expected in [('AutoNumber',False),('ReduceQty',True),('CloseWhenEmpty',close_when_empty)]:
        if not echoed_bool(data.get(field),expected):raise ValueError('拆批选项回显不匹配。')
    details=data.get('ToContainerDetails')
    if not isinstance(details,list) or len(details)!=1 or details[0].get('ToContainerName')!=target:
        raise ValueError('新容器明细回显不匹配。')
    for field,expected in [('Qty',qty),('Qty2',qty2)]:
        actual=details[0].get(field)
        number=Decimal(str(actual))
        if isinstance(actual,bool) or not number.is_finite() or number!=Decimal(str(expected)):
            raise ValueError('拆批数量回显不匹配。')
    return {'environment':CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'operation':'Split','body':body,
            'parent':source,'children':[],'reserved_names':[target],
            'quantity_effect':{f:{'before':str(quantity(source,f)), 'after_if_accepted':str(quantity(source,f)-Decimal(str(v)))}
                               for f,v in [('Qty',qty),('Qty2',qty2)]}}
