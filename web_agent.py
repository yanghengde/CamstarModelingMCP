import asyncio
import os
import sys
import json
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn
from pydantic import BaseModel
from openai import AsyncOpenAI

from dotenv import load_dotenv
load_dotenv()

import server

# 确保控制台输出 UTF-8
sys.stdout.reconfigure(encoding='utf-8')

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-e258e65ced244d40a2de433fb667dfab")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

openai_tools = []
# 初始化一个简单的上下文给单用户测试用
chat_messages = [
    {"role": "system", "content": "你是 Camstar MES Modeling 的超级助手，懂技术并且熟练使用提供的 MCP 工具。你是 Siemens 产品的专家。回答请专业、简洁、友好，并使用 Markdown 进行排版。遇到错误时请明确告知原因。"}
]

oai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_tools
    if not os.getenv("CAMSTAR_BASE_URL"):
        print("⚠️ 警告: 未找到 CAMSTAR_BASE_URL！")
    
    mcp_tools = await server.mcp.list_tools()
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters
            }
        })
    print(f"✅ 成功加载了 {len(openai_tools)} 个 Camstar MCP Tools。Web 服务启动！")
    yield

app = FastAPI(lifespan=lifespan)

class ChatRequest(BaseModel):
    message: str

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    user_input = req.message
    chat_messages.append({"role": "user", "content": user_input})
    
    # 循环：让模型能够连续调用工具，最多循环 15 次防死循环
    max_loops = 15
    loops = 0

    while True:
        loops += 1
        if loops > max_loops:
            return {"reply": "⚠️ 遇到过多连续的操作，自动中断了当前任务。"}

        try:
            response = await oai_client.chat.completions.create(
                model="deepseek-chat",
                messages=chat_messages,
                tools=openai_tools,
                tool_choice="auto"
            )
        except Exception as e:
            return {"reply": f"❌ 请求 DeepSeek 失败: {e}"}

        response_message = response.choices[0].message
        
        # 为了能够在后续传给 DeepSeek，必须先处理 response_message
        # OpenAI 的 message 转换为 dict 时移除 None 以符合 Schema
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

        if not response_message.tool_calls:
            # 模型没有工具调用请求了，直接返回给前端
            return {"reply": response_message.content}

        # 处理工具调用
        for tool_call in response_message.tool_calls:
            func_name = tool_call.function.name
            func_args_str = tool_call.function.arguments
            
            print(f"⚡ 执行: {func_name}({func_args_str})")
            try:
                func_args = json.loads(func_args_str)
            except json.JSONDecodeError:
                func_args = {}

            try:
                tool_func = getattr(server, func_name)
                result = await tool_func(**func_args)
            except Exception as e:
                result = f"Error executing {func_name}: {e}"

            chat_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": func_name,
                "content": str(result)
            })

@app.get("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    print("启动服务器于 http://127.0.0.1:8000")
    uvicorn.run("web_agent:app", host="127.0.0.1", port=8000, reload=False)
