"""A bounded WIP workflow. Live reads use the same registered MES tools as chat."""
import asyncio
import json
import re
from pathlib import Path

from config import LANGGRAPH_CHECKPOINT_DB
from tools import get_tool_func
from .handover import handovers
from . import cases


PREFIX = re.compile(r"^(?:在制品助手|在制品|/wip)(?:[：:\s]+|$)", re.I)
HELP = """### 在制品助手

- `在制品 演示` 查看样例交班报告
- `在制品 异常演示` 查看白班立单、夜班核实结案的样例
- `在制品 工单 MO001` 查看关联容器、当前工序和异常标志
- `在制品 容器 BOX001` 核实单个容器，也可在名称后加容器实例 ID
- `在制品 登记 BOX001 等质量人员核实` 核实容器后，保存一条本服务共享的交班备注
- `在制品 交班` 查看最近的交班记录
- `在制品 立单 1` 将交班记录 #1 转为待接异常单，`在制品 待办` 查看未结事项
- `在制品 接单 1 白班小李` 指定负责人，`在制品 转交 1 夜班小王` 记录换班
- `在制品 跟进 1 已联系质量` 追加进展，`在制品 详情 1` 查看完整记录
- `在制品 结案 1 复核结果及凭据` 记录人工核实结果
- `在制品 提炼 1 可复用的判断方法` 提交经验候选，`在制品 经验` 查看已审核经验

真实查询请替换为现场工单号或容器号。"""


def matches(message):
    match = PREFIX.match(message.strip())
    if not match:
        return False
    tail = message.strip()[match.end():].strip()
    return (not tail or tail.split()[0] in cases.COMMANDS | {"助手", "帮助", "演示", "异常演示", "工单", "容器", "登记", "交班"}
            or message.strip().lower().startswith("/wip"))


def label(value):
    if isinstance(value, dict):
        value = value.get("Name")
    return "未知" if value is None or value == "" else str(value)


def flag(value):
    return "是" if value is True else "否" if value is False else "未知"


def cell(value):
    return label(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def report(title, items):
    lines = [f"### {title}", "", "| 容器 | 工序 | 数量 | 单位 | 状态 | 挂起 | 返工 |",
             "|---|---|---:|---|---|---|---|"]
    for item in items:
        current = item.get("current") or {}
        fields = [item["name"], current.get("Step"), current.get("Qty"), current.get("UOM"),
                  current.get("StatusName"), flag(current.get("OnHold")), flag(current.get("InRework"))]
        lines.append("| " + " | ".join(map(cell, fields)) + " |")
    failed = [item["name"] for item in items if item.get("error")]
    if failed:
        lines.extend(["", "状态查询失败，待核实：" + "、".join(failed)])
    return "\n".join(lines)


async def read_tool(name, **args):
    func = get_tool_func(name)
    if func is None:
        raise ValueError("当前部署未注册所需查询工具。")
    result = json.loads(await func(**args))
    if result.get("error"):
        raise ValueError(str(result["error"]))
    return result


async def run(message, username, request_id, *, query=read_tool, db_path=None):
    command = PREFIX.sub("", message.strip(), count=1).strip()
    parts = command.split()
    if parts == ["异常演示"]:
        return await asyncio.to_thread(cases.demo)
    if parts == ["演示"]:
        items = [
            {"name": "DEMO-001", "current": {"Step": "测试", "Qty": 12, "UOM": "件",
             "StatusName": "加工中", "OnHold": False, "InRework": False}},
            {"name": "DEMO-002", "current": {"Step": "复检", "Qty": 4, "UOM": "件",
             "StatusName": "待复检", "OnHold": True, "InRework": True}},
        ]
        return report("演示数据 · 工单 DEMO-MO", items) + "\n\nDEMO-002 同时标记为挂起和返工，交班时请核实挂起原因及责任人。\n\n" + HELP
    path = db_path or Path(LANGGRAPH_CHECKPOINT_DB).with_name("wip_handovers.sqlite")
    if parts == ["交班"]:
        return await asyncio.to_thread(handovers, path)
    if parts and parts[0] in cases.COMMANDS:
        return await asyncio.to_thread(cases.handle, path, command, username, request_id)
    try:
        if parts[:1] == ["工单"] and len(parts) in (2, 3):
            offset = int(parts[2]) if len(parts) == 3 else 0
            if offset < 0:
                return "工单查询偏移量须为非负整数。"
            data = await query("get_mfgorder_progress", mfgorder_name=parts[1], offset=offset,
                               limit=20, include_history=False)
            items = data["containers"]
            result = report(f"工单 {parts[1]} · 本页 {len(items)} 个关联容器", items)
            if not items:
                result += "\n\n本页未查到关联容器。"
            for row in data["page_step_distribution"]:
                result += f"\n\n{label(row['workflow'])} / {label(row['revision'])} / {label(row['step'])}：{row['container_count']} 个容器"
            if data["page"]["has_more"]:
                result += f"\n\n下一页：`在制品 工单 {parts[1]} {data['page']['next_offset']}`"
            return result
        register = parts[:1] == ["登记"] and len(parts) >= 3
        if register or (parts[:1] == ["容器"] and len(parts) in (2, 3)):
            args = {"container_name": parts[1]}
            if not register and len(parts) == 3:
                args["container_instance_id"] = parts[2]
            data = await query("get_container_details", **args)
            cid = data["container_instance_id"]
            if not cid:
                return "未核实容器实例，请检查容器名称。"
            if register:
                note = command.split(maxsplit=2)[2]
                return await asyncio.to_thread(handovers, path, cid, parts[1], note, username, request_id)
            return report(f"容器 {parts[1]} · {cid}", [
                {"name": parts[1], "current": data["data"]["ContainerStatusDetails"]}])
    except (KeyError, TypeError, ValueError) as exc:
        return f"在制品查询未完成，请核实输入或数据来源。\n\n{exc}"
    return HELP
