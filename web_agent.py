import asyncio
import os
import sys
import json
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel
from openai import AsyncOpenAI

# 确保在导入 server 之前加载环境变量
from dotenv import load_dotenv
load_dotenv()

import server

# 确保控制台输出 UTF-8
sys.stdout.reconfigure(encoding='utf-8')

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-e258e65ced244d40a2de433fb667dfab")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

openai_tools = []
oai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

MEMORY_FILE = "memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_memory(mem_dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem_dict, f, ensure_ascii=False, indent=2)

user_memories = load_memory()

SYSTEM_PROMPT = """你是 Siemens Opcenter (Camstar) MES Modeling 的超级助手，专业且熟练使用提供的 MCP 工具。
请务必严格遵循以下核心原则：
1. **严格参数审查与拒答机制**：在调用任何 MCP API 之前，请核对该操作所需的核心必填参数（如 Name, Revision, Operation等）。如果用户的指令缺乏完成工具调用的必要参数，**绝对不要凭空猜测或虚构参数值，也不要自己填入临时值测试**。
2. **互动追问**：当参数信息不充分时，请**直接放弃本次工具调用**，并在回复中用友好的语气向用户指出缺少哪些具体字段，等待用户补充。例如："您想要创建 Spec，但是缺失了 Revision 字段，请问具体的值是多少？"
3. **记忆连贯性**：由于我们有持久历史记录，在用户补充缺失信息后，你需要结合上文自动拼接出完整的数据，然后再执行正确的工具调用。
4. **格式与态度**：使用 Markdown 丰富排版，回答专业简练，切忌啰嗦。"""

def get_user_messages(username: str):
    if username not in user_memories:
        user_memories[username] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    else:
        # 强制更新老用户的系统提示词，以便新策略立即生效
        if user_memories[username] and user_memories[username][0].get("role") == "system":
            user_memories[username][0]["content"] = SYSTEM_PROMPT
            
    return user_memories[username]

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
app.mount("/svg", StaticFiles(directory="svg"), name="svg")

class ChatRequest(BaseModel):
    message: str
    username: str

@app.get("/config")
def config_endpoint():
    return {"username": os.getenv("CAMSTAR_USERNAME", "CamstarAdmin")}

@app.get("/history/{username}")
def history_endpoint(username: str):
    return {"messages": get_user_messages(username)}

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    async def event_generator():
        user_input = req.message
        username = req.username
        
        chat_messages = get_user_messages(username)
        chat_messages.append({"role": "user", "content": user_input})
        save_memory(user_memories)
        
        max_loops = 15
        loops = 0

        while True:
            loops += 1
            if loops > max_loops:
                reply = "⚠️ 遇到过多连续的操作，自动中断了当前任务。"
                chat_messages.append({"role": "assistant", "content": reply})
                save_memory(user_memories)
                yield f"data: {json.dumps({'type': 'done', 'reply': reply}, ensure_ascii=False)}\n\n"
                break

            try:
                response = await oai_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=chat_messages,
                    tools=openai_tools,
                    tool_choice="auto"
                )
            except Exception as e:
                reply = f"❌ 请求 DeepSeek 失败: {e}"
                yield f"data: {json.dumps({'type': 'error', 'message': reply}, ensure_ascii=False)}\n\n"
                break

            response_message = response.choices[0].message
            
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
            save_memory(user_memories)

            if not response_message.tool_calls:
                yield f"data: {json.dumps({'type': 'done', 'reply': response_message.content}, ensure_ascii=False)}\n\n"
                break

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
                save_memory(user_memories)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    print("启动服务器于 http://127.0.0.1:8000")
    uvicorn.run("web_agent:app", host="127.0.0.1", port=8000, reload=False)
