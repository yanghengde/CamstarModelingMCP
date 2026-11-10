"""Capability questions must refresh discovery instead of reusing old answers."""
import re


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


def capability_tool_choice(messages, selected_tools):
    if needs_capability_refresh(messages) and any(t['function']['name'] == 'get_capabilities' for t in selected_tools):
        return {'type': 'function', 'function': {'name': 'get_capabilities'}}
    return 'auto'
