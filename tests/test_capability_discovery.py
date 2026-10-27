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
