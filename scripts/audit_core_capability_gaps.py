"""Refresh GET-only Swagger evidence for the reviewed core capability backlog.

Does not call business endpoints, even execute=false. Status/priority/current
scope are human-reviewed, never inferred from a Swagger name alone.
"""
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL, CAMSTAR_QUERY_BASE_URL
from core.http_client import get_headers

REPORT = ROOT / 'docs/core-capability-gaps.json'


def render_document(report):
    labels = {'not_integrated': '未接入', 'partial': '部分支持', 'blocked': '执行门禁关闭',
              'contract_unconfirmed': '业务契约待确认', 'validation_pending': '验收与自动核对待补',
              'implemented': '已实现并限定验收'}
    good = sum(c.get('http_status') == 200 and 'operations' in c for c in report['contracts'].values())
    lines = ['# 核心能力缺口与支持边界', '',
        f"核查时间：{report['checked_at']}（Asia/Shanghai）。", '',
        '本次仅 GET 现场 Swagger 目录和契约，没有执行业务事务或 execute=false 预览。'
        '以当前 MCP 注册清单和代码限制人工对照；Swagger HTTP 200 仅说明接口定义可读，不证明业务许可或现场效果。', '',
        '## 范围与证据', '',
        report['scope'], '',
        '、'.join(f"{s} {v['group_count']} 个分组" for s, v in report['catalogs'].items()) +
        f"；针对核心缺口核对 {len(report['contracts'])} 份契约，{good} 份可读。"
        '目录分组数含基类、维护服务和重复路由，不是业务功能数，不能据此计算覆盖率。', '',
        '- [机器可读盘点及契约证据](core-capability-gaps.json)：保留目录分组、HTTP状态、相对地址、方法、请求schema字段/只读字段和响应摘要哈希。',
        '- `python scripts/audit_core_capability_gaps.py`：重新读取现场Swagger并更新本文及JSON；支持状态和优先级仍须随实现人工复核。',
        '- P1：生产/质量闭环优先候选；P2：扩展能力候选。优先级为本次建议，未自动排期。', '',
        '## 当前缺口总表', '',
        '| 编号 | 核心能力 | 状态 | 优先级 | 已有能力 | 尚缺范围 |',
        '|---|---|---|---|---|---|']
    for g in report['gaps']:
        lines.append('| ' + ' | '.join([g['id'], g['title'], labels[g['status']], g['priority'], g['current'], g['missing']]) + ' |')
    lines += ['', '## 每项契约与后续验证', '']
    for g in report['gaps']:
        lines += [f"### {g['id']} {g['title']}", '',
                  'Swagger分组：' + '、'.join(f'`{r}`' for r in g['contracts']) + '。', '',
                  '代码依据：' + '、'.join(f'[{Path(p).name}](../{p})' for p in g['code']) + '。', '',
                  '下一步：' + g['next_step'], '']
    lines += ['## 对原能力介绍的修正', '',
        '- 生产容器报废不能统称“未提供”：基础单容器已有受限数量损失/全量模式；复杂容器及抽样批报废仍受限。PartScrap是资源零件相关事务。',
        '- 质量对象开启/关闭不能统称“未提供”：基础Event开启及条件关闭、CAPA关闭、NCR关闭已有入口；通用对象及复杂组合并未完整开放。',
        '- 电子签名不是全部未支持：已有单人质量签名与条件审批；通用NCR多签/协签门禁仍关闭。',
        '- UOM列表、详情、新建和修改已接入；维护单位名称不代表换算关系可用。',
        '- “所有写入均需确认”已不准确：确认遵循当前对话的标准/自动模式和执行层策略；所需选择、缺失参数及用户明确要求先确认仍需等待。',
        '- Swagger存在、工具已接入、仅预览、执行开放和现场已验收分别陈述，不将回读同值直接当作未知请求成功。', '',
        '## 与历史资料的关系', '',
        '[旧覆盖审查](mcp-tool-coverage-review.md)、[旧生产API盘点](production-api-capability-audit.md)保留为历史证据；'
        '其中未接入结论不能直接当作当前状态。当前缺口以本文及运行时注册目录共同核对；本表也不是未来版本能力承诺。', '']
    (ROOT / 'docs/core-capability-gaps.md').write_text('\n'.join(lines), encoding='utf-8')


async def audit():
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    bases = {'Modeling': CAMSTAR_BASE_URL, 'Shopfloor': CAMSTAR_SHOPFLOOR_BASE_URL,
             'Query': CAMSTAR_QUERY_BASE_URL}
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(verify=False, timeout=20, headers=get_headers()) as client:
        async def get(service, path):
            try:
                async with semaphore:
                    r = await client.get(bases[service].rstrip('/') + path)
                return r.status_code, r.text
            except httpx.HTTPError as exc:
                return None, type(exc).__name__

        async def catalog(service):
            status, body = await get(service, '/swagger/index.html')
            try:
                match = re.search(r"var configObject = JSON.parse\('(.*?)'\);", body)
                names = sorted(item['name'] for item in json.loads(match[1])['urls'])
            except (TypeError, ValueError, KeyError):
                names = []
            return service, {'http_status': status, 'group_count': len(names), 'groups': names}

        report['catalogs'] = dict(await asyncio.gather(*(catalog(s) for s in bases)))
        async def contract(ref):
            service, name = ref.split('/')
            path = f'/swagger/{name}/swagger.json'
            status, body = await get(service, path)
            out = {'source_path': path, 'http_status': status,
                   'listed_in_catalog': name in report['catalogs'][service]['groups']}
            try:
                data = json.loads(body)
                if status != 200 or not isinstance(data.get('paths'), dict):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                return ref, out
            out['sha256'] = hashlib.sha256(body.encode('utf-8')).hexdigest()
            schemas = data.get('components', {}).get('schemas', data.get('definitions', {}))
            out['operations'] = []
            roots = set()
            for endpoint, operations in data['paths'].items():
                for method, op in operations.items():
                    if method not in ('get', 'post', 'put', 'patch', 'delete'):
                        continue
                    content = op.get('requestBody', {}).get('content', {})
                    refs = sorted({v.get('schema', {}).get('$ref', '') for v in content.values()} - {''})
                    roots.update(r.rsplit('/', 1)[-1] for r in refs)
                    out['operations'].append({'method': method.upper(), 'path': endpoint, 'body_refs': refs})
            roots.update(n for n in schemas if n in {name, name.rstrip('s')+'Entity',
                'ChangeQtyDetails', 'LossDetails', 'ComponentIssueDetails', 'ESigPasswordCapture'})
            out['request_schemas'] = {n: {'required': schemas[n].get('required', []),
                'fields': sorted(schemas[n].get('properties', {})),
                'read_only_fields': sorted(k for k, v in schemas[n].get('properties', {}).items() if v.get('readOnly'))}
                for n in sorted(roots) if n in schemas}
            return ref, out
        refs = sorted({r for gap in report['gaps'] for r in gap['contracts']})
        report['contracts'] = dict(await asyncio.gather(*(contract(r) for r in refs)))
    report['checked_at'] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    report['method'] = 'GET Swagger index and contracts only; no business reads/writes or transaction previews'
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    render_document(report)
    good = sum(c.get('http_status') == 200 and 'operations' in c for c in report['contracts'].values())
    print(f'{len(report["gaps"])} reviewed gaps; {good}/{len(refs)} contracts readable')
    for ref, c in report['contracts'].items():
        if c.get('http_status') != 200 or 'operations' not in c:
            print(ref, c)


if __name__ == '__main__':
    asyncio.run(audit())
