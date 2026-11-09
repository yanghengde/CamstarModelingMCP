"""Narrow evidence for a native missing-numbering validation rejection.

Not a generic HTTP-error recovery path: timeouts, malformed replies, other
errors, any Event/history, or changed source/configuration stay unknown.
"""
import json
import re
import time
import uuid
from tools import quality_plans as p,quality_inspection_event as e
from core.transfer_attempt_lock import attempt_lock


async def proof(snap,outcome):
    e.validate_transport(snap)
    if (outcome.get('http_status')!=400 or outcome.get('response_truncated')is not False
        or not outcome.get('submission_attempted')or outcome.get('error')or outcome.get('interrupted')):
        raise ValueError('不是完整的原生明确配置拒绝响应，保留unknown。')
    reply=json.loads(outcome['raw_response']);details=reply.get('Details')
    code=reply.get('ErrorCode')
    if code=='13501022':message="A numbering rule was not found for Organization '"+snap['organization']['Name']+"' to use in auto generation of names for category 'Nonconformance'."
    elif code=='2424841':
        prefix=snap['numbering']['rule']['Prefix']
        if not isinstance(prefix,str)or not prefix.endswith('-')or not all(c.isalnum()or c=='-'for c in prefix):raise ValueError('不属于已核实的编号字面值语法拒绝。')
        message="Syntax error: <expression> is expected after '+' or '-' in '"+prefix+"'"
    elif code=='13369354':
        if not isinstance(details,list)or len(details)!=1 or not re.fullmatch(r'The user "[^"\r\n]{1,100}" is not authorized to perform the service "Create Production Event"',details[0].get('Message','')):
            raise ValueError('未匹配此创建服务的明确权限拒绝。')
        message=details[0]['Message']
    else:raise ValueError('原生拒绝码未开放，保留unknown。')
    if (not isinstance(details,list)or len(details)!=1
        or details[0].get('ErrorCode')!=code or details[0].get('Message')!=message):
        raise ValueError('未匹配本次目标组织的原生缺失编号拒绝，不根据HTTP状态推断未提交。')
    org=snap['organization'];key=org['InstanceID']
    current=await e.source(snap['arguments'])
    config=await e.triage.asset('organization',name=org['Name'],signature_txn_type=e.TYPE)
    if current!=snap['source']or config!=org:raise ValueError('原检验或拒绝时组织配置已变化，证据不足。')
    maps=await e.q.sql_rows('SELECT NumberingRuleMapId,OrganizationId,Category,NumberingRuleId,IsFrozen,ChangeCount FROM NumberingRuleMap WHERE OrganizationId='+e.q.literal(key)+' ORDER BY NumberingRuleMapId',101)
    if len(maps)>100:raise ValueError('编号映射超出核对边界。')
    if code=='13501022'and any(e.ih.integer(r['Category'])==3 for r in maps):raise ValueError('拒绝时的缺失类别条件已不成立，不能结案。')
    if code in ('2424841','13369354'):
        if [r for r in maps if e.ih.integer(r['Category'])==3]!=[snap['numbering']['persistent_mapping']]:raise ValueError('拒绝时的编号映射已变化。')
        rule=await e.lifecycle.numbering(org,category=3)
        if {k:v for k,v in rule.items()if k!='mapping_id'}!={k:v for k,v in snap['numbering'].items()if k!='persistent_mapping'}:raise ValueError('拒绝时的编号规则已变化。')
    events=await e.q.sql_rows('SELECT TOP 2 EventId,EventName FROM Event WHERE OrganizationId='+e.q.literal(key)+' ORDER BY EventId',2)
    native=await e.q.sql_rows('SELECT TOP 2 CreateEventHistoryId,HistoryMainlineId,TxnId FROM CreateEventHistory WHERE OrganizationId='+e.q.literal(key)+' ORDER BY CreateEventHistoryId',2)
    roots=await e.roots(snap,limit=1);duplicates=await e.duplicates(snap['source']['key'])
    if events or native or roots!=([],False)or duplicates:raise ValueError('出现事件或创建历史，不能作为明确未创建结案。')
    return dict(exact_native_validation_rejection=True,organization=org,numbering_maps=maps,original_source=current,
        no_event_for_organization=True,no_native_creation_history=True,no_source_event=True,
        native_error_code=code,basis='Exact original native pre-name numbering validation/expression rejection, unchanged original inspection/configuration and bounded absence checks; not a generic absence-of-history conclusion.')


async def resolve(plan_id,fingerprint,note,reference):
    with attempt_lock(p.PLAN_DB,plan_id):
        with p.db_connect()as db:
            row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            attempt=db.execute('SELECT attempt_id,started,finished FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()
        snap=p.checked(row,fingerprint)
        if not e.supported(snap)or row[2]!='unknown'or not attempt or attempt[2]is None:raise ValueError('仅核对已结束的原关联事件尝试。')
        outcome=json.loads(row[4]);evidence=await proof(snap,outcome)
        if reference!='native:'+evidence['native_error_code']:raise ValueError('拒绝证据引用与原生错误码不一致。')
        if await proof(snap,outcome)!=evidence:raise ValueError('原生拒绝取证期间发生变化。')
        with p.db_connect()as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()!=row or db.execute('SELECT attempt_id,started,finished FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()!=attempt:raise ValueError('原计划或尝试变化。')
            audit=dict(action='resolve_rejection',conclusion='not_committed',plan_digest=fingerprint,previous_outcome=outcome,attempt=attempt,
                evidence=evidence,evidence_digest=p.digest(evidence),review_note=note,created=time.time())
            result=dict(state='native_authorization_rejected'if evidence['native_error_code']=='13369354'else'native_configuration_rejected',
                verified_success=False,known_native_rejection=True,retry_allowed=False,original_plan_reusable=False,rejection_attestation=audit)
            db.execute('INSERT INTO reviews VALUES(?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(audit,ensure_ascii=False)))
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',(result['state'],json.dumps(result,ensure_ascii=False),plan_id))
        return dict(state_changed=True,mes_write_performed=False,**result)
