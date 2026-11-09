import asyncio
import json
from tools.tool_discovery import get_capabilities
from tools import mcp
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES


def test_overview_matches_registered_inventory():
    data=json.loads(asyncio.run(get_capabilities()))
    names={t.name for t in asyncio.run(mcp.list_tools())}
    entries=[t for g in data['groups'] for t in g['tools']]
    assert {t['name'] for t in entries}=={n for n,p in TOOL_POLICIES.items() if p.default_visible and n in names}
    assert json.loads(asyncio.run(get_capabilities(tool_name='patch_mfgorder')))['tool']['status']=='disabled'


def test_details_use_actual_schema_and_limits():
    data=json.loads(asyncio.run(get_capabilities(tool_name='release_container')))['tool']
    assert 'container_name' in data['input_schema']['required']
    assert '不是工单释放' in data['limitations']
    assert data['always_confirm_in_app'] is True
    assert 'error' in json.loads(asyncio.run(get_capabilities(group='invented')))
    assert 'error' in json.loads(asyncio.run(get_capabilities(tool_name='release_mfgorder')))


def test_discovery_always_available():
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    selected=select_tools(tools,[{'role':'user','content':'你支持哪些异常处理能力？'}])
    assert 'get_capabilities' in {t['function']['name'] for t in selected}
    data=json.loads(asyncio.run(get_capabilities(group='reference')))
    assert 'list_production_reasons' in {t['name'] for g in data['groups'] for t in g['tools']}


def test_core_gap_snapshot_is_scoped_and_not_a_live_health_claim():
    overview = json.loads(asyncio.run(get_capabilities()))['capability_gaps']
    assert overview['checked_at'] and overview['live_health_check'] is False
    items = {g['id']: g for g in overview['items']}
    assert items['G09']['status'] == 'partial'  # Restricted Container loss is implemented.
    assert items['G10']['status'] == 'implemented' and '2026-11-09' in items['G10']['current']
    assert items['G20']['status'] == 'partial' and 'UOM已有' in items['G20']['current']
    assert items['G01']['status'] == 'contract_unconfirmed'  # Never invent a release transaction.
    material = json.loads(asyncio.run(get_capabilities(group='material')))['capability_gaps']
    assert material['items'] and all('material' in g['groups'] for g in material['items'])
    assert not any(g['id'] == 'G14' for g in material['items'])


def test_scrap_preflight_is_not_misreported_as_an_unimplemented_feature():
    ready=json.loads(asyncio.run(get_capabilities(tool_name='get_sampling_lot_scrap_readiness')))['tool']
    assert '不表示报废未接入' in ready['limitations']
    scrap=json.loads(asyncio.run(get_capabilities(tool_name='prepare_sampling_lot_scrap')))['tool']
    assert '正负现场验收' in scrap['limitations'] and 'resolve_history' in scrap['limitations']


def test_gap_evidence_and_source_references_are_complete():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'docs/core-capability-gaps.json').read_text(encoding='utf-8'))
    assert len({g['id'] for g in data['gaps']}) == len(data['gaps'])
    for gap in data['gaps']:
        assert gap['groups'] and set(gap['groups']) <= {p.group for p in TOOL_POLICIES.values()}
        assert all((root / path).is_file() for path in gap['code'])
        for ref in gap['contracts']:
            contract = data['contracts'][ref]
            assert contract['http_status'] == 200 and contract['operations']
            assert contract['listed_in_catalog'] and len(contract['sha256']) == 64
