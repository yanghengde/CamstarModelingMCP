# Modeling 工作台

工作台沿用 CamstarDesignerMCP 的浅色背景、青绿色强调、左侧导航和内容区布局。

- `/`：建模助手、最近对话、多行输入、Excel / CSV 附件、操作指南。
- `/records`：全部会话记录，点击后继续原会话。
- `/logs`：运行日志，保留原有日志数据与分析视图。

`static/workspace.css` 保存工作台样式，`static/workspace.js` 负责前端交互。
Markdown 在渲染前经过 DOMPurify 清理；外部脚本不可用时回退为纯文本。

## LangGraph 状态

继续使用现有 LangGraph 引擎和 SQLite 检查点。`/config` 返回执行引擎名称；
只读接口 `/workflow/{username}/{session_id}` 返回会话检查点的状态和待确认提示，
不会创建会话或执行工具。前端在加载会话、刷新页面和聊天结束时读取状态。

确认卡片通过原有 `/chat` 发送“确认执行”或“取消执行”，沿用现有确认策略和恢复逻辑。
正在生成回复或执行工具时，通过 SSE 更新状态栏。

检查点存在未完成节点时显示“上次流程尚未完成”，不据此宣称后台任务仍在执行。
停止按钮中断浏览器响应流，不表示回滚已经执行的 Camstar 操作。
本次未引入持久化后台任务调度或独立执行进度页面。

## 验证与启动

使用项目虚拟环境执行相关回归测试：

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_workflow_status.py tests/test_langgraph_checkpoint.py tests/test_langgraph_limits.py tests/test_agent_safety.py tests/test_chat_stream_errors.py tests/test_mcp_transport.py -q
node --check static/workspace.js
```

浏览器模拟验证覆盖桌面/手机布局、会话切换、操作记录、SSE 回复、附件上传、
待确认状态刷新恢复、确认与取消、日志页面。模拟验证不调用实际 Camstar 写入接口。

更新后重启现有 Web 服务并刷新浏览器，使新增静态资源和状态接口生效：

```powershell
.\venv\Scripts\python.exe main.py
```

请使用项目虚拟环境。检查点依赖要求见 `requirements.txt`；系统 Python 中不匹配的
`aiosqlite` 版本可能导致 `Connection.is_alive` 错误。
