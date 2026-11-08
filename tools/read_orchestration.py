"""MCP entry points for shared read contracts and deterministic query graphs."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from tools import mcp
from tools.modeling_details import raw_json
from core import read_catalog as catalog, read_plan


class OrderDatesParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    order_names: list[str] = Field(min_length=1, max_length=20,
        description='本次明确的工单名列表；包含已关闭容器，无需先查询schema。')
    timezone: str = Field(default='Asia/Shanghai', min_length=1, max_length=100,
        description='结果显示的IANA时区；GMT原值始终保留。')


@mcp.tool
async def search_camstar_read_tools(query: str, limit: int = 5) -> str:
    """搜索已注册只读工具，跨业务组发现工具和真实参数，无MES请求。
    返回的orchestration_allowed表示能否纳入execute_camstar_read_plan；read策略不自动授权批量。
    工单日期优先run_camstar_read_template(template=order_dates)，不逐次猜SQL列名。
    """
    try:
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('query为1..200字符，limit=1..10。')
        from core.tool_catalog import TOOL_POLICIES
        terms = set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]{1,}', query.lower()))
        aliases = {'工单': 'mfgorder order', '日期': 'date time 日期', '开工': 'start 日期',
                   '容器': 'container', '产品': 'product', '工艺': 'workflow process',
                   '历史': 'history', '设备': 'resource', '汇总': 'summary', '吞吐': 'throughput'}
        for word, expanded in aliases.items():
            if word in query:
                terms.update(expanded.split())
        ranked = []
        for tool in await mcp.list_tools():
            policy = TOOL_POLICIES.get(tool.name)
            if not policy or policy.effect != 'read':
                continue
            text = (tool.name + ' ' + (tool.description or '')).lower()
            score = sum((5 if term in tool.name.lower() else 1) for term in terms if term in text)
            if score:
                ranked.append((score, tool))
        ranked.sort(key=lambda x: (-x[0], x[1].name))
        return raw_json({'complete': True, 'source': 'live MCP registry + explicit read policies',
            'tools': [{'name': t.name, 'description': t.description, 'input_schema': t.parameters,
                       'orchestration_allowed': t.name in catalog.READ_TOOLS} for _, t in ranked[:limit]],
            'templates': ['order_dates'], 'mes_write_performed': False})
    except (ValueError, TypeError) as exc:
        return raw_json({'complete': False, 'error': str(exc), 'mes_write_performed': False})


@mcp.tool
async def get_camstar_read_metadata(kind: Literal['overview', 'dataset', 'entity', 'tool'] = 'overview',
        name: str = '', refresh: bool = False) -> str:
    """共享只读契约目录：overview列出数据集/审核工具/模板/业务口径，无MES调用；
    dataset读取已审核物理表的现场列名/类型；entity读指定Modeling实体现场Swagger；
    tool返回指定已注册只读工具真实参数。只缓存元数据6小时，按环境隔离，refresh强制刷新。
    元数据失败不回退过期副本，不缓存生产结果，不含凭据。API字段与SQL字段是两个命名空间。
    """
    try:
        if kind == 'overview':
            result = {'datasets': {key: {'table': v['table'], 'reviewed_fields': v['fields']}
                       for key, v in catalog.DATASETS.items()},
                      'entities': sorted(catalog.ENTITIES), 'orchestration_tools': sorted(catalog.READ_TOOLS),
                      'templates': {'order_dates': {'parameters': ['order_names', 'timezone'],
                          'scope': '工单计划/原生生产日期 + 全部当前关联容器最早FactoryStartDateGMT，含关闭容器'}},
                      'semantics': catalog.SEMANTICS, 'plan_schema': read_plan.PLAN_SCHEMA,
                      'source': 'reviewed read definitions; availability verified on dataset/entity use'}
        elif kind == 'dataset':
            result = await catalog.dataset_metadata(name, refresh)
        elif kind == 'entity':
            result = await catalog.entity_metadata(name, refresh)
        else:
            from core.tool_catalog import TOOL_POLICIES
            policy = TOOL_POLICIES.get(name)
            matches = [t for t in await mcp.list_tools() if t.name == name]
            if not policy or policy.effect != 'read' or len(matches) != 1:
                raise ValueError('未找到明确注册的只读工具。')
            tool = matches[0]
            result = {'name': name, 'description': tool.description, 'input_schema': tool.parameters,
                      'orchestration_allowed': name in catalog.READ_TOOLS}
        encoded = raw_json({'complete': True, 'metadata': result, 'mes_write_performed': False})
        if len(encoded) > 80000:
            raise ValueError('元数据超过输出上限；请查询具体数据集或工具。')
        return encoded
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return raw_json({'complete': False, 'error': str(exc), 'mes_write_performed': False})


@mcp.tool
async def execute_camstar_read_plan(plan: read_plan.ReadPlan) -> str:
    """一次执行受控只读JSON计划，零内部模型回合，复用现有工具。先get_camstar_read_metadata查看schema。
    version=1，steps为带id/op的DAG：dataset结构化选列/eq/in/is_null/分页；
    call调用审核只读工具，foreach最多20项；select投影、group精确汇总、join等值关联。
    引用格式{"$ref":"orders#/data","column":"MfgOrderId"}；foreach参数{"$item":"/ContainerId"}。
    output指定返回引用。最多12步、4并发、500行、30工具调用、120秒。
    所有步骤结构先验证，动态引用参数在解析后再验证；上游失败跳过后续，上游不完整禁止汇总。
    500行限制针对结构化数据集和本地变换；原工具沿用原分页。不接受原始SQL/Python/URL/写入/预览。
    每步返回来源、完整性和耗时；生产结果不缓存、跨查询不保证事务快照。
    """
    try:
        return raw_json(await read_plan.run(plan))
    except Exception as exc:
        return raw_json({'complete': False, 'error': str(exc)[:2000], 'mes_write_performed': False,
                         'stage': 'plan_validation'})


@mcp.tool
async def run_camstar_read_template(template: Literal['order_dates'], parameters: OrderDatesParameters) -> str:
    """一次调用已验证的只读模板，不需要编写SQL或多轮探索。
    order_dates参数{"order_names":["MO-1","MO-2"],"timezone":"Asia/Shanghai"}，最多20工单。
    返回计划开工、MES生产开工/完成、全部当前关联容器数量、最早实际首箱开工（含关闭容器）、
    GMT原值与指定时区显示。完整无关联与空日期/查询失败区分，不用InstanceID推断创建先后。
    复用同一通用只读执行器；工单/容器当前关联口径，无写入，无生产结果缓存。
    """
    try:
        from tools.read_templates import order_dates
        if template != 'order_dates':
            raise ValueError('模板不存在。')
        if isinstance(parameters, OrderDatesParameters):
            parameters = parameters.model_dump()
        return raw_json(await order_dates(parameters))
    except Exception as exc:
        return raw_json({'complete': False, 'error': str(exc)[:2000], 'mes_write_performed': False})
