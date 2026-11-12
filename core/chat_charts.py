"""Bounded chart snapshots from approved read tools, never model-generated options."""
import json
import asyncio
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from config import CHAT_CHART_DB, ENABLE_CHAT_CHARTS
from tools.collection_submission import digest

MAX_POINTS = 500
MAX_BYTES = 512 * 1024
SUPPORTED = {'analyze_spc_series', 'analyze_spc_history', 'analyze_spc_attributes', 'get_mfgorder_progress', 'get_spc_parameter_trend'}


def requested(question):
    if not isinstance(question, str) or re.search(r'(?:不要|无需|不用|不需要).{0,8}(?:图|可视化)', question):
        return False
    return bool(re.search(r'出图|画.{0,15}图|绘图|图形|控制图|趋势|走势|柱状|柱形|折线|图表|可视化|spc|chart|plot', question, re.I))


def text(value, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError('图表文字字段无效。')
    return value


def number(value):
    if type(value) not in (str, int, float) or len(str(value)) > 128:
        raise ValueError('图表数值缺失或超界。')
    actual = float(value)
    if not math.isfinite(actual) or abs(actual) > 1e30:
        raise ValueError('图表不接受非有限值或超界数值。')
    return str(value)


def panel(title, rows, kind='line'):
    if not 1 <= len(rows) <= MAX_POINTS or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('图表点为空、重复或超界，不截断原序列。')
    return {'title': title, 'kind': kind, 'rows': rows}


def build(tool, raw):
    if tool not in SUPPORTED or not isinstance(raw, str) or len(raw.encode('utf8')) > 2 * 1024 * 1024:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('error'):
        return None
    if tool == 'get_mfgorder_progress':
        entries = data.get('page_step_distribution')
        containers = data.get('containers')
        if (data.get('distribution_scope') != 'current_page_only' or not isinstance(entries, list)
                or not isinstance(containers, list) or not entries or any(row.get('error') for row in containers)
                or len(containers) != data.get('returned_container_count')):
            return None
        rows = []
        for index, row in enumerate(entries):
            count = row['container_count']
            if type(count) is not int or count <= 0:
                raise ValueError('工序计数无效。')
            label = text(row['workflow'])+'/'+text(row['revision'])+'/'+text(row['step'])
            rows.append({'id': str(index), 'label': label, 'value': str(count), 'signal': False, 'detail': '单位：个容器'})
        if sum(int(row['value']) for row in rows) != len(containers):
            raise ValueError('工序分布与本页容器数量不一致。')
        complete = data.get('container_list_complete') is True and data.get('page', {}).get('has_more') is False
        scope = ('当前关联容器完整列表' if complete else '仅当前页，不代表全工单分布')+'；容器数不等于产量'
        payload = {'version': 1, 'title': text(data['order']['Name'])+' · 工序容器分布',
                   'subtitle': scope, 'unit': '个容器', 'reference': '当前关联容器查询',
                   'source_digest': digest(data), 'complete': complete, 'tool': tool,
                   'note': '保存当次查询快照；打开历史图表不会重新查询MES。',
                   'panels': [panel('工序分布', rows, 'bar')]}
    elif tool == 'get_spc_parameter_trend':
        if (data.get('version') != 'spc-parameter-trend-v1' or data.get('complete') is not True
                or data.get('data_verified_against_mes') is not True or data.get('source_window_verified') is not True
                or data.get('source_kind') != 'mes_persisted_direct_collection'
                or data.get('analysis_performed') is not False or data.get('native_spc_result') is not False
                or data.get('ready_for_mes_write') is not False or data.get('release_authorized') is not False
                or 'control_limits' not in data or data['control_limits'] is not None):
            return None
        evidence = data['source_history']
        if digest(evidence) != data.get('source_history_digest'):
            raise ValueError('趋势来源摘要不一致。')
        ctx, points = data['context'], data['points']
        if (not 1 <= len(points) <= 100 or len(points) != len(evidence['points'])
                or ctx['characteristic'] != evidence['scope']['DataName']
                or ctx['unit'] != evidence['unit']['name'] or evidence['unit']['id'] != evidence['scope']['UOMId']
                or evidence['scope_kind'] not in ('container', 'process')
                or evidence['scope_kind'] == 'container' and any(
                    p['ContainerId'] != evidence['scope']['ContainerId'] for p in evidence['points'])):
            raise ValueError('趋势范围或单位与来源不一致。')
        excluded = {r['point_id'] for r in evidence['active_exclusion_records']}
        rows = []
        for i, (point, source) in enumerate(zip(points, evidence['points']), 1):
            pid = text(point['observation_id'], 128)
            if (pid != source['DataPointHistoryDetailId'] or point['value'] != source['DataValue']
                    or point['timestamp_gmt'] != source['TxnDateGMT'] or type(point['sequence']) is not int
                    or point['sequence'] != i or type(point['has_active_exclusion_record']) is not bool
                    or point['has_active_exclusion_record'] != (pid in excluded)):
                raise ValueError('趋势点与原始历史不一致。')
            rows.append({'id': pid, 'label': text(point['timestamp_gmt']), 'value': number(point['value']),
                         'signal': False, 'excluded': point['has_active_exclusion_record'],
                         'detail': '原始GMT：'+point['timestamp_gmt']+'；'+
                                   ('存在未冲销的排除记录，原值仍显示。' if pid in excluded else '未发现未冲销的排除记录。')})
        if [row['id'] for row in rows] != evidence['history_ids'] or not excluded.issubset(evidence['history_ids']):
            raise ValueError('趋势观测ID与来源清单不一致。')
        payload = {'version': 1, 'title': text(ctx['characteristic'])+' · 原始参数走势',
                   'subtitle': 'MES原始历史 · '+('同一容器' if evidence['scope_kind']=='container' else '同一过程跨容器范围')+' · 仅展示，不判异', 'unit': text(ctx['unit']),
                   'reference': 'MES原始历史点及GMT顺序', 'source_digest': data['source_history_digest'],
                   'complete': True, 'tool': tool, 'note': text(data['note'], 1600),
                   'panels': [panel('原始值（GMT先后，观测等距）', rows, 'trend')]}
    else:
        if (data.get('native_spc_result') is not False or data.get('ready_for_mes_write') is not False
                or not isinstance(data.get('input_snapshot'), dict)
                or digest(data['input_snapshot']) != data.get('input_digest')
                or tool == 'analyze_spc_history' and (data.get('complete') is not True
                    or data.get('data_verified_against_mes') is not True or data.get('source_window_verified') is not True)):
            return None
        chart = data['chart_type']
        ctx = data['context']
        unit, characteristic = text(ctx['unit']), text(ctx['characteristic'])
        points = data['points']
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
            raise ValueError('SPC图表不截断分析序列。')
        signals = {key for signal in data.get('primary_signals', []) for key in signal['contributing_ids']}
        rows, panels = [], []
        for point in points:
            pid = text(point['observation_id'], 128)
            if chart in ('p', 'np', 'c', 'u'):
                if data.get('algorithm_version') != 'spc-attributes-v1':
                    raise ValueError('未知计数图算法版本。')
                limits = point
                center = point['center']
                signal = point['beyond_3sigma']
                detail = f"计数：{point['count']}；检查单位：{point['inspected_units']}"
            elif chart in ('i_mr', 'xbar_r'):
                if data.get('algorithm_version') != 'spc-local-v1':
                    raise ValueError('未知SPC算法版本。')
                limits = data['control_limits']['primary']
                center, signal = limits['center'], pid in signals
                detail = '原值：'+', '.join(text(v, 128) for v in point['values'])
            else:
                raise ValueError('未开放的SPC图型。')
            if type(signal) is not bool:
                raise ValueError('SPC信号标记必须明确。')
            rows.append({'id': pid, 'label': str(point['sequence']), 'value': number(point['statistic']),
                         'center': number(center), 'lcl': number(limits['lcl']), 'ucl': number(limits['ucl']),
                         'signal': signal, 'detail': text(detail, 1600)})
        panels.append(panel(chart.upper()+' · 评估段', rows))
        if chart in ('i_mr', 'xbar_r') and data['dispersion_points']:
            positions = {p['observation_id']: p['sequence'] for p in points}
            dispersion_signals = {s['window_end_id'] for s in data['dispersion_signals']}
            limits = data['control_limits']['dispersion']
            dispersion = [{'id': text(p['end_id'], 128), 'label': str(positions[p['end_id']]), 'value': number(p['value']),
                'center': number(limits['center']), 'lcl': number(limits['lcl']), 'ucl': number(limits['ucl']),
                'signal': p['end_id'] in dispersion_signals, 'detail': '沿用后端离散统计量，不跨基线/评估段计算。'}
                for p in data['dispersion_points']]
            panels.append(panel('移动极差 MR' if chart == 'i_mr' else '极差 R', dispersion))
        source = 'MES原始历史' if tool == 'analyze_spc_history' else '调用方明确数据'
        payload = {'version': 1, 'title': characteristic+' · '+chart.upper(),
            'subtitle': source+' · 本地SPC分析 · 图示评估段',
            'unit': {'p':'比例（0–1）','np':'件','c':'个缺陷','u':'缺陷/检查单位'}.get(chart, unit),
            'reference': text(str(data['control_reference']), 512)+' / '+text(str(data['control_version']), 128),
            'source_digest': data['input_digest'], 'complete': True, 'tool': tool, 'panels': panels,
            'note': '控制限来自独立基线或明确固定配置；本地信号不代表原生引擎判定、产品合格或放行。'
                + (' 检查单位：'+unit+'。' if chart in ('p','np','c','u') else '')
                + (' 存在小期望计数，三西格玛近似需审核。' if data.get('small_expected_counts') else '')}
    if len(json.dumps(payload, ensure_ascii=False).encode('utf8')) > MAX_BYTES:
        raise ValueError('图表快照超过上限，保留原文字结果。')
    return payload


class ChartStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS charts(id TEXT PRIMARY KEY,username TEXT,session_id TEXT,tool_call_id TEXT,created REAL,payload TEXT,digest TEXT,title TEXT,UNIQUE(username,session_id,tool_call_id))')
        if 'title' not in {row[1] for row in db.execute('PRAGMA table_info(charts)')}:
            db.execute('ALTER TABLE charts ADD COLUMN title TEXT')
            db.execute("UPDATE charts SET title=json_extract(payload,'$.title')")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def save(self, username, session_id, tool_call_id, payload):
        for value in (username, session_id, tool_call_id): text(value, 128)
        if tool_call_id == 'unknown':
            raise ValueError('图表必须绑定实际工具调用。')
        raw, checksum = json.dumps(payload, ensure_ascii=False), digest(payload)
        if len(raw.encode('utf8')) > MAX_BYTES: raise ValueError('图表快照超界。')
        with self.db() as db:
            existing = db.execute('SELECT id,digest,created FROM charts WHERE username=? AND session_id=? AND tool_call_id=?',
                                  (username, session_id, tool_call_id)).fetchone()
            if existing:
                if existing[1] != checksum: raise ValueError('原工具调用已绑定其他图表快照。')
                key, _, created = existing
            else:
                key, created = uuid.uuid4().hex, time.time()
                db.execute('INSERT INTO charts VALUES (?,?,?,?,?,?,?,?)', (key, username, session_id, tool_call_id, created, raw, checksum, payload['title']))
        return {'id': key, 'tool_call_id': tool_call_id, 'title': payload['title'], 'created': created}

    def get(self, username, session_id, key):
        if not re.fullmatch('[0-9a-f]{32}', key): return None
        if not self.path.exists(): return None
        with self.db() as db:
            row = db.execute('SELECT payload,digest FROM charts WHERE id=? AND username=? AND session_id=?', (key, username, session_id)).fetchone()
        if not row: return None
        payload = json.loads(row[0])
        if digest(payload) != row[1]: raise ValueError('图表快照损坏。')
        return payload

    def list(self, username, session_id, messages):
        if not self.path.exists(): return []
        calls = {m.get('tool_call_id') for m in messages if m.get('role') == 'tool'}
        with self.db() as db:
            rows = db.execute('SELECT id,tool_call_id,created,title FROM charts WHERE username=? AND session_id=? ORDER BY created', (username, session_id)).fetchall()
        return [{'id': key, 'tool_call_id': call, 'created': created, 'title': title}
                for key, call, created, title in rows if call in calls]


STORE = ChartStore(CHAT_CHART_DB)


def publish(username, session_id, call_id, question, tool, raw):
    if not ENABLE_CHAT_CHARTS or tool not in SUPPORTED or not requested(question): return None
    try:
        payload = build(tool, raw)
        return STORE.save(username, session_id, call_id, payload) if payload else None
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error):
        # Chart failures must never change an already completed tool result.
        return None


async def publish_async(*args):
    """Optional visualization cannot hold up the next answer on slow storage."""
    if not ENABLE_CHAT_CHARTS or args[4] not in SUPPORTED or not requested(args[3]): return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(publish, *args), .2)
    except Exception:
        # Optional presentation failures must not fail a completed business tool.
        # CancelledError is a BaseException and still propagates normally.
        return None
