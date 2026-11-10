"""Capability questions must refresh discovery instead of reusing old answers."""
import re
import json

from agent.context_budget import _discovery_view
from core.user_errors import diagnostic_text


def needs_capability_refresh(messages):
    start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=-1)
    if start < 0:
        return False
    text = str(messages[start].get('content', ''))
    intent = re.search(r'你(?:目前|现在|当前)?(?:具备|有|支持|能做).{0,15}(?:能力|功能|什么|哪些)|'
                       r'(?:能力|功能)(?:目录|清单|介绍)|(?:介绍|列出|更新|刷新).{0,12}能力|'
                       r'what can you do|your capabilities', text, re.I)
    return bool(intent) and not any(m.get('role') == 'tool' and m.get('name') == 'get_capabilities'
                                    for m in messages[start+1:])


async def read_capability_directory(messages, selected_tools, lookup):
    """Application-owned read, before inference; no provider forced tool choice.

    This executes the real local tool. Do not invent an assistant tool call or
    reasoning_content: thinking providers require their own continuation data.
    """
    if not needs_capability_refresh(messages) or not any(
            t['function']['name'] == 'get_capabilities' for t in selected_tools):
        return ''
    try:
        tool = lookup('get_capabilities')
        if tool is None:
            raise ValueError('本地能力目录工具不可用。')
        return await tool()
    except Exception as exc:
        return json.dumps({'complete': False, 'error': diagnostic_text(exc)}, ensure_ascii=False)


def capability_context(raw):
    if not raw:
        return ''
    # Reuse the same bounded projection as actual directory tool responses.
    # Raw source remains in graph state; provider messages contain no fake tool exchange.
    projected = _discovery_view({'role': 'tool', 'name': 'get_capabilities', 'content': raw})['content']
    return ('以下是应用在本轮推理前实际读取的能力目录证据，仅用于回答本次能力问题。'
            '本轮已经刷新目录，无需重复读取；若包含error或不可用说明，必须说明无法完整核实，不能沿用旧答案。'
            '不显示处理时长或历史核查日期；不把工具注册等同现场验收。\n' + projected)
