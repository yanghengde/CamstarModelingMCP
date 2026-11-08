"""GET contracts and optional production inquiries only; never submit Shopfloor.

Example: python scripts/validate_production_reporting.py --mfgorder MO-100076435
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import close_client, get_client, get_headers
from tools.production_reporting import get_container_throughput_history, get_mfgorder_production_summary
from tools.production_records import get_production_worktime, get_production_reports, get_mfgorder_completion_readiness


async def validate(args):
    out = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
           'shopfloor_transactions_executed': False, 'shopfloor_previews_executed': False,
           'contracts': {}, 'reads': {}, 'errors': []}
    try:
        for name, fields in {'Thruput': {'container', 'qty', 'thruputAllQty'},
                              'MoveWithThruput': {'container', 'moveAllQty', 'thruputAllQty', 'close'}}.items():
            path = f'/swagger/{name}/swagger.json'
            response = await get_client().get(CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/') + path, headers=get_headers())
            response.raise_for_status()
            definition = response.json()
            schema = definition['components']['schemas'][name]
            if not fields <= schema.get('properties', {}).keys():
                raise ValueError(name + '请求契约字段缺失。')
            out['contracts'][name] = {'source_path': path, 'http_status': response.status_code,
                'sha256': hashlib.sha256(response.content).hexdigest(),
                'post_path_verified': '/api/' + name in definition['paths'],
                'required': schema.get('required', []),
                'used_fields': {key: schema['properties'][key] for key in sorted(fields)}}
        if args.mfgorder:
            out['reads']['order'] = json.loads(await get_mfgorder_production_summary(args.mfgorder))
            out['reads']['worktime'] = json.loads(await get_production_worktime(args.mfgorder))
            out['reads']['report_ledger'] = json.loads(await get_production_reports(args.mfgorder))
            out['reads']['completion_readiness'] = json.loads(await get_mfgorder_completion_readiness(args.mfgorder))
        if args.container:
            out['reads']['throughput_history'] = json.loads(await get_container_throughput_history(
                args.container, args.container_instance_id, limit=5))
        for name, result in out['reads'].items():
            if result.get('complete') is not True:
                out['errors'].append(name + ': ' + str(result.get('error', '读取不完整')))
    except Exception as exc:
        out['errors'].append(str(exc))
    finally:
        await close_client()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'contracts': len(out['contracts']), 'reads': list(out['reads']),
                      'errors': out['errors'], 'output': str(output),
                      'shopfloor_transactions_executed': False}, ensure_ascii=True))
    return bool(out['errors'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mfgorder')
    parser.add_argument('--container')
    parser.add_argument('--container-instance-id')
    parser.add_argument('--output', default='tmp/production-reporting-validation.json')
    raise SystemExit(asyncio.run(validate(parser.parse_args())))
