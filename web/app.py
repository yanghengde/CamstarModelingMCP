"""
FastAPI 应用工厂
==================
创建并配置 FastAPI 应用实例。
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import CAMSTAR_BASE_URL
from agent.memory import init_memory
from agent.llm_client import register_tools
from web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载记忆和注册工具。"""
    if not CAMSTAR_BASE_URL or CAMSTAR_BASE_URL == "http://localhost/Modeling":
        print("⚠️ 警告: 未找到有效的 CAMSTAR_BASE_URL！请检查 .env 配置。")

    # 恢复历史记忆
    init_memory()

    # 注册 MCP 工具
    await register_tools()

    print("🚀 Web 服务启动就绪！")
    yield


def create_app() -> FastAPI:
    """创建并返回配置完毕的 FastAPI 实例。"""
    app = FastAPI(
        title="Siemens Opcenter Modeling AI Agent",
        lifespan=lifespan
    )

    # 挂载静态资源
    svg_dir = os.path.join("static", "svg")
    if os.path.isdir(svg_dir):
        app.mount("/svg", StaticFiles(directory=svg_dir), name="svg")

    # 注册路由
    app.include_router(router)

    return app
