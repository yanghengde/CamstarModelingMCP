"""Reviewable issue intent; not an executable Shopfloor payload."""
import math
from typing import Literal
from tools import mcp
from tools.boms import encode
from tools.process_context import ReadContext
from tools.resources import reference
from tools.container_inquiry import get_container_details
import json


@mcp.tool
async def preview_component_issue(container_name: str, product_name: str, product_revision: str,
                                   qty: float, issue_control: Literal[1, 2, 3, 4, 5],
                                   source_name: str = '', container_instance_id: str | None = None) -> str:
    """仅预览单条投料意图，不执行、不生成可提交payload。模式1/2来源容器，3批号，4库存点，5数量；数量正数、产品明确版本。LoadRequirements/单位/现场状态尚未验证，executable始终false。"""
    out = {'executable': False, 'complete': False, 'stage': 'intent_preview',
           'blockers': ['尚未验证 LoadRequirements 需求加载契约与 BOM 行定位。',
                        '尚未核对单位、允许投料数量、工序及当前现场状态。',
                        '尚未验证事务成功回读与重复提交核实路径。'],
           'note': '本预览不执行投料，不证明物料满足需求，不是可提交的 Shopfloor 请求。'}
    try:
        if (not container_name.strip() or not product_name.strip() or not product_revision.strip()
                or isinstance(qty, bool) or not math.isfinite(qty) or qty <= 0
                or type(issue_control) is not int or issue_control not in (1, 2, 3, 4, 5)):
            raise ValueError('名称/产品版本必填，数量须为有限正数，issue_control=1..5。')
        if issue_control != 5 and not source_name.strip():
            raise ValueError('此追踪模式必须明确来源容器、批号或库存点。')
        if issue_control == 5 and source_name.strip():
            raise ValueError('仅数量模式不接受来源，避免来源被忽略。')
        if container_instance_id is not None and not container_instance_id.strip():
            raise ValueError('容器 ID 不能为空字符串。')
        out['intent'] = {'target': container_name.strip(), 'product': product_name.strip(),
                         'revision': product_revision.strip(), 'quantity_to_issue': qty,
                         'issue_control': issue_control, 'source': source_name.strip() or None,
                         'source_kind': {1: 'serial_container', 2: 'bulk_container', 3: 'lot', 4: 'stock_point', 5: 'quantity_only'}[issue_control]}
        product = await ReadContext(max_requests=2).resolve('Products', reference(product_name, None, product_revision))
        out['resolved_product'] = {k: product.get(k) for k in ['InstanceID', 'Name', 'Revision']}
        info = json.loads(await get_container_details(container_name, container_instance_id, view='info'))
        ref = info.get('data', {}).get('Container')
        if info.get('error') or not isinstance(ref, dict) or ref.get('Name') != container_name.strip():
            raise ValueError('目标容器身份无法核对。')
        identifier = ref.get('InstanceId') or ref.get('InstanceID')
        if not identifier or (container_instance_id and identifier != container_instance_id.strip()):
            raise ValueError('目标容器 ID 无法核对。')
        out['resolved_target'] = ref
        out['target_query_complete'] = info.get('complete') is True
        out['blockers'].append('来源仅记录为用户意图，尚未核对其存在性、余额及与需求的匹配。')
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)
