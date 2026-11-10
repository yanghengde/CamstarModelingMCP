"""Capability questions must refresh discovery instead of reusing old answers."""
import re
import json
import html

from agent.context_budget import _discovery_view
from core.user_errors import diagnostic_text
from config import AGENT_CAPABILITY_FAST_REPLY


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


def render_capability_overview(message, raw):
    """Render only an unambiguous generic overview from actual registry data.

    Detailed/domain-specific questions still use the model. Every registered
    group is represented; full gap restrictions stay available in the reply.
    """
    if not AGENT_CAPABILITY_FAST_REPLY or not needs_capability_refresh([{'role': 'user', 'content': message}]):
        return None
    if re.search(r'详细|参数|schema|对比|比较|为什么|如何|接口|原理|只|仅|特别|重点|针对|关于|方面|'
                 r'质量|工单|产品|资源|采集|物料|设备|容器|工艺|路线|审批|检验|生产|BOM|SPC|'
                 r'SQL|JSON|英文|English|格式|不要|修改|修复|代码|开发|文件|调整|设置', message, re.I):
        return None
    try:
        data = json.loads(raw)
        if data.get('error') or not isinstance(data.get('groups'), list):
            return None
        gaps = data['capability_gaps']
        if gaps.get('available') is False or not isinstance(gaps.get('items'), list):
            return None
        groups = [g for g in data['groups'] if g['tools']]
        if not groups:
            return None
        def cell(value):
            return html.escape(re.sub(r'(?:于)?20\d\d-\d\d-\d\d', '', str(value))).replace('|', '\\|').replace('\n', '<br>')
        lines = ['# 当前能力说明', '',
            '依据本次实际目录整理。工具已接入不等于现场已验收，实际执行仍受权限、配置和对象状态约束。', '',
            '| 业务领域 | 已接入的操作类型 |', '|---|---|']
        for group in groups:
            actions = set()
            for tool in group['tools']:
                if tool.get('status') == 'disabled':
                    continue
                name, effect = tool['name'], tool['effect']
                if name.startswith(('prepare_', 'preview_')):
                    actions.add('操作预览')
                elif name.startswith(('verify_', 'reconcile_', 'review_', 'recover_')):
                    actions.add('核实与恢复')
                elif name.startswith(('execute_', 'collect_', 'issue_', 'container_move', 'container_start', 'hold_', 'release_')) and effect != 'read':
                    actions.add('受控执行')
                elif effect == 'read':
                    actions.add('查询与校验')
                else:
                    actions.add({'create': '新建或登记', 'update': '修改', 'delete': '删除'}.get(effect, '受控操作'))
            lines.append('| ' + cell(group['label']) + ' | ' + '、'.join(sorted(actions)) + ' |')
        lines += ['', '各领域按对象提供上述入口，具体条件见对应限制。', '', '**尚不支持或受限**', '']
        buckets = {}
        for item in gaps['items']:
            if item['status'] != 'implemented':
                buckets.setdefault(item['status_label'], []).append(cell(item['title']))
        for label, titles in buckets.items():
            lines.append('- **' + cell(label) + '**：' + '；'.join(titles) + '。')
        if data.get('freshness', {}).get('reload_required'):
            lines += ['', '**相关代码有变更尚未加载，当前可用范围以运行中的工具目录为准。**']
        lines += ['', '<details><summary>查看各项具体限制</summary>', '']
        for item in gaps['items']:
            if item.get('missing'):
                lines.append('- **' + cell(item['title']) + '**：' + cell(item['missing']))
        for limitation in data.get('limitations', []):
            lines.append('- ' + cell(limitation))
        lines += ['', '</details>']
        return '\n'.join(lines)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
