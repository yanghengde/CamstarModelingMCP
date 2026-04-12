"""
LLM 客户端 & 工具编排
========================
负责 LLM 初始化、MCP Tool 注册到 OpenAI schema、以及流式聊天处理。
"""

import json
from openai import AsyncOpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MAX_TOOL_LOOPS
from agent.memory import get_user_messages, save_memory
from tools import mcp, get_tool_func

oai_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

# 存储已注册的 OpenAI 格式工具列表
openai_tools: list[dict] = []


async def register_tools():
    """
    从 MCP Server 读取所有已注册的工具，转换为 OpenAI function calling 格式。
    应在 FastAPI lifespan 启动时调用。
    """
    global openai_tools
    openai_tools.clear()

    mcp_tools = await mcp.list_tools()
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters
            }
        })
    print(f"✅ 成功加载了 {len(openai_tools)} 个 Camstar MCP Tools。")
    return openai_tools


async def chat_stream(username: str, message: str):
    """
    SSE 流式聊天生成器。
    每执行一步工具调用都会向前端推送进度事件。
    """
    chat_messages = get_user_messages(username)
    chat_messages.append({"role": "user", "content": message})
    save_memory()

    loops = 0

    while True:
        loops += 1
        if loops > MAX_TOOL_LOOPS:
            reply = "⚠️ 遇到过多连续的操作，自动中断了当前任务。"
            chat_messages.append({"role": "assistant", "content": reply})
            save_memory()
            yield f"data: {json.dumps({'type': 'done', 'reply': reply}, ensure_ascii=False)}\n\n"
            break

        try:
            response = await oai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=chat_messages,
                tools=openai_tools,
                tool_choice="auto"
            )
        except Exception as e:
            reply = f"❌ 请求 LLM ({LLM_MODEL}) 失败: {e}"
            yield f"data: {json.dumps({'type': 'error', 'message': reply}, ensure_ascii=False)}\n\n"
            break

        response_message = response.choices[0].message

        # 序列化模型回复以存入记忆
        msg_dict = {"role": response_message.role}
        if response_message.content:
            msg_dict["content"] = response_message.content
        if response_message.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                } for tc in response_message.tool_calls
            ]
        chat_messages.append(msg_dict)
        save_memory()

        # 无工具调用 → 返回最终回答
        if not response_message.tool_calls:
            yield f"data: {json.dumps({'type': 'done', 'reply': response_message.content}, ensure_ascii=False)}\n\n"
            break

        # 依次执行工具调用
        for tool_call in response_message.tool_calls:
            func_name = tool_call.function.name
            func_args_str = tool_call.function.arguments

            print(f"⚡ 执行: {func_name}({func_args_str}) [@{username}]")
            # 向前端推送当前正在调用的工具名
            yield f"data: {json.dumps({'type': 'step', 'func': func_name, 'args': func_args_str}, ensure_ascii=False)}\n\n"

            try:
                func_args = json.loads(func_args_str)
            except json.JSONDecodeError:
                func_args = {}

            try:
                tool_func = get_tool_func(func_name)
                if tool_func is None:
                    result = f"Error: tool '{func_name}' not found"
                else:
                    result = await tool_func(**func_args)
            except Exception as e:
                result = f"Error executing {func_name}: {e}"

            chat_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": func_name,
                "content": str(result)
            })
            save_memory()
