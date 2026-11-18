"""An exception case keeps the ID and verified container of its original shift note."""
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import learning
from .handover import database, handovers


# Status and accepted prior status for each business action.
TRANSITIONS = {
    "立单": ("交班备注", "待接单"),
    "接单": ("待接单", "处理中"),
    "转交": ("处理中", "处理中"),
    "跟进": ("处理中", "处理中"),
    "结案": ("处理中", "已结案"),
    "提炼": ("已结案", "已结案"),
}
SYNTAX = {
    "立单": "立单 单号", "接单": "接单 单号 负责人",
    "转交": "转交 单号 新负责人", "跟进": "跟进 单号 进展说明",
    "结案": "结案 单号 核实结果或凭据", "提炼": "提炼 单号 建议复用的经验",
    "详情": "详情 单号",
}
COMMANDS = {*SYNTAX, "待办", "经验"}


def pending(db, before):
    rows = db.execute("""SELECT h.id,h.name,h.note,e.status,e.assignee FROM handovers h
        JOIN wip_case_events e ON e.id=(SELECT MAX(id) FROM wip_case_events WHERE case_id=h.id)
        WHERE e.status IN ('待接单','处理中') AND h.id<? ORDER BY h.id DESC LIMIT 21""",
        (before,)).fetchall()
    lines = ["### 在制品异常待办"]
    for row in rows[:20]:
        lines.append(f"**#{row['id']} · {row['name']}**　{row['status']} · {row['assignee'] or '待认领'}"
                     f"\n\n{row['note']}")
    if len(rows) > 20:
        lines.append(f"下一页：`在制品 待办 {rows[19]['id']}`")
    if not rows:
        lines.append("当前范围内没有未结异常单。")
    return "\n\n".join(lines)


def case_summary(number, event):
    return f"异常单 #{number} · {event['status']} · {event['assignee'] or '待认领'}"


def positive_id(value):
    if value.startswith("#"):
        value = value[1:]
    if value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        if 0 < number <= 2**63 - 1:
            return number
    return None


def handle(path, command, actor, request_id):
    parts = command.split(maxsplit=2)
    action = parts[0]
    if action == "经验":
        if len(parts) != 1:
            return "用法：在制品 经验"
        rows = learning.retrieve("在制品", owner=actor, limit=10)
        return "### 已审核的在制品经验\n\n" + ("\n\n".join(
            f"**经验 #{r['id']}** · {r['content']}\n\n审核依据：{r['evidence']}"
            for r in rows) or "当前没有适用于本环境的已审核经验。")
    number = positive_id(parts[1]) if len(parts) >= 2 else None
    if action == "待办":
        if len(parts) > 2 or (len(parts) == 2 and number is None):
            return "用法：在制品 待办 [上一页给出的单号]"
        with database(path) as db:
            return pending(db, number or 2**63 - 1)
    detail = parts[2].strip() if len(parts) == 3 else ""
    if number is None or bool(detail) != (action not in {"立单", "详情"}):
        return f"用法：在制品 {SYNTAX[action]}"
    with database(path) as db:
        note = db.execute("SELECT * FROM handovers WHERE id=?", (number,)).fetchone()
        if note is None:
            return f"未找到交班记录 #{number}。请先用「在制品 交班」核对单号。"
        events = db.execute("SELECT * FROM wip_case_events WHERE case_id=? ORDER BY id",
                            (number,)).fetchall()
        previous = events[-1] if events else {"status": "交班备注", "assignee": ""}
        if action == "详情":
            lines = [case_summary(number, previous), f"容器 {note['name']} · {note['container_id']}",
                     f"{note['created']} UTC · {note['author']} 登记：{note['note']}"]
            for event in events:
                lines.append(f"{event['created']} UTC · {event['actor']} {event['action']}　{event['detail']}")
            return "\n\n".join(lines)
        replay = db.execute("SELECT * FROM wip_case_events WHERE request_id=?", (request_id,)).fetchone()
        if replay:
            return case_summary(replay['case_id'], replay) + "\n\n" + replay['detail']
        required, status = TRANSITIONS[action]
        if previous["status"] != required:
            return f"#{number} 当前为「{previous['status']}」；{action}需要「{required}」状态。"
        assignee = detail if action in {"接单", "转交"} else previous["assignee"]
        if action == "立单":
            detail = f"来源：交班记录 #{number}"
        if action == "提炼":
            closure = next(e for e in events if e["action"] == "结案")
            evidence = f"异常单 #{number}，容器 {note['name']}（{note['container_id']}）。结案记录：{closure['detail']}"
            key = learning.propose_knowledge(content=detail, topics=["wip"], evidence=evidence,
                source=f"wip_case:{note['request_id']}", knowledge_key=f"wip.case.{note['request_id']}")
            detail = f"经验候选 #{key} · {detail}。审核状态可在学习管理工具中查看。"
        db.execute("""INSERT INTO wip_case_events
            (case_id, action, status, assignee, actor, detail, request_id)
            VALUES (?,?,?,?,?,?,?)""", (number, action, status, assignee, actor, detail, request_id))
        return case_summary(number, {"status": status, "assignee": assignee}) + "\n\n" + detail


def demo():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "demo.sqlite"
        handovers(path, "DEMO-REF", "DEMO-002", "复检批次待核实", "白班", "demo-note")
        for actor, command in [
            ("白班", "立单 1"), ("白班", "接单 1 白班小李"), ("白班", "跟进 1 已请质量复核"),
            ("白班", "转交 1 夜班小王"), ("夜班", "结案 1 已核对演示检验记录 DEMO-Q1"),
        ]:
            handle(path, command, actor, command)
        return "### 异常单交接演示（样例数据）\n\n" + handle(path, "详情 1", "夜班", "")
