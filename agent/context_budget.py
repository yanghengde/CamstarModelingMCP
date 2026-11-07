"""Conservative history budget without rewriting business evidence/checkpoints.

UTF-8 byte count bounds byte-tokenizer input conservatively; it is not a claim
of provider-exact tokens. Tool exchanges, user instructions and the latest two
turns are never truncated. If that evidence does not fit, stop before inference.
"""
import json
import re
from config import AGENT_HISTORY_TOKEN_BUDGET


class ContextBudgetExceeded(ValueError):
    pass


_REFERENCE_OR_STATE = re.compile(
    r'\d|(?i:\b(?:plan|digest|instance|revision|unknown|approval|confirm)\b)|[A-Z][A-Z0-9_-]+'
    r'|计划|对象|容器|工单|产品|工艺|版本|编号|确认|审批|取消|未知|待核实')


def estimate_tokens(message):
    return len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 16


def budget_history(messages, budget=None):
    budget = AGENT_HISTORY_TOKEN_BUDGET if budget is None else budget
    costs = [0 if m.get('role') == 'system' else estimate_tokens(m) for m in messages]
    total = sum(costs)
    if total <= budget:
        return list(messages), {'history_estimated_tokens': total, 'history_removed_messages': 0}
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
    return result, {'history_estimated_tokens': total, 'history_removed_messages': len(removed)}
