"""Classification diagnostics and submission guards; no native write adapter.

UpdateEvent 14001/14003 are read-only expressions. EventDetailToEvent and
UpdateEventDataToEvent contain no classification write mapping. A ref-shaped
Swagger property alone must never enable reclassification.
"""
import copy
from tools import quality as q,quality_inspection as i
from tools.quality_spc_annotations import iid,schema_check
from tools.process_context import ReadContext
from tools.quality_definitions import resources

MAP_COLUMNS=('EventClassificationSpecMapId,CDOTypeId,OrganizationId,ClassificationId,SubClassificationId,'
    'ChecklistTemplateId,FailureModeGroupId,OwnerId,RoleId,PEDefault,IsFrozen,ChangeCount')
SCHEMA={'EventClassificationSpecMap':MAP_COLUMNS,'Classification':'ClassificationId,ChangeCount',
    'SubClassification':'SubClassificationId,ChangeCount'}
KINDS={'ClassificationId':('Classifications','Classification','ClassificationId'),
    'SubClassificationId':('SubClassifications','SubClassification','SubClassificationId')}
SELECT='InstanceID,Name,IsFrozen'


def write_support():
    return dict(supported=False,standard_update_event_fields_read_only=True,native_write_mapping_verified=False,
        reason='标准UpdateEvent分类/子分类为只读表达式，EventDetail及UpdateEventData保存映射不含分类；未核实独立原生修改入口。',
        deployment_extension_verified=False,workaround_authorized=False)


async def reference(field,key=None,name=None):
    entity,table,column=KINDS[field];ctx=ReadContext(2)
    if key is None:
        name=q.text(name,'明确分类名称')
        found=await ctx.get(entity,params={'$select':SELECT,'$filter':'Name eq '+q.literal(name)[1:],'$top':2})
        rows=found.get('value')
        if not isinstance(rows,list) or len(rows)!=1 or q._has_next_link(found):raise ValueError('明确分类名称未唯一匹配，不选首行。')
        if rows[0].get('Name')!=name:raise ValueError('分类候选名称不符。')
        key=iid(q.identifier(rows[0]))
    key=iid(key)
    async def version():
        rows=await q.sql_rows(f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'",1)
        if len(rows)!=1 or iid(rows[0].get(column))!=key:raise ValueError('分类持久引用不存在或身份不符。')
        value=i.wire_int(rows[0].get('ChangeCount'),'分类配置版本')
        if value<0:raise ValueError('分类配置版本无效。')
        return value
    count=await version();root=await ctx.get(entity,key,{'$select':SELECT})
    if iid(q.identifier(root))!=key or name is not None and root.get('Name')!=name or q._has_next_link(root):raise ValueError('分类配置身份或名称不符。')
    state=dict(entity=entity,InstanceID=key,Name=q.text(root.get('Name')),IsFrozen=i.wire_bool(root.get('IsFrozen'),'分类冻结标志'),ChangeCount=count)
    if await version()!=count:raise ValueError('分类配置在读取期间变化。')
    return state


async def mappings(org_id):
    org_id=iid(org_id)
    rows=await q.sql_rows('SELECT TOP 101 '+MAP_COLUMNS+f" FROM EventClassificationSpecMap WHERE OrganizationId='{org_id}' ORDER BY EventClassificationSpecMapId",101)
    if len(rows)>100:raise ValueError('组织分类映射超过100条，不能使用部分清单。')
    clean=[];seen=set()
    for raw in rows:
        if not isinstance(raw,dict) or not set(MAP_COLUMNS.split(','))<=raw.keys():raise ValueError('组织分类映射字段缺失。')
        row={k:copy.deepcopy(raw[k]) for k in MAP_COLUMNS.split(',')}
        for f in ('EventClassificationSpecMapId','OrganizationId','ClassificationId','SubClassificationId'):row[f]=iid(row[f])
        if row['OrganizationId']!=org_id or row['EventClassificationSpecMapId'] in seen:raise ValueError('组织分类映射归属不符或ID重复。')
        seen.add(row['EventClassificationSpecMapId'])
        if i.wire_int(row['CDOTypeId'],'分类映射类型')!=8001:raise ValueError('组织分类映射不是标准8001基础类型。')
        row['CDOTypeId']=8001;row['ChangeCount']=i.wire_int(row['ChangeCount'],'分类映射版本')
        if row['ChangeCount']<0:raise ValueError('组织分类映射版本无效。')
        for f in ('ChecklistTemplateId','FailureModeGroupId','OwnerId','RoleId'):row[f]=None if row[f] in (None,'') else iid(row[f])
        for f in ('PEDefault','IsFrozen'):row[f]=i.wire_bool(row[f],f)
        clean.append(row)
    return sorted(clean,key=lambda r:r['EventClassificationSpecMapId'])


def assess(refs,maps):
    blockers=[]
    for field,state in refs.items():
        if state is None:blockers.append('missing_'+field)
        elif state['IsFrozen']:blockers.append('frozen_'+field)
    found=[m for m in maps if all(refs[k] is not None and m[k]==refs[k]['InstanceID'] for k in KINDS)]
    if len(found)!=1:blockers.append('organization_pair_not_unique')
    elif found[0]['IsFrozen']:blockers.append('frozen_mapping')
    return dict(valid=not blockers,blockers=blockers,matching_map_count=len(found),selected_mapping=found[0] if len(found)==1 else None,
        native_submission_permission_verified=False,
        frozen_policy='本受限提交入口保守拒绝冻结配置，不把IsFrozen解释为所有原生事务均禁止使用。')


async def configuration(organization,classes,proposal=None):
    await schema_check(SCHEMA);org_id=iid(organization['InstanceID']);maps=await mappings(org_id)
    refs={field:await reference(field,key=classes[field]) if classes[field] is not None else None for field in KINDS}
    out=dict(organization_id=org_id,references=refs,mappings=maps,current=assess(refs,maps),classification_write_support=write_support())
    if proposal is not None:
        names=dict(zip(KINDS,proposal));proposed={f:await reference(f,name=names[f]) for f in KINDS}
        out['proposal']=dict(references=proposed,assessment=assess(proposed,maps),changes_applied=False)
        for field,state in proposed.items():
            if await reference(field,key=state['InstanceID'],name=state['Name'])!=state:raise ValueError('拟选分类在核验期间变化。')
    if await mappings(org_id)!=maps:raise ValueError('组织分类映射在读取期间变化。')
    for field,state in refs.items():
        if state is not None and await reference(field,key=state['InstanceID'],name=state['Name'])!=state:raise ValueError('原分类配置在核验期间变化。')
    return out


def require_submission(config):
    if config['current']['valid'] is not True:
        raise ValueError('原草稿分类/子分类或原组织唯一组合未通过受限提交核验：'+','.join(config['current']['blockers'])+'。分类修改尚无已核实原生入口，不改走CreateEvent覆盖或直接改库。')


def verify_loaded(data,before):
    for field,wire in [('ClassificationId','Classification'),('SubClassificationId','SubClassification')]:
        if wire not in data:raise ValueError('原生Load/提交回显缺少只读分类保持值。')
        expected=before['classifications'][field];actual=data[wire]
        if expected is None:
            if actual is not None:raise ValueError('原生Load为缺失分类派生默认值，不回填。')
        elif iid(q.identifier(actual))!=expected:raise ValueError('原生只读分类与原草稿持久分类不符，不提交。')


def read_resources(config):
    result=set()
    for state in config['references'].values():
        if state:result.update(resources(state['entity'],state))
    return result


async def read_context(args):
    from tools import quality_event_drafts as d,quality_event_update as u,quality_approval_authorization as auth
    from core.signature_channel import verified_channel
    names=[args.get('classification_name'),args.get('subclassification_name')]
    if any(x is not None for x in names) and any(x is None for x in names):raise ValueError('拟选分类/子分类须同时明确；本入口仅比较，不执行修改。')
    with verified_channel():
        before=await d.source(args);contract=await u.contract();bound=auth.capture_headers('UpdateEvent')
        loaded=await d.load(before,'MCP-QUAL:'+'0'*32,bound)
        config=await configuration(before['organization'],before['classifications'],names if names[0] is not None else None)
        if await d.source(args)!=before:raise ValueError('分类核验期间原草稿、组织或分类配置变化。')
        return dict(complete=True,original_event_id=before['event']['EventId'],before=before,native_contract=contract,
            classification_configuration=config,server_context=loaded['current'],ready_for_execution=False,ready_for_submission=False,
            changes_applied=False,classification_write_supported=False,
            note='只读比较原分类/拟选组合及原组织完整映射。组合有效不代表可改分类、可提交或已保存；既有草稿分类原生写入未核实，禁止直接改库/通用HTTP/同名CreateEvent覆盖。')
