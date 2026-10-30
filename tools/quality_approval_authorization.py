"""Read the native role/org authorization function; never accept an actor override.

Transport credentials are copied only into the executor's ephemeral binding box.
They are never put in a plan, preview, digest, log or tool parameter.
"""
import copy
import re
from core import http_client as http
from tools import quality as q,quality_approvals as a,quality_event_triage as t
from tools.quality_spc_annotations import iid,schema_check
from tools.quality_definitions import resources
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SCHEMA={'EmployeeRole':'EmployeeRoleId,EmployeeId,RoleId,OrganizationId,PropagateToChildOrgs,IsFrozen,ChangeCount'}


def role_opt_in(args):
    value=args.get('allow_role_substitution',False)
    if type(value) is not bool:raise ValueError('allow_role_substitution须为明确布尔值。')
    return value


def entry_policy(entry,args):
    allow=role_opt_in(args)
    from tools.quality_approval_delegation import opted
    delegation=opted(args)
    if delegation and allow:raise ValueError('一次转派后的指定员工审批与角色替代不能同时选择。')
    if entry.get('DelegationTaskId') is not None:raise ValueError('当前角色替代入口不处理委托任务。')
    option=a.integer(entry['SubstituteOption'],'审批替代策略')
    if delegation:
        if option!=1:raise ValueError('一次转派后签批仅支持Named1当前指定人员。')
        return 'delegated'
    if entry.get('AssignedApproverId') not in (None,entry.get('ApproverId')):
        raise ValueError('原分配人与当前审批人不同，须明确受委托审批及原生转派历史；不猜测身份链。')
    if option==1:return 'named'
    if option==2 and allow:return 'role'
    raise ValueError('审批条目不允许当前方式；角色替代须原配置为Role2且明确allow_role_substitution=true。')


def capture_headers(service='SignApproval'):
    if service not in ('SignApproval','NCRUpdate','DelegateTask','UpdateEvent','UpdateEventData'):raise ValueError('不支持的签名会话绑定服务。')
    headers=dict(http.get_headers())
    if not isinstance(headers.get('Authorization'),str) or not headers['Authorization'].strip():
        raise ValueError('角色审批缺少已配置的认证会话，不接受匿名会话或员工参数覆盖。')
    # An explicit Cookie header (including an empty value) also prevents a shared
    # client's cookie jar from silently changing the session after the preview.
    prepared=http.get_client().build_request('POST',CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')+'/api/'+service,headers=headers)
    headers['Cookie']=prepared.headers.get('cookie','')
    return headers


async def definition():
    rows=await q.sql_rows("SELECT TOP 2 o.object_id AS FunctionId,CONVERT(varchar(64),HASHBYTES('SHA2_256',m.definition),2) AS DefinitionHash "
        "FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id JOIN sys.sql_modules m ON m.object_id=o.object_id "
        "WHERE s.name=N'dbo' AND o.name=N'csiAuthGetAllowedUsers' AND o.type IN ('IF','TF') ORDER BY o.object_id",2)
    if len(rows)!=1 or not isinstance(rows[0],dict):raise ValueError('当前原生角色授权函数不存在、不唯一或定义不可核实。')
    row=rows[0];key=a.integer(row.get('FunctionId'),'原生授权函数ID',1);fingerprint=row.get('DefinitionHash')
    if not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9A-Fa-f]{64}',fingerprint):
        raise ValueError('原生授权函数定义不可核实；需要只读定义权限，不回退为直接角色表猜测。')
    return dict(function_id=key,definition_sha256=fingerprint.lower())


async def membership(actor_id,organization,role):
    """Same function and argument order as ValidateEmployeeRoleOrgCombination."""
    actor_id=iid(actor_id);await schema_check(SCHEMA)
    actor=await t.asset('owner',key=actor_id)
    function=await definition()
    rows=await q.sql_rows('SELECT TOP 101 '+SCHEMA['EmployeeRole']+
        f" FROM EmployeeRole WHERE EmployeeId='{actor_id}' ORDER BY EmployeeRoleId",101)
    if len(rows)>100:raise ValueError('当前员工角色配置超过100项，不以部分配置准备审批。')
    a.complete(rows,SCHEMA['EmployeeRole']);a.unique(rows,'EmployeeRoleId')
    clean=[]
    for raw in rows:
        row={k:copy.deepcopy(raw[k]) for k in SCHEMA['EmployeeRole'].split(',')}
        for key in ('EmployeeRoleId','EmployeeId','RoleId'):row[key]=iid(row[key])
        if row['EmployeeId']!=actor_id:raise ValueError('角色配置不属于原生返回的当前员工。')
        row['OrganizationId']=a.optional(row['OrganizationId'])
        for key in ('PropagateToChildOrgs','IsFrozen'):row[key]=a.i.wire_bool(row[key],key)
        row['ChangeCount']=a.integer(row['ChangeCount'],'员工角色版本');clean.append(row)
    org_name=q.text(organization['Name'],'原组织名称');role_name=q.text(role['Name'],'原审批角色名称')
    result=await q.sql_rows('SELECT COUNT(*) AS RowCount FROM dbo.csiAuthGetAllowedUsers('+q.literal(org_name)+','+q.literal(role_name)+
        f") WHERE EmployeeId='{actor_id}'",1)
    if len(result)!=1 or not isinstance(result[0],dict):raise ValueError('原生角色/组织授权查询未返回唯一计数。')
    count=a.integer(result[0].get('RowCount'),'原生授权计数')
    if not 1<=count<=2147483647:raise ValueError('当前员工未获原角色及原组织的原生授权。')
    if await definition()!=function:raise ValueError('原生授权函数在核验期间变化。')
    return dict(actor=actor,organization_id=iid(organization['InstanceID']),role_id=iid(role['InstanceID']),
        authorization_source='dbo.csiAuthGetAllowedUsers(Organization.Name,Role.Name)',native_authorized_count=count,
        authorization_function=function,employee_roles=sorted(clean,key=lambda r:r['EmployeeRoleId']),
        role_organization_membership_verified=True,delegation_verified=False,extra_signature_performed=False)


def authorization_resources(proof):
    return sorted(resources(proof['actor']['entity'],proof['actor']))
