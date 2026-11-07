"""
FastAPI 应用工厂
==================
创建并配置 FastAPI 应用实例。
"""

import os
import asyncio
from contextlib import asynccontextmanager,suppress
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import (
    AGENT_ENGINE,
    CAMSTAR_BASE_URL,
    ENABLE_MCP_HTTP,
    MCP_ALLOWED_HOSTS,
    MCP_ALLOWED_ORIGINS,
    MCP_API_KEY,
    MCP_HTTP_PATH,
)
from agent.memory import init_memory
from agent.llm_client import oai_client, openai_tools, register_tools
from tools import mcp
from web.routes import router
from web.workbench import router as workbench_router
from web.mcp_transport import BearerTokenMiddleware
from core.quality_signature_vault import VAULT
from core import quality_signature_ui
from web.chat_runs import RUNS
from web.upload_limits import UploadLimitMiddleware
from core.retention import maintenance_loop


async def expire_signature_inputs():
    while True:
        await asyncio.sleep(5)
        VAULT.expire()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载记忆和注册工具。"""
    if not CAMSTAR_BASE_URL or CAMSTAR_BASE_URL == "http://localhost/Modeling":
        print("[WARN] 未找到有效的 CAMSTAR_BASE_URL，请检查 .env 配置。")

    # 恢复历史记忆
    await asyncio.to_thread(init_memory)
    await asyncio.to_thread(RUNS.initialize)

    # 注册 MCP 工具
    await register_tools()

    if AGENT_ENGINE == "langgraph":
        from agent.langgraph_runtime import init_langgraph_runtime

        await init_langgraph_runtime(oai_client, openai_tools)

    print("[READY] Web 服务启动就绪！")
    quality_signature_ui.activate()
    signature_reaper=asyncio.create_task(expire_signature_inputs())
    maintenance = asyncio.create_task(maintenance_loop(RUNS))
    mcp_http_app = getattr(app.state, "mcp_http_app", None)
    try:
        # Mounted Starlette applications do not run their lifespan implicitly.
        # FastMCP's session/task manager must therefore share the parent lifespan.
        if mcp_http_app is not None:
            async with mcp_http_app.router.lifespan_context(mcp_http_app):
                yield
        else:
            yield
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await RUNS.shutdown()
        signature_reaper.cancel()
        with suppress(asyncio.CancelledError):await signature_reaper
        await quality_signature_ui.close()
        from core.signature_channel import close_client as close_signature_client
        await close_signature_client()
        if AGENT_ENGINE == "langgraph":
            from agent.langgraph_runtime import close_langgraph_runtime

            await close_langgraph_runtime()
        # 应用关闭时，清理 HTTP 连接池，释放网络端口
        from core.http_client import close_client

        await close_client()



def create_app() -> FastAPI:
    """创建并返回配置完毕的 FastAPI 实例。"""
    app = FastAPI(
        title="Siemens Opcenter Modeling AI Agent",
        lifespan=lifespan
    )
    app.add_middleware(UploadLimitMiddleware)

    # 挂载静态资源
    svg_dir = os.path.join("static", "svg")
    if os.path.isdir(svg_dir):
        app.mount("/svg", StaticFiles(directory=svg_dir), name="svg")

    # 注册路由
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(router)
    app.include_router(workbench_router)

    if ENABLE_MCP_HTTP:
        if not MCP_API_KEY:
            raise RuntimeError(
                "ENABLE_MCP_HTTP=True requires MCP_API_KEY. "
                "Refusing to expose Camstar write tools without authentication."
            )

        mcp_http_app = mcp.http_app(
            path="/",
            transport="streamable-http",
            stateless_http=True,
            json_response=True,
            allowed_hosts=MCP_ALLOWED_HOSTS,
            allowed_origins=MCP_ALLOWED_ORIGINS or None,
        )
        app.state.mcp_http_app = mcp_http_app
        app.mount(
            MCP_HTTP_PATH,
            BearerTokenMiddleware(mcp_http_app, MCP_API_KEY),
            name="mcp",
        )
        print(f"[MCP] Streamable HTTP endpoint: {MCP_HTTP_PATH}/")
    else:
        app.state.mcp_http_app = None
        print("[MCP] 外部 Streamable HTTP 未启用（ENABLE_MCP_HTTP=False）。")

    return app
