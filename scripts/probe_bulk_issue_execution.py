"""Single-use, fixed F-fixture mode-2 contract probe. Defaults to planning.

Not a general write tool. --apply reserves a permanent journal before one POST;
timeouts, business rejection, cancellation and crashes never permit replay.
"""
import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import get_client, get_headers, close_client
from core.transfer_attempt_lock import attempt_lock
from tools.component_issue_bulk import preview_bulk_component_issue
from tools.component_issue import get_component_requirement, get_component_issue_history
from tools.container_inquiry import get_container_details, get_container_history
from tools import component_issue_plans

PREFIX = 'CDX-T05-20261025-F'
PROBE_JOURNAL = ROOT/'docs/t05-fixtures/CDX-T05-20261025-F-bulk-execution-probe.json'


def validate(fixture, preview):
    expected = {key: PREFIX+suffix for key, suffix in (
        ('component', '-COMP'), ('target', '-TARGET'), ('source_order', '-SRC-MO'),
        ('target_order', '-DST-MO'), ('source', '-SRC'), ('destination', '-DST'))}
    if (fixture.get('names') != expected or fixture.get('environment') != CAMSTAR_BASE_URL.rstrip('/')
            or fixture.get('state') != 'fixtures_created_mode2_requirement_matched'
            or len(fixture.get('attempts', [])) != 6
            or any(a.get('state') != 'created_and_read_verified' for a in fixture['attempts'])):
        raise ValueError('Only the acknowledged F fixture in this deployment is allowed.')
    if not preview.get('source_checks_passed') or not preview.get('serialization_verified') or preview.get('error'):
        raise ValueError('Complete source check and derived serialization proof are required.')
    body = preview['request_preview']; line = body['serviceDetails'][0]; actual = line['actuals'][0]
    acknowledged = {a['name']: a['verified_object'] for a in fixture['attempts'] if a['kind'] == 'Container'}
    source = acknowledged[expected['source']]['Container']; target = acknowledged[expected['destination']]['Container']
    if (body['container'] != {'name': target['Name'], 'instanceId': target['InstanceId']}
            or body['container']['instanceId'] != '00041080000000e8'
            or actual['fromContainer'] != {'name': source['Name'], 'instanceId': source['InstanceId']}
            or actual['fromContainer']['instanceId'] != '00041080000000e7'
            or line.get('@odata.type') != '#shopfloor.IssueDetailsBulk'
            or actual.get('@odata.type') != '#shopfloor.ActualsBulk'
            or line.get('issueControl') != 2 or line.get('actualQtyIssued') != 2
            or actual.get('qtyIssued') != 2 or 'issueActualDetails' in body
            or preview['quantity_context']['available_observed'] not in ('3', '3.0')
            or preview['requirement']['data']['IssueDetails']['QtyIssued'] != 0):
        raise ValueError('Probe must be the untouched F source 3 / demand 2, single derived actual.')
    # Reconstruct the entire allowlisted body, rather than accepting extra fields.
    product, bill, unit = line['product'], line['bomLineItem'], line['uom']
    expected_body = {'container': body['container'], 'preventOverIssue': True, 'serviceDetails': [{
        '@odata.type': '#shopfloor.IssueDetailsBulk', 'bomLineItem': bill, 'product': product, 'uom': unit,
        'issueControl': 2, 'qtyRequired': 2, 'actualQtyIssued': 2, 'actualQty2Issued': 0,
        'actuals': [{'@odata.type': '#shopfloor.ActualsBulk', 'fromContainer': actual['fromContainer'],
                    'product': product, 'toContainer': body['container'], 'qtyIssued': 2, 'qty2Issued': 0}]}]}
    if (body != expected_body or product['name'] != expected['component'] or product['revision'] != '1'
            or product['instanceId'] != '00062c8000000087'
            or bill['instanceId'] != '001b7b8000000006' or bill['parent']['instanceId'] != '0006548000000089'
            or unit != {'name': 'EACH', 'instanceId': '0008de8000000002'}):
        raise ValueError('Unexpected source execution request; overrides or different identities are forbidden.')
    return body


def reject_local_occupancy(body):
    identities = {body['container']['instanceId'], body['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId']}
    with component_issue_plans.db_connect() as db:
        rows = db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')").fetchall()
    for (raw,) in rows:
        snap = json.loads(raw)['snapshot']
        if (snap['environment'] == CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
                and component_issue_plans.participants(snap) & identities):
            raise ValueError('A source/target has an unresolved local issue plan; do not probe.')


async def probe(fixture_path, output, apply=False):
    fixture_path, output = Path(fixture_path), Path(output)
    # One fixed source/target pair has ONE journal even if the input is copied.
    journal = PROBE_JOURNAL
    if output.resolve() in (fixture_path.resolve(), journal.resolve()):
        raise ValueError('Output cannot overwrite the fixture or permanent journal.')
    with attempt_lock(journal, 'bulk-issue-contract'):
        if journal.exists():
            raise ValueError('Execution journal exists; never replay a submitted or uncertain probe.')
        fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
        args = {'container_name': PREFIX+'-DST', 'source_container_name': PREFIX+'-SRC',
                'product_name': PREFIX+'-COMP', 'product_revision': '1', 'qty': 2,
                'container_instance_id': '00041080000000e8', 'source_container_instance_id': '00041080000000e7',
                'verify_serialization': True}
        preview = json.loads(await preview_bulk_component_issue(**args))
        body = validate(fixture, preview)
        reject_local_occupancy(body)
        history = json.loads(await get_component_issue_history(PREFIX+'-DST', '00041080000000e8'))
        if history.get('complete') is not True or history.get('rows') != []:
            raise ValueError('Only an empty initial target actual history may be probed.')
        marker = 'Codex bulk '+uuid.uuid4().hex
        body = {**body, 'comments': marker}
        report = {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'fixture': str(fixture_path.resolve()),
                  'created_at_utc': datetime.now(timezone.utc).isoformat(), 'marker': marker,
                  'state': 'prepared', 'request_sent': False, 'retry_allowed': False, 'verified_success': False,
                  'preview': preview, 'body': body, 'history_before': history,
                  'body_digest': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}
        def save():
            text = json.dumps(report, ensure_ascii=False, indent=2)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding='utf-8')
            if apply: journal.write_text(text, encoding='utf-8')
        if not apply:
            save()
            return report
        with journal.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        report.update(state='unknown', request_sent=True)
        save()
        try:
            response = await asyncio.wait_for(get_client().post(
                CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')+'/api/ComponentIssue', headers=get_headers(), json=body,
                params={'execute': 'true', '$select': 'Container,CompletionMsg'}, follow_redirects=False), 30)
            report.update(http_status=response.status_code, raw_response=response.text)
            try: report['response'] = response.json()
            except ValueError: pass
            save()
            report['source_after'] = json.loads(await get_container_details(PREFIX+'-SRC', '00041080000000e7'))
            report['requirement_after'] = json.loads(await get_component_requirement(PREFIX+'-DST', PREFIX+'-SRC', '00041080000000e8'))
            report['actual_history_after'] = json.loads(await get_component_issue_history(PREFIX+'-DST', '00041080000000e8'))
            report['mainline_after'] = json.loads(await get_container_history(PREFIX+'-DST', '00041080000000e8', history_source='mainline'))
            # Never infer a committed transaction from the response or balances.
            report['state'] = 'unknown_evidence_collected'
        except BaseException as error:
            report['error'] = repr(error)
            raise
        finally:
            save()
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=ROOT/'docs/t05-fixtures/CDX-T05-20261025-F.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    async def main():
        try:
            result = await probe(options.fixture, options.output, options.apply)
            print(json.dumps({k: result.get(k) for k in ('state', 'request_sent', 'http_status', 'error')}, ensure_ascii=True))
        finally: await close_client()
    asyncio.run(main())
