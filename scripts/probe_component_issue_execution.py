"""One-shot mode-5 execution contract probe, restricted to recorded CDX fixtures.

Default is planning. --apply reserves the fixture journal. An explicit correction
is allowed only for the recorded pre-action OData select rejection. Business
failures/timeouts never replay. This is not a general production issue tool.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import get_client, get_headers, close_client
from core.transfer_attempt_lock import attempt_lock
from scripts.verify_component_issue_contract import build, ref
from tools.component_issue import get_component_requirement, get_component_issue_history
from tools.container_inquiry import get_container_details, get_container_history


def fixture_prefix(fixture):
    names = fixture.get('names', {})
    container = names.get('container', '')
    prefix = container.removesuffix('-C01')
    if (prefix != 'CDX-T05-20261024-B' or names != {
            'component': prefix + '-COMP', 'target': prefix + '-TARGET',
            'order': prefix + '-MO', 'container': prefix + '-C01'}):
        raise ValueError('This probe accepts only the existing isolated B fixture names.')
    return prefix


def validate_fixture(fixture, status, requirement):
    prefix = fixture_prefix(fixture)
    if (not prefix.startswith('CDX-T05-') or fixture.get('environment') != CAMSTAR_BASE_URL.rstrip('/')
            or fixture.get('state') != 'fixtures_created_requirement_matched_business_execution_unverified'):
        raise ValueError('Only a recorded, unconsumed isolated T05 fixture is supported.')
    detail = status.get('data', {}).get('ContainerStatusDetails', {})
    container = detail.get('Container', {})
    if (status.get('complete') is not True or container.get('Name') != prefix + '-C01'
            or detail.get('ProductName') != prefix + '-TARGET' or detail.get('ProductRev') != '1'
            or detail.get('MfgOrderName') != prefix + '-MO' or detail.get('Status') != 1
            or detail.get('InProcess') is not True or detail.get('Qty') != 1
            or detail.get('UOMName') != 'EACH'):
        raise ValueError('Current container is not the active isolated fixture context.')
    # Bind to the successful creation acknowledgement, not just a name prefix.
    created = [a for a in fixture.get('attempts', []) if a.get('kind') == 'Container'
               and a.get('state') == 'created_and_read_verified']
    if (len(created) != 1 or container.get('InstanceId') != '00041080000000e3'
            or created[0].get('name') != container.get('Name')):
        raise ValueError('This probe is restricted to the already verified B fixture identity.')
    body = build(requirement, 1)
    row = requirement['data']['IssueDetails']
    if (body['container'] != ref(container) or body['serviceDetails'][0]['product']['name'] != prefix + '-COMP'
            or body['serviceDetails'][0]['product']['revision'] != '1'
            or body['serviceDetails'][0]['bomLineItem']['parent']['instanceId'] != detail['MfgOrder']['InstanceId']
            or any(row.get(k) != 0 for k in ('QtyIssued', 'ActualQtyIssued'))
            or row.get('QtyRequired') != 2 or row.get('NetQtyRequired') != 2):
        raise ValueError('Demand is not the expected untouched isolated 2 EACH requirement.')
    return body


def rejected_select_before_action(previous):
    """Only the recorded OData URI rejection can be corrected, never business failures."""
    response = previous.get('response', {}).get('error', {})
    inner = response.get('innererror', {})
    return (previous.get('http_status') == 400 and previous.get('request_sent') is True
            and previous.get('select_correction') is not True
            and previous.get('request', {}).get('params', {}).get('$select') == 'Container,CompletionMsg,TxnId'
            and response.get('message') == "The query specified in the URI is not valid. Could not find a property named 'TxnId' on type 'shopfloor.ComponentIssue'."
            and inner.get('type') == 'Microsoft.OData.ODataException'
            and inner.get('stacktrace', '').endswith('EnableQueryAttribute.OnActionExecuting(ActionExecutingContext actionExecutingContext)'))


async def probe(fixture_path, output, apply=False, correct_rejected_select=False):
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    prefix = fixture_prefix(fixture)
    # A single fixed journal is shared by every apply invocation for this fixture.
    journal = fixture_path.with_name(fixture_path.stem + '-execution-probe.json')
    if output.resolve() in (fixture_path.resolve(), journal.resolve()):
        raise ValueError('Output must not overwrite the fixture or fixed execution journal.')
    report = {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'fixture': str(fixture_path.resolve()),
              'execute': apply, 'request_sent': False, 'retry_allowed': False,
              'transaction_verified': False, 'service_accepted': False, 'state': 'planning',
              'created_at': datetime.now(timezone.utc).isoformat()}
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if apply:
            journal.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        with attempt_lock(fixture_path, 'component-execution-probe'):
            previous = None
            if journal.exists():
                previous = json.loads(journal.read_text(encoding='utf-8'))
                if not (apply and correct_rejected_select and rejected_select_before_action(previous)
                        and previous.get('environment') == report['environment']
                        and previous.get('fixture') == report['fixture']):
                    raise ValueError('An execution journal already exists; review it, never replay this probe.')
            elif correct_rejected_select:
                raise ValueError('No recorded URI rejection exists to correct.')
            container, product = prefix + '-C01', prefix + '-COMP'
            report['status_before'] = json.loads(await get_container_details(container))
            report['requirement_before'] = json.loads(await get_component_requirement(container, product))
            body = validate_fixture(fixture, report['status_before'], report['requirement_before'])
            report['request'] = {'body': body, 'path': '/api/ComponentIssue', 'params': {
                'execute': 'true', '$select': 'Container,CompletionMsg',
                '$expand': 'ServiceDetails,IssueActualDetails'}}
            report['issue_history_before'] = json.loads(await get_component_issue_history(container))
            if report['issue_history_before'].get('complete') is not True or report['issue_history_before'].get('rows') != []:
                raise ValueError('A complete, empty initial actual-issue history is required.')
            report['state'] = 'prepared'
            if not apply:
                save()
                return report
            if previous is not None:
                if previous.get('request', {}).get('body') != body:
                    raise ValueError('URI correction cannot change the original issue body.')
                report.update(select_correction=True, prior_attempt=previous)
            # Exclusive creation reserves the fixture even if a process dies before POST.
            if previous is None:
                with journal.open('x', encoding='utf-8') as handle:
                    json.dump(report, handle, ensure_ascii=False, indent=2)
            report.update(state='unknown', request_sent=True)
            save()
            response = await asyncio.wait_for(get_client().post(
                CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/') + '/api/ComponentIssue',
                headers=get_headers(), json=body, params=report['request']['params'],
                follow_redirects=False), timeout=30)
            report['http_status'] = response.status_code
            report['raw_response'] = response.text
            try:
                report['response'] = response.json()
            except ValueError:
                pass
            data = report.get('response')
            report['service_accepted'] = (200 <= response.status_code < 300 and isinstance(data, dict)
                                          and not data.get('ErrorCode') and not data.get('error'))
            # A business error alone does not establish whether the transaction committed.
            save()
            report['requirement_after'] = json.loads(await get_component_requirement(container, product))
            report['issue_history_after'] = json.loads(await get_component_issue_history(container))
            report['container_history_after'] = json.loads(await get_container_history(container, history_source='mainline'))
            report['state'] = 'evidence_collected_requires_review'
            save()
    except asyncio.CancelledError:
        if report['request_sent']:
            report['state'] = 'unknown'
            save()
        raise
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        if not report['request_sent']:
            report['state'] = 'not_submitted'
            # Never overwrite an earlier execution journal on rejected replay.
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.resolve() != journal.resolve():
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        else:
            report['state'] = 'unknown'
            save()
    finally:
        await close_client()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--correct-rejected-select', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(probe(args.fixture, args.output, args.apply, args.correct_rejected_select))
    print(json.dumps({k: result.get(k) for k in ('state', 'request_sent', 'http_status', 'error')}))
    sys.exit(1 if result.get('error') or args.apply and not result.get('service_accepted') else 0)
