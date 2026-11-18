import asyncio
from unittest.mock import AsyncMock

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent.domains.wip import workflow
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
