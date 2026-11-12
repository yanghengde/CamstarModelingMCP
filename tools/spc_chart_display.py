"""Bounded display-mode repair for the original C6 synthetic test chart only."""
import json
from pathlib import Path
import re
import sqlite3

from jsonschema import validate

from config import CAMSTAR_BASE_URL
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from tools import mcp
from tools import quality_spc_config as config, quality_spc_history as history
from tools.collection_submission import digest
from tools.modeling_details import raw_json
from tools.spc_modeling_test import http, matches

NAME = 'CDX-C6-20261112-CHART'
ID = '0020048000000002'
POINTS = ['001c6b800000000f','001c6b8000000010','001c6b8000000011','001c6b8000000012']
DB = Path(__file__).resolve().parents[1]/'data/spc_chart_display.sqlite3'
CHANGES = {'isNewSPC': True, 'spcChartDisplayOption': 2, 'runTimeDisplay': True, 'spcChartSaveOptions': 3}


def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(scope TEXT PRIMARY KEY,before_digest TEXT,before_json TEXT,body TEXT,state TEXT,status INTEGER,response TEXT,after_json TEXT)')
    return db


def scope():
    return digest({'environment': CAMSTAR_BASE_URL.rstrip('/'), 'chart_id': ID})


async def snapshot():
    response = await http('GET', '/api/SPCChartDefs/'+ID)
    response.raise_for_status()
    chart = response.json()
    persisted = await config.read_chart(NAME, ID)
    points = await history.read_points(POINTS)
    if (chart.get('InstanceID') != ID or chart.get('Name') != NAME or chart.get('ChartVariable') != 'TestValue'
            or persisted is None or persisted['flags']['IsFrozen'] is not False):
        raise ValueError('原C6测试图身份、参数或冻结状态未核实。')
    if any(persisted['flags'][field] is not False for field in
           ('TriggerOCAPs','RequireAnnotation','NotificationConfigured','HoldConfigured','ResourceStatusConfigured','RecordAgainstCustomSPCDataSrc')):
        raise ValueError('测试图存在处置/通知或其他执行配置，不能修改。')
    if [points[key]['DataValue'] for key in POINTS] != ['9','10','11','14']:
        raise ValueError('原隔离采集值已改变，不能修改图配置。')
    return {'environment': CAMSTAR_BASE_URL.rstrip('/'), 'chart': chart, 'persisted': persisted, 'points': points}


async def display_contract():
    response = await http('GET', '/swagger/SPCChartDefs/swagger.json')
    response.raise_for_status()
    document = response.json()
    operation = document['paths']['/api/SPCChartDefs/{key}']['put']
    schema = operation['requestBody']['content']['application/json']['schema']
    body = {'name': NAME, 'chartVariable': 'TestValue', **CHANGES}
    validate(body, {**schema, 'components': document['components']})
    response = await http('POST', '/api/SPCChartDefs/RequestSelectionValues',
                          params={'selectionValuesExpression':'SPCChartDisplayOption'},
                          json={'objectToChange':{'name':NAME,'instanceId':ID}})
    response.raise_for_status()
    selection = response.json()['ObjectChanges']['SPCChartDisplayOption']
    rows = selection['SelectionValuesEx']
    if (str(selection['RecordCount']) != '3' or str(selection['RowCount']) != '3' or len(rows) != 3
            or {row['Name']:str(row['Value']) for row in rows} !=
               {'DisplayOnlyOnException':'1','NoChartNever':'3','RunTimeDisplay':'2'}):
        raise ValueError('现场显示选项未核实，不猜枚举值。')
    return body


@mcp.tool
async def get_spc_test_chart_display() -> str:
    """只读核对原CDX-C6-20261112-CHART的显示配置、原四笔9/10/11/14历史和现场显示选项。
    仅返回该固定测试图及拟改字段与摘要；不保存或生成图，不把显示开启当引擎就绪。
    """
    try:
        before = await snapshot()
        body = await display_contract()
        return raw_json({'before_digest': digest(before), 'before': before, 'proposed_body': body,
                         'mes_write_performed': False, 'native_chart_verified': False,
                         'remaining_prerequisites': ['实际图类型及参数','原始数据查询','可用连接与引擎','原生生成验证']})
    except Exception as error:
        return raw_json({'error': str(error)[:2000], 'mes_write_performed': False})


@mcp.tool
async def update_spc_test_chart_display(before_digest: str) -> str:
    """用户授权后修正固定原C6测试图为Inline SPC、Run Time Display(2)、运行时显示及不保存图片。
    绑定只读入口返回的原状态摘要；仅一次最小PUT，预先持久记账，失败/未知不重试。
    不修改采集值、图类型/查询/连接/规则或生产关联，不执行生成/采集/注释/违例或处置。
    成功仅证明显示字段保存，原生出图仍需完整引擎和数据配置。
    """
    attempted = False
    try:
        if not re.fullmatch(r'[0-9a-f]{64}', before_digest):
            raise ValueError('须提供只读核对返回的原状态摘要。')
        with attempt_lock(DB, scope()):
            with database() as db:
                if db.execute('SELECT 1 FROM attempts WHERE scope=?', (scope(),)).fetchone():
                    raise ValueError('该固定图已有显示修改尝试，只能只读核实，禁止重发。')
            before = await snapshot()
            if digest(before) != before_digest:
                raise ValueError('原图、源点或环境已变化，尚未提交。')
            body = await display_contract()
            if matches(CHANGES, before['chart']):
                return raw_json({'state':'already_configured','mes_write_performed':False,'native_chart_verified':False})
            # Recheck source and chart after fetching live metadata/selection values.
            if await snapshot() != before:
                raise ValueError('检查期间原图或源点发生变化，尚未提交。')
            with database() as db:
                db.execute('INSERT INTO attempts(scope,before_digest,before_json,body,state) VALUES(?,?,?,?,?)',
                           (scope(), before_digest, raw_json(before), raw_json(body), 'unknown'))
            attempted = True
            response = await http('PUT', '/api/SPCChartDefs/'+ID, json=body)
            with database() as db:
                db.execute('UPDATE attempts SET status=?,response=? WHERE scope=?', (response.status_code,response.text,scope()))
            if not 200 <= response.status_code < 300:
                raise ValueError('原生保存响应不是2xx；保留unknown，禁止重发。')
            if response.content:
                result = response.json()
                if isinstance(result,dict) and (result.get('ErrorCode') or result.get('error')):
                    raise ValueError('原生保存响应含错误；保留unknown，禁止重发。')
            after = await snapshot()
            if (not matches(CHANGES, after['chart']) or after['points'] != before['points']
                    or after['persisted']['references'] != before['persisted']['references']):
                raise ValueError('显示字段、原点保持或原引用未通过保存后核对；保留unknown。')
            audit = {'@odata.context','ChangeCount','LastChangeDate','LastChangeDateGMT','LastChangedBy','LastChangedDate','LastChangedDateGMT'}
            changed = {k.casefold() for k in CHANGES}
            other = [k for k in before['chart'].keys()|after['chart'].keys()
                     if k not in audit and k.casefold() not in changed and before['chart'].get(k)!=after['chart'].get(k)]
            if other:
                raise ValueError('其他字段出现变化，保留证据不认领完整成功：'+','.join(sorted(other)))
            with database() as db:
                db.execute("UPDATE attempts SET state='verified',after_json=? WHERE scope=?", (raw_json(after),scope()))
            return raw_json({'state':'verified','chart_name':NAME,'chart_id':ID,'changes':CHANGES,
                             'source_values_unchanged':True,'references_unchanged':True,
                             'mes_write_performed':True,'native_chart_verified':False,
                             'remaining_prerequisites':['实际图类型及参数','原始数据查询','可用连接与引擎','原生生成验证']})
    except Exception as error:
        return raw_json({'state':'unknown' if attempted else 'not_submitted', 'error':str(error)[:2000],
                         'mes_write_attempted':attempted,'native_chart_verified':False})
