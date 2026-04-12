import asyncio
import os
import sys
import json
from dotenv import load_dotenv

from openai import AsyncOpenAI
from dotenv import load_dotenv
load_dotenv()

import server

# 确保控制台输出 UTF-8，防止乱码
sys.stdout.reconfigure(encoding='utf-8')

# DeepSeek 配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-e258e65ced244d40a2de433fb667dfab")
# 官方兼容端点
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

async def main():
    print("🤖 正在初始化 DeepSeek MCP Agent...")
    
    # 确保 Camstar 所需的配置能从环境变量读取
    # 如果 .env 中缺少，提醒用户
    if not os.getenv("CAMSTAR_BASE_URL"):
        print("⚠️ 未找到 CAMSTAR_BASE_URL，请确保 .env 文件配置正确")
        return

    client = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )

    # 1. 提取 MCP Tools 到 OpenAI Schema
    mcp_tools = await server.mcp.list_tools()
    openai_tools = []
    
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters
            }
        })
        
    print(f"✅ 成功加载了 {len(openai_tools)} 个 Camstar 建模能力 (MCP Tools)。\n")
    print("="*60)
    print("🗣️ 欢迎使用 Camstar AI 助手！你可以问我类似于：")
    print(" - '当前系统里有多少个 Spec？'")
    print(" - '帮我查一下名叫 Incoming Inspection 的 Spec'")
    print(" - '创建一个叫 TEST-001，版本号为1的Spec'")
    print("输入 'q' 或 'exit' 退出。")
    print("="*60)

    messages = [
        {"role": "system", "content": "你是 Camstar MES Modeling 的超级助手。你可以通过调用提供的 MCP (Model Context Protocol) 工具来查询和管理 Spec。请优先用中文友好地回答用户的提问。如果API调用出错，请将错误信息告诉用户。"}
    ]

    while True:
        try:
            user_input = input("\n🧑 你: ")
        except (KeyboardInterrupt, EOFError):
            break
            
        if user_input.strip().lower() in ['q', 'exit', 'quit']:
            break
            
        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})

        # 循环，直到大模型不再调用工具为止
        while True:
            try:
                response = await client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto"
                )
            except Exception as e:
                print(f"❌ 请求 DeepSeek 失败: {e}")
                break

            response_message = response.choices[0].message
            messages.append(response_message) # 记录模型原始回复（含 function_call 信息）

            # 如果模型没有调用工具，直接打印回复并跳出执行循环
            if not response_message.tool_calls:
                print(f"\n🤖 助手: {response_message.content}")
                break

            # 否则，模型要求调用工具
            for tool_call in response_message.tool_calls:
                func_name = tool_call.function.name
                func_args_str = tool_call.function.arguments
                
                print(f"\n⚡ 正在执行操作: {func_name}({func_args_str}) ...")
                
                # 解析参数
                try:
                    func_args = json.loads(func_args_str)
                except json.JSONDecodeError:
                    func_args = {}

                # 实际调用 server.py 中的函数
                try:
                    tool_func = getattr(server, func_name)
                    # 动态执行带有 kwargs 的 async 装饰器函数
                    result = await tool_func(**func_args)
                except Exception as e:
                    result = f"Error executing {func_name}: {e}"

                print("✔️ 执行完毕。正在分析结果...\n")

                # 把结果传回给大模型
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": func_name,
                    "content": str(result)
                })

if __name__ == "__main__":
    asyncio.run(main())
