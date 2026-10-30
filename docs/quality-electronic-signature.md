# 单人质量电子签名：C2.B第一闭环

更新日期：2026-10-30。代码及离线验证完成，真实MES未验收。此页只覆盖直接Event/CAPA批准或拒绝时的原生单人 `QualityESigDetail`；该审批入口仍阻断通用多签/协签。后续已补齐[NCR更新通用签名条件接入](quality-native-multisignatures.md)，但原REST契约及协签参数传递仍有阻塞，不能认定现场可用，C2整体尚未完成。

## 入口与出口

入口是已有、已提交、无流程/事件数据/检查表的受支持Event/CAPA，审批表及所选条目已Routed，当前原生Employee/EntryForApproval、Approve512、原组织及决策均核实。原指定审批人，或原Role2且明确允许角色替代的实际当前员工，可使用本链路；不能指定另一员工代签。

组织必须要求SignApproval事务7839质量电子签名。默认 `require_quality_esign=false` 仍拒绝该要求；明确选择true才加载原生 `LoadESigDetails` 并核对UserRequired及实际员工。不得通过修改组织策略、把其他签名类型改称质量签名或普通确认绕过验证。执行出口是一次原生SignApproval尝试和后续只读证据；实际结果仍按unknown管理，由最终事务证据人工审计。

## 用户问法与执行顺序

1. “查看事件X在组织Y的审批情况。” → `get_quality_approval_context`，读取真实对象、表、条目、允许决策和组织要求，不挑选猜测ID。
2. “批准这个条目，说明为……，按要求电子签名。” → `prepare_quality_approval_decision(action='approve', …, require_quality_esign=true)`。拒绝用reject和原列表对应拒绝决策；通知或角色替代仍分别明确选择其原有参数。
3. 展示计划及 `signature_input_url`。用户在独立本机页面核对对象、原周期、决策、说明和实际员工，勾选一次提交授权后输入口令。**聊天、模型、MCP参数均不收口令，也不让代理代填签名页面。**
4. `get_quality_transaction_plan` 仅核对 `signature_input_status`。captured表示短期暂存，未验证口令、未审批。用户确认同一计划后，通过既有系统确认执行 `execute_quality_transaction(plan_id,plan_digest)`。
5. 执行先原子领取并消耗输入，再重读契约、周期、对象、员工、组织要求及权限，核对原计划并单次提交。任何领取后的核验失败也不能复用输入。取消、过期或服务重启后须重新准备计划及输入。
6. `inspect_quality_transaction` 获取原条目/周期历史和QualityESigConfirmed候选。仅当签批历史精确关联HistoryMainlineId时才展示该标记，否则为未知；不自动归因请求成功、不解占用。unknown须沿既有最终人工审计恢复，不能新建计划重试。

页面“取消签名输入”只销毁暂存口令，不取消已路由审批、原计划或已领取的事务。输错口令导致HTTP400仍是已尝试事务，不能自动再试；先核查原生最终记录和人工审计。

## 输入保护和运行条件

- Web主进程按需启动独立的127.0.0.1动态端口，页面与聊天来源隔离；私有服务只暴露固定页面、两个静态资源和采集/取消接口，无聊天、代理或OpenAPI入口。仅回环直连、Host/Origin校验、浏览器绑定、CSRF、防嵌入、禁止缓存/来源转发；关闭访问日志。
- URL片段只有随机挑战值，加载后从地址栏移除；不存在口令读取接口。临时输入仅在同一进程内存，绑定plan_id及摘要，不写SQLite、计划、模型、工具结果、文件或口令摘要。公开状态及capture_reference不能用于读取口令。
- 挑战最多300秒；采集后最多120秒且不超过原截止时间；最多100个活跃请求。每5秒清理过期缓冲，浏览器到期/离页清空输入框。执行领取是一次性的，并发领取最多一个成功；使用、撤销、过期和服务关闭清除可变缓冲。
- Python/浏览器/HTTP库会产生短命字符串或编码副本；此实现尽量缩短其生命周期，不能承诺运行时内存取证无残留。不得启用额外请求体抓取、调试追踪或内存转储来记录生产口令。
- 签名链路全部Modeling、Query及Shopfloor请求使用独立、验证证书和主机名的HTTPS客户端，不继承普通客户端的不验证配置，不跟随重定向。私有CA可配置 `CAMSTAR_SIGNATURE_CA_FILE`；证书不受信任则停止，不能回退不验证。
- 原生核验与提交的认证头/Cookie仅在本次调用内存绑定；核验期间会话Cookie刷新即停止。Signer固定为原生实际Employee；UserRequired固定为原组织/原生回显，用户不能覆盖身份或策略。
- 原生提交响应与异常自由文本不保存/回显，防止服务器反射口令。只记录受控状态、HTTP码或异常类别。HTTP200也不能当作审批成功。
- 当前只支持浏览器与Web服务在同一台机器、同一进程。独立stdio、远程浏览器、反向代理及多worker共享不支持，不提供远程口令中转。主Web启动/关闭管理输入服务和过期清理；没有为本轮开发重启现用服务。

## 原生依据与未覆盖项

标准模型QualityESigDetail7850仅有Signer、SignerPassword、UserRequired业务输入；Validate调用父事务VerifyPasswordSignature，ModifyEntityUsingDetail写HistoryMainline.QualityESigConfirmed。QualityTxn按组织策略取得要求、加载明细、最终验证并执行签名。其自身前后明细历史为Noop，不主动写口令历史。

依据是只读标准 `Base_InSite.mdb`，SHA256 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。索引、流程及历史字段证据见[索引](quality-fixtures/approval-c2b-native-index.json)、[流程](quality-fixtures/approval-c2b-native-flows.json)、[历史字段](quality-fixtures/approval-c2b-history-native.json)。这些不是实际部署契约；每次准备仍需核实现场Swagger和原生非提交回显。

本闭环未覆盖通用ESigRequirement/AdditionalESigReqs/ESigDetails、协签/多签、委托、自定义签名扩展，以及挂接/路由/修订/关闭等其它事务要求的签名。上述入口继续保留原门禁。后续先完成C2.B剩余通用签名与协签/多签，再推进C2.C委托，不跳过未知要求。

## 验证与现场状态

隔离模拟覆盖页面采集→计划状态→原生提交载荷→签名历史候选，另覆盖错误员工/组织策略、契约变化、角色替代与签名组合、失效、撤销、重启、并发、超时/400/取消、跨来源/CSRF及口令不回显/不落库。提交后若本地日志保存失败，返回无法确定而非“未提交”，保留in_flight占用并禁止重试。真实Edge浏览器使用虚构资料核对桌面/移动布局、地址片段移除、输入框清空及取消，无外部请求，不调用MES。

结果入口：[本轮汇总](quality-fixtures/approval-c2b-summary.json)、[完整JUnit](quality-fixtures/approval-c2b-delivery-junit.xml)、[最终定向JUnit](quality-fixtures/approval-c2b-final-junit.xml)、[浏览器验证](quality-fixtures/approval-c2b-ui.json)、[工具核对](quality-fixtures/approval-c2b-manifest.json)。HTTPX离线守卫不等于操作系统级网络隔离。

此前[只读通道复核](quality-fixtures/approval-c1-channel-diagnostic.json)环境与直接连接均ConnectError，部署契约、真实口令验证、签批结果、通知及最终审计仍未验收。本轮未部署、重启现用服务、更改MES配置或发出真实业务提交。
