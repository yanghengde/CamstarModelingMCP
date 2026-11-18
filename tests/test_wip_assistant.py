import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent.domains.wip import workflow
from agent.domains.wip import cases
from agent import experience, learning
from agent.langgraph_runtime import _build_graph


def test_demo_through_chat_graph(tmp_path):
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.db")) as saver:
            graph = _build_graph(saver)
            state = {"request_message": "在制品 演示", "username": "operator",
                     "request_id": "1", "messages": []}
            result = await graph.ainvoke(state, {"configurable": {"thread_id": "demo"}})
            assert "演示数据" in result["final_reply"]
            assert "DEMO-002" in result["messages"][-1]["content"]
    asyncio.run(scenario())
    assert not workflow.matches("查工单 MO1 进度")
    assert not workflow.matches("在制品 滞留时长如何计算？")


def test_handover_survives_session_and_replay(tmp_path):
    async def scenario():
        read = AsyncMock(return_value={"container_instance_id": "abcd0123456789ab"})
        async def run(message, actor, rid):
            return await workflow.run(message, actor, rid, query=read, db_path=tmp_path / "notes.db")
        for _ in range(2):
            assert "#1" in await run("在制品 登记 C1 等待质量确认", "day", "request1")
        notes = await run("在制品 交班", "night", "request2")
        assert notes.count("等待质量确认") == 1
        read.side_effect = ValueError("未取得匹配名称的实际Container引用")
        assert "查询未完成" in await run("在制品 登记 C2 测试", "day", "request3")
        assert "C2" not in await run("在制品 交班", "night", "request4")
    asyncio.run(scenario())


def test_order_results_are_page_scoped_and_keep_unknowns():
    read = AsyncMock(return_value={
        "containers": [{"name": "BOX", "current": {"OnHold": False, "Qty": 0}}],
        "page_step_distribution": [],
        "page": {"has_more": True, "next_offset": 20}})
    result = asyncio.run(workflow.run("在制品 工单 MO1", "day", "one", query=read))
    assert "本页 1 个" in result and "在制品 工单 MO1 20" in result
    assert "| 0 | 未知 | 未知 | 否 | 未知 |" in result
    read.assert_awaited_once_with("get_mfgorder_progress", mfgorder_name="MO1", offset=0,
                                  limit=20, include_history=False)


def test_legacy_handover_to_reviewed_experience(tmp_path, monkeypatch):
    monkeypatch.setattr(experience, "EXPERIENCE_DB", str(tmp_path / "knowledge.sqlite"))
    path = tmp_path / "handovers.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE handovers(id INTEGER PRIMARY KEY, container_id TEXT NOT NULL,
            name TEXT NOT NULL,note TEXT NOT NULL,author TEXT NOT NULL,created TEXT NOT NULL
            DEFAULT CURRENT_TIMESTAMP, request_id TEXT NOT NULL UNIQUE)""")
        db.execute("INSERT INTO handovers(container_id,name,note,author,request_id) VALUES(?,?,?,?,?)",
                   ("1234", "BOX-A", "老化测试疑似超时", "白班", "legacy"))
    def run(text, rid="read", actor="班组长"):
        assert workflow.matches("在制品 " + text)
        return asyncio.run(workflow.run("在制品 " + text, actor, rid, db_path=path))
    assert "没有未结" in run("待办")
    assert "待接单" in run("立单 #1", "open")
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda name: cases.handle(path, "接单 1 " + name, name, name),
                                ("甲", "乙")))
    assert sum("需要" in a for a in answers) == 1
    run("转交 1 夜班小周", "transfer")
    assert "夜班小周" in run("待办")
    run("跟进 1 查阅工艺卡 A", "update")
    assert "需要" in run("提炼 1 排队与加工分别核算", "early")
    assert "用法" in run("结案 1")
    run("结案 1 工艺卡 A 核实为正常", "close")
    assert "没有未结" in run("待办")
    assert "需要" in run("跟进 1 迟到更新", "late")
    first = run("提炼 1 排队与加工分别核算", "proposal")
    assert run("提炼 1 排队与加工分别核算", "proposal") == first
    assert run("详情 1").count("经验候选") == 1
    rows = learning.list_knowledge("candidate")
    assert len(rows) == 1 and "工艺卡 A" in rows[0]["evidence"]
    assert "分别核算" not in run("经验")
    learning.review_knowledge(rows[0]["id"], action="activate", evidence="工艺负责人复核 A 版")
    assert "分别核算" in run("经验")
    learning.review_knowledge(rows[0]["id"], action="retract", evidence="工艺已换版")
    assert "分别核算" not in run("经验")
