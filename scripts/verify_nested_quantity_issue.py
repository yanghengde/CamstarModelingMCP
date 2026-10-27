"""One-shot full-demand nested Actuals experiment on the new C fixture only."""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import get_client, get_headers, close_client
from core.transfer_attempt_lock import attempt_lock
from tools.component_issue_payload import build_quantity_issue, verify_quantity_echo, PREVIEW_PARAMS, reference
from tools.component_issue import get_component_requirement, get_component_issue_history
from tools.container_inquiry import get_container_details, get_container_history

PREFIX = 'CDX-T05-20261024-C'


def validate_context(fixture, status, requirement):
    if (fixture.get('environment') != CAMSTAR_BASE_URL.rstrip('/')
            or fixture.get('names') != {'component': PREFIX+'-COMP', 'target': PREFIX+'-TARGET',
                                        'order': PREFIX+'-MO', 'container': PREFIX+'-C01'}
            or fixture.get('state') != 'fixtures_created_requirement_matched'):
        raise ValueError('Only the new, acknowledged C fixture is supported.')
    attempts = fixture.get('attempts', [])
    if len(attempts) != 4 or any(a.get('state') != 'created_and_read_verified' for a in attempts):
        raise ValueError('All four fixture objects must be acknowledged and verified.')
    created = [a['verified_object'] for a in attempts if a.get('kind') == 'Container']
    row = status.get('data', {}).get('ContainerStatusDetails', {})
    if (len(created) != 1 or status.get('complete') is not True or row.get('Status') != 1
            or row.get('InProcess') is not True or row.get('Qty') != 1
            or row.get('ProductName') != PREFIX+'-TARGET' or row.get('ProductRev') != '1'
            or row.get('MfgOrderName') != PREFIX+'-MO'):
        raise ValueError('Active fixture container context is incomplete or changed.')
    for key in ('Container', 'Product', 'MfgOrder', 'Spec', 'Step', 'UOM', 'Operation', 'ContainerLevel'):
        if row.get(key) != created[0].get(key):
            raise ValueError('Fixture context changed: '+key)
    body = build_quantity_issue(requirement, 2)
    if (body['container'] != reference(row['Container'])
            or body['serviceDetails'][0]['product']['name'] != PREFIX+'-COMP'
            or body['serviceDetails'][0]['product']['revision'] != '1'
            or body['serviceDetails'][0]['bomLineItem']['parent']['instanceId'] != row['MfgOrder']['InstanceId']):
        raise ValueError('Demand does not belong to this fixture product/order/container.')
    return body


async def verify(fixture_path, output, apply=False):
    journal = fixture_path.with_name(fixture_path.stem+'-execution-probe.json')
    if output.resolve() in (fixture_path.resolve(), journal.resolve()):
        raise ValueError('Output cannot overwrite fixture or execution journal.')
    report = {'execute': apply, 'request_sent': False, 'retry_allowed': False, 'transaction_verified': False,
              'service_accepted': False, 'serialization_verified': False, 'state': 'not_submitted',
              'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'marker': 'Codex T05 Actuals '+uuid.uuid4().hex}
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        output.write_text(text, encoding='utf-8')
        if apply and report['request_sent']:
            journal.write_text(text, encoding='utf-8')
    try:
        with attempt_lock(fixture_path, 'component-execution-probe'):
            if journal.exists():
                raise ValueError('Execution journal exists; do not replay the experiment.')
            fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
            report['status_before'] = json.loads(await get_container_details(PREFIX+'-C01'))
            report['requirement_before'] = json.loads(await get_component_requirement(PREFIX+'-C01', PREFIX+'-COMP'))
            body = validate_context(fixture, report['status_before'], report['requirement_before'])
            body['comments'] = report['marker']
            report['body'] = body
            report['issue_history_before'] = json.loads(await get_component_issue_history(PREFIX+'-C01'))
            if report['issue_history_before'].get('complete') is not True or report['issue_history_before'].get('rows') != []:
                raise ValueError('Expected complete empty actual history.')
            client = get_client(); url = CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')+'/api/ComponentIssue'
            echo = await asyncio.wait_for(client.post(url, headers=get_headers(), json=body,
                                                      params=PREVIEW_PARAMS, follow_redirects=False), 30)
            report['preview_http_status'] = echo.status_code
            report['preview_response'] = echo.json()
            if echo.status_code != 200:
                raise ValueError('Preview HTTP status is not 200.')
            report['serialization_verified'] = verify_quantity_echo(report['preview_response'], body)
            report['state'] = 'prepared'
            if not apply:
                save(); return report
            report['status_recheck'] = json.loads(await get_container_details(PREFIX+'-C01'))
            report['requirement_recheck'] = json.loads(await get_component_requirement(PREFIX+'-C01', PREFIX+'-COMP'))
            again = validate_context(fixture, report['status_recheck'], report['requirement_recheck'])
            again['comments'] = report['marker']
            if again != body:
                raise ValueError('Demand or context changed after preview.')
            with journal.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
            report.update(request_sent=True, state='unknown')
            save()
            response = await asyncio.wait_for(client.post(url, headers=get_headers(), json=body,
                params={'execute': 'true', '$select': 'Container,CompletionMsg'}, follow_redirects=False), 30)
            report.update(http_status=response.status_code, raw_response=response.text)
            try: report['response'] = response.json()
            except ValueError: pass
            data = report.get('response')
            report['service_accepted'] = (response.status_code == 200 and isinstance(data, dict)
                                          and not data.get('error') and not data.get('ErrorCode'))
            save()
            report['requirement_after'] = json.loads(await get_component_requirement(PREFIX+'-C01', PREFIX+'-COMP'))
            report['issue_history_after'] = json.loads(await get_component_issue_history(PREFIX+'-C01'))
            report['container_history_after'] = json.loads(await get_container_history(PREFIX+'-C01', history_source='mainline'))
            report['state'] = 'evidence_collected_requires_review'
            save()
    except BaseException as error:
        report['error'] = type(error).__name__+': '+str(error)
        save()
        if isinstance(error, asyncio.CancelledError): raise
        if not isinstance(error, Exception): raise
    finally:
        await close_client()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(verify(args.fixture, args.output, args.apply))
    print(json.dumps({k: result.get(k) for k in ('state', 'serialization_verified', 'http_status', 'service_accepted', 'error')}))
    sys.exit(1 if result.get('error') or args.apply and not result['service_accepted'] else 0)
