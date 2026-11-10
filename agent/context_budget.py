"""Bounded history with lightweight directory views, without rewriting checkpoints.

Use a local BPE token estimate, not UTF-8 bytes counted as tokens. This is not
the provider's exact usage. Tool exchanges, user instructions and the latest two
turns are never truncated. If that evidence does not fit, stop before inference.
Directory projection is independently enabled by default even with no token cap;
it changes only navigation metadata supplied to the model, never business receipts.
"""
import json
import re
from functools import lru_cache
from config import AGENT_HISTORY_TOKEN_BUDGET, AGENT_PROJECT_DISCOVERY


class ContextBudgetExceeded(ValueError):
    pass


_REFERENCE_OR_STATE = re.compile(
    r'\d|(?i:\b(?:plan|digest|instance|revision|unknown|approval|confirm)\b)|[A-Z][A-Z0-9_-]+'
    r'|计划|对象|容器|工单|产品|工艺|版本|编号|确认|审批|取消|未知|待核实')


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken
        return tiktoken.get_encoding('cl100k_base')
    except Exception:
        return None


def initialize_context_budget():
    if AGENT_HISTORY_TOKEN_BUDGET > 0 and _encoding() is None:
        raise RuntimeError('历史预算分词器不可用，请在服务 Python 环境安装 requirements.txt 并检查 tiktoken 编码缓存。')


@lru_cache(maxsize=256)
def _text_tokens(text):
    encoding = _encoding()
    return (len(encoding.encode(text, disallowed_special=())) if encoding is not None
            else len(text.encode('utf-8'))) + 16


def estimate_tokens(message):
    text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
    return _text_tokens(text)


def _discovery_view(message, completed=False):
    """Project only the local capability directory, never business evidence.

    The full tool response remains in memory/checkpoints. Exact tool-detail
    responses (including schemas and caveats) are always kept verbatim.
    """
    if message.get('role') != 'tool' or message.get('name') != 'get_capabilities':
        return message
    try:
        data = json.loads(message.get('content', ''))
        if not isinstance(data, dict) or 'tool' in data or not isinstance(data.get('groups'), list):
            return message
        groups = []
        for group in data['groups']:
            tools = group['tools']
            if not isinstance(tools, list):
                return message
            groups.append({**{k: v for k, v in group.items() if k != 'tools'},
                           'tool_count': len(tools),
                           'effects': sorted({t['effect'] for t in tools}),
                           'example_tools': [t['name'] for t in tools[:3]],
                           'disabled_tools': [t['name'] for t in tools if t.get('status') == 'disabled']})
        # Gap current/missing/status/date are business limitations, not optional
        # presentation metadata. Keep them in full, as well as global caveats.
        projected = {**data, 'groups': groups,
                     'directory_projection': {
                         'scope': '仅能力目录概览；示例工具不是完整工具清单。',
                         'detail_rule': '具体工具的参数、限制或执行就绪性必须用 get_capabilities(tool_name=实际名称) 查询；不要从分组计数或示例推断。',
                         'full_response_preserved': True}}
        gaps = data.get('capability_gaps')
        if isinstance(gaps, dict) and isinstance(gaps.get('items'), list):
            projected['capability_gaps'] = {**gaps, 'items': [
                {k: v for k, v in item.items() if k not in ('swagger_contracts', 'priority')}
                for item in gaps['items']]}
        if completed:
            # Completed discovery is navigation data, not a business receipt.
            # Keep all groups and constraints classifications; reread precise
            # gaps/details on demand. The original response stays on disk.
            if isinstance(gaps, dict) and isinstance(gaps.get('items'), list):
                projected['capability_gaps'] = {**gaps, 'items': [
                    {k: item[k] for k in ('id', 'title', 'groups', 'status', 'status_label') if k in item}
                    for item in gaps['items']]}
            projected['directory_projection']['scope'] = (
                '已完成轮次的目录索引。完整原响应仍保留；需要具体缺口的 current/missing 或工具限制时重新查询 get_capabilities。')
        content = json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        if estimate_tokens({**message, 'content': content}) < estimate_tokens(message):
            return {**message, 'content': content}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return message


def budget_history(messages, budget=None):
    budget = AGENT_HISTORY_TOKEN_BUDGET if budget is None else budget
    original_cost = sum(estimate_tokens(m) for m in messages if m.get('role') != 'system')
    source_messages = messages
    turn_start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=0)
    if budget > 0 or AGENT_PROJECT_DISCOVERY:
        messages = [_discovery_view(m, completed=i < turn_start) for i, m in enumerate(messages)]
    # Reasoning is provider continuation metadata, not user-visible evidence.
    # Retain it inside the active tool exchange; do not carry old-turn metadata.
    messages = [{k: v for k, v in m.items() if k != 'reasoning_content'}
                if i < turn_start and 'reasoning_content' in m else m
                for i, m in enumerate(messages)]
    seen_directories = {}
    for i, message in enumerate(messages):
        if (message.get('role') == 'tool' and message.get('name') == 'get_capabilities'
                and '"directory_projection"' in str(message.get('content', ''))):
            content = source_messages[i]['content']
            if content in seen_directories:
                messages[i] = {**message, 'content': json.dumps({
                    'directory_duplicate_of_tool_call_id': seen_directories[content],
                    'note': '本次目录与上述工具响应完全相同，沿用其全部分组、缺口与限制。'}, ensure_ascii=False)}
            elif message.get('tool_call_id'):
                seen_directories[content] = message['tool_call_id']
    projected_count = sum(1 for m in messages if m.get('role') == 'tool'
                          and m.get('name') == 'get_capabilities'
                          and '"directory_projection"' in str(m.get('content', '')))
    costs = [0 if m.get('role') == 'system' else estimate_tokens(m) for m in messages]
    total = sum(costs)
    if budget <= 0:
        return list(messages), {'history_budget_enabled': False,
            'history_estimated_tokens': total, 'history_original_estimated_tokens': original_cost,
            'history_removed_messages': 0, 'history_projected_discovery_messages': projected_count,
            'history_token_estimator': 'cl100k_base' if _encoding() else 'utf8_fallback'}
    if total <= budget:
        return list(messages), {'history_estimated_tokens': total, 'history_removed_messages': 0,
                                'history_token_estimator': 'cl100k_base' if _encoding() else 'utf8_fallback',
                                'history_original_estimated_tokens': original_cost,
                                'history_projected_discovery_messages': projected_count}
    users = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    recent = users[-2] if len(users) >= 2 else 0
    # Only discard old narration. Calls/results retain exact JSON, IDs, digest,
    # unknown outcomes and pairing; approvals remain in the original graph.
    removable = [i for i, m in enumerate(messages) if i < recent
                 and m.get('role') == 'assistant' and not m.get('tool_calls')
                 and not _REFERENCE_OR_STATE.search(str(m.get('content', '')))]
    removed = set()
    for i in removable:
        if total <= budget:
            break
        removed.add(i)
        total -= costs[i]
    if total > budget:
        raise ContextBudgetExceeded(
            '本次历史中的用户要求、对象引用和工具证据超过上下文预算，已在推理前停止。'
            '请在新对话明确对象或计划编号继续核实；已有审批和事务状态保持，未自动执行。')
    result = [m for i, m in enumerate(messages) if i not in removed]
    if removed:
        result.insert(1 if result and result[0].get('role') == 'system' else 0,
                      {'role': 'system', 'content': '部分较早的助手说明已省略。用户原指令和全部工具证据保留；不得猜测省略内容或据此扩展授权。'})
    return result, {'history_estimated_tokens': total, 'history_removed_messages': len(removed),
                    'history_token_estimator': 'cl100k_base' if _encoding() else 'utf8_fallback',
                    'history_original_estimated_tokens': original_cost,
                    'history_projected_discovery_messages': projected_count}
