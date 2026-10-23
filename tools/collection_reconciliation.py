"""Read-only comparison of an immutable collection plan with a candidate transaction."""
import json
from decimal import Decimal, InvalidOperation

from tools import mcp
from tools.boms import encode
from tools import collection_submission as submission
from tools.collection_history import get_container_collection_history


def same_value(expected, actual, kind):
    if actual is None or isinstance(actual, bool):
        return False
    if kind == 4:
        return isinstance(actual, str) and actual == expected
    if kind not in (1, 2, 3, 9):
        return False
    try:
        left, right = Decimal(str(expected)), Decimal(str(actual))
        return left.is_finite() and right.is_finite() and left == right
    except InvalidOperation:
        return False


@mcp.tool
async def reconcile_collection_submission(plan_id: str, txn_id: str) -> str:
    """只读核对采集计划与用户明确提供的候选16位事务ID。最多读取300条历史，比较容器/定义/点/类型/值；不推断请求关联、不解除unknown锁、不执行或重试。"""
    out = {'plan_id': plan_id, 'candidate_txn_id': txn_id, 'complete': False,
           'verified_success': False, 'request_correlation_verified': False,
           'retry_allowed': False, 'state_changed': False, 'comparison': 'unavailable',
           'note': '候选事务由调用者提供；值匹配不证明它来自本计划。保持原计划状态，不解除未知结果锁。'}
    try:
        if not submission.valid_id(txn_id):
            raise ValueError('候选事务ID必须为16位十六进制字符串。')
        with submission.db_connect() as db:
            row = db.execute('SELECT digest,payload,state FROM plans WHERE id=?', (plan_id,)).fetchone()
        if not row:
            raise ValueError('计划不存在。')
        stored = json.loads(row[1])
        snap = stored['snapshot']
        out['plan_state'] = row[2]
        if row[2] not in ('unknown', 'in_flight'):
            raise ValueError('仅核对已尝试提交或结果未知的计划；未执行计划不能认领历史。')
        if submission.digest(snap) != row[0]:
            raise ValueError('计划内容与摘要不一致。')
        if snap['environment'] != submission.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
            raise ValueError('当前服务环境与原计划不同。')
        body = snap['body']
        container = body['container']
        expected = {d['dataPoint']['instanceId']: d for d in body['parametricData']['dataPointDetails']}
        types = {c['point_instance_id']: c['data_type'] for c in snap['validation']['checks']}
        rows, seen = [], set()
        offset = 0
        for _ in range(3):
            page = json.loads(await get_container_collection_history(
                container['name'], container['instanceId'], txn_id=txn_id, offset=offset, limit=100))
            items = page.get('rows')
            if page.get('error') or page.get('page_complete') is not True or not isinstance(items, list):
                raise ValueError('历史查询失败或页面不完整，不能核对。')
            for item in items:
                if (not isinstance(item, dict) or not item.get('DataPointHistoryDetailId')
                        or item['DataPointHistoryDetailId'] in seen
                        or item.get('ContainerId') != container['instanceId'] or item.get('TxnId') != txn_id):
                    raise ValueError('跨页重复明细或容器/事务不匹配。')
                seen.add(item['DataPointHistoryDetailId'])
                rows.append(item)
            if page.get('has_more') is False:
                break
            if page.get('has_more') is not True or page.get('next_offset') != offset + 100 or len(items) != 100:
                raise ValueError('历史分页游标不完整。')
            offset += 100
        else:
            out['comparison'] = 'incomplete'
            raise ValueError('历史达到300条核对上限，不能认定完整匹配。')
        out['history_row_count'] = len(rows)
        if not rows:
            out['comparison'] = 'no_records'
            out['note'] += ' 空记录不证明提交失败。'
            return encode(out)
        differences = []
        matched = set()
        mainlines, groups = set(), set()
        for item in rows:
            pid = item.get('DataPointId')
            wanted = expected.get(pid)
            reasons = []
            if wanted is None:
                reasons.append('unexpected_point')
            elif pid in matched:
                reasons.append('duplicate_point')
            else:
                matched.add(pid)
                if item.get('DataCollectionDefId') != body['dataCollectionDef']['instanceId']:
                    reasons.append('definition_mismatch')
                if item.get('DataName') != wanted['dataPoint']['name']:
                    reasons.append('point_name_mismatch')
                if str(item.get('DataType')) != str(types[pid]):
                    reasons.append('type_mismatch')
                if not same_value(wanted['dataValue'], item.get('DataValue'), types[pid]):
                    reasons.append('value_mismatch')
            mainlines.add(item.get('HistoryMainlineId'))
            groups.add(item.get('DataPointHistoryId'))
            if reasons:
                differences.append({'history_detail_id': item['DataPointHistoryDetailId'], 'point_id': pid,
                                    'reasons': reasons, 'actual_value': item.get('DataValue')})
        for pid in expected.keys() - matched:
            differences.append({'point_id': pid, 'reasons': ['missing_point']})
        if len(mainlines) != 1 or None in mainlines or '' in mainlines or len(groups) != 1 or None in groups or '' in groups:
            differences.append({'reasons': ['ambiguous_history_group']})
        out.update(comparison='mismatch' if differences else 'values_match', differences=differences,
                   history_detail_ids=sorted(seen), candidate_history_mainline_ids=sorted(x for x in mainlines if x),
                   comparison_complete=True)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)
