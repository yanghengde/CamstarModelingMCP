"""Recheck C6 prerequisites without calling SPC engines or business transactions."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import close_client, get_client, get_headers
from scripts.acceptance_readonly import ReadOnlyGate
from tools import quality as q, quality_spc_annotations as annotations
from tools import quality_spc_history as history, quality_spc_evidence as evidence


async def audit(output):
    import httpx
    gate = ReadOnlyGate(CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL)
    original = gate.install()
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'C6 prerequisites and evidence contracts; not transaction acceptance',
              'mes_business_write_performed': False, 'external_spc_engine_called': False,
              'contracts': {}, 'native_registration_onsite_accepted': False, 'analysis_onsite_accepted': False}
    try:
        report['readiness'] = json.loads(await history.get_spc_native_readiness())
        schema = {table: fields for table, fields, _ in evidence.TABLES.values()}
        try:
            fields = await annotations.schema_check(schema)
            report['registration_history_columns'] = {'complete': True, 'verified_column_count': len(fields)}
        except (ValueError, KeyError, TypeError) as exc:
            report['registration_history_columns'] = {'complete': False, 'error_type': type(exc).__name__}
        for name in ('AddSPCAnnotation', 'RecordSPCViolation', 'InlineSPCTxn'):
            response = await get_client().get(CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')+'/swagger/'+name+'/swagger.json',
                                               headers=get_headers(), follow_redirects=False)
            entry = {'http_status': response.status_code, 'schema_verified': False}
            try:
                document = response.json()
                schema = document['components']['schemas'][name]
                entry.update(schema_verified=response.status_code == 200 and bool(schema.get('properties')),
                             properties=sorted(schema.get('properties', {})),
                             service_endpoint_present='post' in document.get('paths', {}).get('/api/'+name, {}))
            except (ValueError, KeyError, TypeError):
                pass
            report['contracts'][name] = entry
        rows = await q.sql_rows('SELECT TOP 101 '+history.POINT_FIELDS+history.HISTORY_JOIN
            +' WHERE d.DataType IN (1,2,3,9) AND m.ReversalStatus=1 ORDER BY m.TxnDateGMT,d.DataPointHistoryDetailId', 101)
        groups, unavailable = {}, []
        for raw in rows[:100]:
            try:
                row = history.point_record(raw)
            except (ValueError, KeyError, TypeError):
                unavailable.append(raw.get('DataPointHistoryDetailId'))
                continue
            scope = tuple(row[field] for field in history.SCOPE_FIELDS)
            groups.setdefault(scope, []).append(row)
        report['numeric_history_probe'] = {
            'bounded_at': 100, 'has_more': len(rows) > 100, 'rows_read': min(len(rows), 100),
            'unsupported_source_ids': unavailable,
            'scopes': [{'characteristic': scope[2], 'unit_id': points[0]['UOMId'],
                        'source_point_ids': [row['DataPointHistoryDetailId'] for row in points],
                        'available_count': len(points)} for scope, points in groups.items()],
            'note': '候选不是已批准基线。独立基线至少2点且非零变差，另需评估点；合理分组、连续区间及统计假设须另核实。'}
        report['remaining_prerequisites'] = [
            '明确现场SPC引擎部署、版本和连接；当前程序不新建虚构地址或更改生产连接。',
            '明确测试图、图类型、查询、编译规则及生效控制限的来源和版本。',
            '隔离原始数值序列、单位、合理子组和独立基线；现有零散历史不拼接为基线。',
            '对原生计算、登记、注释及副作用分别准备和验收；本地判异不替代引擎结论。']
    finally:
        report['requests'] = gate.requests
        await close_client()
        httpx.AsyncHTTPTransport.handle_async_request = original
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'report': str(output.resolve()), 'checks': report.get('readiness', {}).get('checks'),
                      'history_columns': report.get('registration_history_columns'),
                      'contracts': {name: item['schema_verified'] for name, item in report['contracts'].items()},
                      'business_write_performed': False}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(audit(parser.parse_args().output))
