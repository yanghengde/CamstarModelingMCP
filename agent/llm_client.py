"""
LLM 客户端 & 工具编排
========================
负责 LLM 初始化、MCP Tool 注册到 OpenAI schema、以及流式聊天处理。
"""

import json
import asyncio
import time
from openai import AsyncOpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MAX_TOOL_LOOPS
from agent.memory import get_user_messages, save_memory, update_session_title, user_memories
from tools import mcp, get_tool_func
from core.perf_logger import record_perf

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

async def generate_title(message: str) -> str:
    """生成不超过30字的短标题"""
    try:
        resp = await oai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个标题生成助手。请根据用户的第一句话提炼总结一个极短的标题（最多30个字符，只输出标题内容，不要加引号、句号等标点符号）。"},
                {"role": "user", "content": message}
            ],
            max_tokens=20,
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return "新对话"


async def chat_stream(username: str, message: str, session_id: str = None):
    """
    SSE 流式聊天生成器。
    每执行一步工具调用都会向前端推送进度事件。
    """
    chat_messages = get_user_messages(username, session_id)
    
    # 尝试恢复 session_id，保证日志准确性和更新标题
    actual_session_id = session_id
    if not actual_session_id:
        actual_session_id = user_memories.get(username, {}).get("active_session", "unknown")
        
    is_first_message = (len(chat_messages) == 1) # 只有 system prompt
        
    chat_messages.append({"role": "user", "content": message})
    save_memory()

    if is_first_message and actual_session_id != "unknown":
        new_title = await generate_title(message)
        update_session_title(username, actual_session_id, new_title)
        yield f"data: {json.dumps({'type': 'title_update', 'title': new_title, 'session_id': actual_session_id}, ensure_ascii=False)}\n\n"


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
            llm_start_time = time.time()
            response_stream = await oai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=chat_messages,
                tools=openai_tools,
                tool_choice="auto",
                stream=True
            )
            
            tool_calls_dict = {}
            full_content = ""
            has_started_stream = False
            
            async for chunk in response_stream:
                if len(chunk.choices) == 0:
                    continue
                delta = chunk.choices[0].delta
                
                # 推送流式文本
                if delta.content:
                    if not has_started_stream:
                        yield f"data: {json.dumps({'type': 'stream_start'}, ensure_ascii=False)}\n\n"
                        has_started_stream = True
                    full_content += delta.content
                    # Streaming per chunk
                    yield f"data: {json.dumps({'type': 'stream_chunk', 'content': delta.content}, ensure_ascii=False)}\n\n"
                
                # 拼接工具调用
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name or "",
                                    "arguments": tc.function.arguments or ""
                                }
                            }
                        else:
                            if tc.function.name:
                                tool_calls_dict[idx]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

            llm_duration = time.time() - llm_start_time
            record_perf("LLM_Inference", llm_duration, username, actual_session_id, {"model": LLM_MODEL, "prompt": message, "streamed": True})
        except asyncio.CancelledError:
            print(f"⚠️ [中止] 用户取消了请求 [@{username}]")
            raise
        except Exception as e:
            reply = f"❌ 请求 LLM ({LLM_MODEL}) 失败: {e}"
            yield f"data: {json.dumps({'type': 'error', 'message': reply}, ensure_ascii=False)}\n\n"
            break

        # 整理出 response 结构
        response_tool_calls = [tool_calls_dict[k] for k in sorted(tool_calls_dict.keys())]

        # 序列化模型回复以存入记忆
        msg_dict = {"role": "assistant"}
        if full_content:
            msg_dict["content"] = full_content
        if response_tool_calls:
            msg_dict["tool_calls"] = response_tool_calls
            
        chat_messages.append(msg_dict)
        save_memory()

        # 无工具调用 → 返回最终回答
        if not response_tool_calls:
            if has_started_stream:
                # 已经推过流，则用 stream_end
                yield f"data: {json.dumps({'type': 'stream_end'}, ensure_ascii=False)}\n\n"
            else:
                # 从没推过流，用原来的 done 兜底
                yield f"data: {json.dumps({'type': 'done', 'reply': full_content}, ensure_ascii=False)}\n\n"
            break

        # 依次执行工具调用
        for tool_call in response_tool_calls:
            func_name = tool_call["function"]["name"]
            func_args_str = tool_call["function"]["arguments"]

            print(f"⚡ 执行: {func_name}({func_args_str}) [@{username}]")
            # 向前端推送当前正在调用的工具名
            yield f"data: {json.dumps({'type': 'step', 'func': func_name, 'args': func_args_str}, ensure_ascii=False)}\n\n"

            try:
                func_args = json.loads(func_args_str)
            except json.JSONDecodeError:
                func_args = {}

            tool_start_time = time.time()
            try:
                tool_func = get_tool_func(func_name)
                if tool_func is None:
                    result = f"Error: tool '{func_name}' not found"
                else:
                    result = await tool_func(**func_args)
            except asyncio.CancelledError:
                print(f"⚠️ [中止] 用户在执行工具 {func_name} 时取消了请求")
                chat_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": func_name,
                    "content": "Action aborted by user/system."
                })
                save_memory()
                raise
            except Exception as e:
                result = f"Error executing {func_name}: {e}"
            
            tool_duration = time.time() - tool_start_time
            record_perf(f"Tool_Execute", tool_duration, username, actual_session_id, {"tool": func_name, "prompt": message})

            chat_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": func_name,
                "content": str(result)
            })
            save_memory()

