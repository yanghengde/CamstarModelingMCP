"""Avoid repeating an identical failed read within the same user request."""
import json
from core.tool_catalog import TOOL_POLICIES
from core.user_errors import tool_diagnostic


def previous_failed_read(messages, name, arguments):
    policy = TOOL_POLICIES.get(name)
    if policy is None or policy.effect != 'read':
        return None
    start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=0)
    calls = {}
    for message in messages[start:]:
        for call in message.get('tool_calls', []) if message.get('role') == 'assistant' else []:
            function = call.get('function') or {}
            try:
                calls[call['id']] = (function.get('name'), json.loads(function.get('arguments') or '{}'))
            except (ValueError, TypeError, KeyError):
                continue
        if (message.get('role') == 'tool'
                and calls.get(message.get('tool_call_id')) == (name, arguments)):
            diagnostic = tool_diagnostic(message.get('content'))
            if diagnostic:
                return diagnostic
    return None
