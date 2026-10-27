"""Plan/create six isolated mode-2 source/target objects; never issue material.

Default is read-only. --apply is single-use and requires the complete count to
fit SAFE_CREATE_THRESHOLD. Every mutation is journaled and read back; stop on
any uncertainty, never retry or resume a mutation.
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import request, close_client
from tools.modeling_details import raw_json, _has_next_link
from tools.process_context import ReadContext
from tools.products import create_product
from tools.mfgorders import create_mfgorder
from tools.container_start import container_start, request_container_start_selection_values
from tools.container_inquiry import get_container_details
from tools.component_issue import get_component_requirement
from scripts.prepare_component_issue_fixture import named, selection


async def prepare(prefix, output, apply=False):
    if not re.fullmatch(r'CDX-T05-[A-Z0-9-]{1,40}', prefix):
        raise ValueError('Use a short isolated CDX-T05-... prefix.')
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    journal = output / (prefix + '.json')
    if apply and journal.exists():
        raise ValueError('Mutation journal already exists; never replay this fixture.')
    if apply and 6 > SAFE_CREATE_THRESHOLD:
        raise ValueError('Six objects exceed configured confirmation threshold; no mutations attempted.')
    names = {key: prefix + suffix for key, suffix in (
        ('component', '-COMP'), ('target', '-TARGET'), ('source_order', '-SRC-MO'),
        ('target_order', '-DST-MO'), ('source', '-SRC'), ('destination', '-DST'))}
    ctx = ReadContext(max_requests=45)
    source_product = await ctx.resolve('Products', {'Name': 'WM-900A', 'Revision': 'A'})
    workflow = await ctx.resolve('Workflows', source_product['Workflow'])
    product_type = await ctx.resolve('ProductTypes', source_product['ProductType'], revisioned=False)
    unit = await ctx.resolve('UOMs', {'Name': 'EACH'}, revisioned=False)
    level = await ctx.resolve('ContainerLevels', {'Name': 'BOX'}, revisioned=False)
    released = await ctx.resolve('OrderStatuses', {'Name': 'Released'}, revisioned=False)
    existing_order = await ctx.resolve('MfgOrders', {'Name': 'MO-100076435'}, revisioned=False)
    if existing_order.get('Product', {}).get('Name') != source_product['Name']:
        raise ValueError('Existing Start selection context does not match source product.')
    for entity, keys in (('Products', ('component', 'target')), ('MfgOrders', ('source_order', 'target_order'))):
        for key in keys:
            found = await ctx.get(entity, params={'$filter': "Name eq '" + names[key] + "'", '$top': 2})
            if found.get('value') != [] or _has_next_link(found):
                raise ValueError('Name absence not confirmed: ' + names[key])
    for key in ('source', 'destination'):
        raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
            body={'queryText': "SELECT TOP 2 ContainerId FROM Container WHERE ContainerName=N'" + names[key] + "'"},
            response_formatter=raw_json, retry_read_only=True)
        if json.loads(raw) != []:
            raise ValueError('Container name absence not confirmed: ' + names[key])
    context = {'mfg_order_name': existing_order['Name'], 'product_name': source_product['Name'],
               'product_revision': source_product['Revision'], 'qty': 1, 'level_name': level['Name']}
    owner = selection(await request_container_start_selection_values('Details.Owner', **context), 'Owner')
    reason = selection(await request_container_start_selection_values('Details.StartReason', **context), 'StartReason')
    material = {'name': 'T05-BULK', 'product': {'name': names['component'], 'revision': '1'},
                'qtyRequired': 2, 'qty2Required': 0, 'issueControl': 2, 'UOM': named(unit)}
    report = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'environment': CAMSTAR_BASE_URL.rstrip('/'),
              'names': names, 'count': 6, 'issue_control': 2, 'source_quantity': 3, 'destination_quantity': 1,
              'material_quantity': 2, 'level': level['Name'], 'owner': owner, 'start_reason': reason,
              'product_revision': '1', 'unit': unit['Name'], 'workflow': named(workflow, True),
              'material': material, 'attempts': [], 'state': 'planned',
              'numbering': 'explicit names, autoNumber=false', 'component_issue_submitted': False}
    if not apply:
        (output / (prefix+'-plan.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report

    def save():
        journal.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    async def create(kind, name, function, arguments, verify):
        record = {'kind': kind, 'name': name, 'arguments': arguments, 'state': 'attempting'}
        report['attempts'].append(record); save()
        try:
            record['response'] = await function(**arguments); save()
            record['verified_object'] = await verify()
            record['state'] = 'created_and_read_verified'; save()
        except BaseException as error:
            record.update(state='unknown_or_failed', error=repr(error)); save()
            raise

    async def verify_product(key):
        obj = await ReadContext().resolve('Products', {'Name': names[key], 'Revision': '1'})
        if (obj.get('StdStartUOM', {}).get('Name') != unit['Name']
                or obj.get('Workflow', {}).get('Name') != workflow['Name']
                or obj['Workflow'].get('Revision') != workflow['Revision']):
            raise ValueError('Created product workflow/unit mismatch.')
        return {key: obj.get(key) for key in ('Name', 'Revision', 'InstanceID', 'StdStartUOM', 'Workflow')}

    async def verify_order(key, product_key, qty):
        ctx = ReadContext(); obj = await ctx.resolve('MfgOrders', {'Name': names[key]}, revisioned=False)
        if (obj.get('Product', {}).get('Name') != names[product_key]
                or obj['Product'].get('Revision') != '1' or obj.get('Qty') != qty):
            raise ValueError('Created order product/version/quantity mismatch.')
        if key == 'target_order':
            expanded = await ctx.get('MfgOrders', obj['InstanceID'], {'$expand': 'MaterialList'})
            rows = expanded.get('MaterialList')
            if (expanded.get('InstanceID') != obj['InstanceID'] or not isinstance(rows, list) or len(rows) != 1
                    or rows[0].get('Name') != 'T05-BULK' or rows[0].get('IssueControl') != 2
                    or rows[0].get('QtyRequired') != 2 or rows[0].get('Product', {}).get('Name') != names['component']
                    or rows[0]['Product'].get('Revision') != '1' or rows[0].get('UOM', {}).get('Name') != unit['Name']
                    or _has_next_link(expanded)):
                raise ValueError('Target order material row mismatch.')
            obj['MaterialList'] = rows
        return obj

    async def verify_container(key, order_key, product_key, qty):
        result = json.loads(await get_container_details(names[key])); row = result.get('data', {}).get('ContainerStatusDetails', {})
        if (result.get('complete') is not True or row.get('Container', {}).get('Name') != names[key]
                or row.get('MfgOrder', {}).get('Name') != names[order_key] or row.get('Qty') != qty
                or row.get('Product', {}).get('Name') != names[product_key] or row['Product'].get('Revision') != '1'
                or row.get('ContainerLevel', {}).get('Name') != level['Name'] or row.get('UOM', {}).get('Name') != unit['Name']):
            raise ValueError('Created Container context mismatch.')
        return row

    try:
        for key in ('component', 'target'):
            arguments = {'name': names[key], 'revision': '1', 'product_type_name': product_type['Name'], 'status': 1,
                'description': 'TEST DATA ONLY: isolated mode-2 source preview; not production.',
                'body_json': json.dumps({'StdStartUOM': named(unit), 'workflow': named(workflow, True)})}
            await create('Product', names[key], create_product, arguments, lambda key=key: verify_product(key))
        for key, product_key, qty in (('source_order', 'component', 3), ('target_order', 'target', 1)):
            extra = {'UOM': named(unit)}
            if key == 'target_order': extra['materialList'] = [material]
            arguments = {'name': names[key], 'product_name': names[product_key], 'product_revision': '1', 'qty': qty,
                'order_status_name': released['Name'], 'description': 'TEST DATA ONLY: mode-2 preview fixture.',
                'body_json': json.dumps(extra)}
            await create('MfgOrder', names[key], create_mfgorder, arguments,
                         lambda key=key, product_key=product_key, qty=qty: verify_order(key, product_key, qty))
        for key, order_key, product_key, qty in (('source', 'source_order', 'component', 3), ('destination', 'target_order', 'target', 1)):
            arguments = {'mfg_order_name': names[order_key], 'product_name': names[product_key], 'product_revision': '1',
                         'qty': qty, 'level_name': level['Name'], 'owner_name': owner, 'start_reason_name': reason}
            for expression, field, wanted in (('Details.Owner', 'Owner', owner), ('Details.StartReason', 'StartReason', reason)):
                if selection(await request_container_start_selection_values(expression, **arguments), field) != wanted:
                    raise ValueError('Start context reference changed; stop before Start.')
            arguments.update(container_name=names[key], comments='TEST DATA ONLY: isolated bulk source preview.')
            await create('Container', names[key], container_start, arguments,
                         lambda key=key, order_key=order_key, product_key=product_key, qty=qty: verify_container(key, order_key, product_key, qty))
        report['requirement'] = json.loads(await get_component_requirement(names['destination'], names['source']))
        if (report['requirement'].get('status') != 'matched' or report['requirement'].get('complete') is not True
                or report['requirement']['data']['IssueDetails'].get('IssueControl') != 2):
            raise ValueError('Created mode-2 source requirement did not match.')
        report['state'] = 'fixtures_created_mode2_requirement_matched'; save()
    except BaseException:
        report['state'] = 'stopped_without_retry'; save()
        raise
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'docs/t05-fixtures')
    parser.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    async def main():
        try:
            report = await prepare(options.prefix, options.output, options.apply)
            print(json.dumps({key: report[key] for key in ('state', 'names', 'count', 'source_quantity', 'destination_quantity', 'level', 'owner', 'start_reason')}, ensure_ascii=True))
        finally:
            await close_client()
    asyncio.run(main())
