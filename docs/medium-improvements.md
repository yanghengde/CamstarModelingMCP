# 中等优先级优化

- LangGraph 确认绑定当前工具批次的名称、顺序和规范化参数摘要；新一轮模型计划清除批准标记，修改参数需要重新确认。确认窗口展示工具及参数。这里是执行计划预览，不是 Excel 每行数据或数据库前后值差异；超长参数有明确截断提示。
- legacy 不再用一句“确认”绕过高影响操作门槛，需要确认的操作提示转到 LangGraph。普通阈值内操作保持现有策略。
- patch_mfgorder、patch_mfgline、patch_producttype 保留注册兼容性，但函数直接返回禁用错误，不发送 HTTP。外部 MCP 同样生效。应使用已验证的 update 工具。
- 两条推理路径记录 first_delta_seconds 和供应商 usage，含无 choices 的最终 usage chunk。缺失用量记为 null，不能解释为零成本。设置 LLM_STREAM_USAGE=true 才请求流式用量，默认不向未验证兼容性的供应商发送额外参数。
- 尚未实测模型供应商 token/缓存数据或比较实际性能收益；未声称已经降低费用或耗时。

验证：29 项针对性测试通过。未执行生产写操作。重启服务后生效。
