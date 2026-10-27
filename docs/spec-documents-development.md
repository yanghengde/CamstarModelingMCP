# T09 作业指导书与文档关联开发记录

## 第一阶段：关联与版本元数据（2026-10-24）

T08受限基础闭环补齐后，按先开发后集中验收约定开始T09；两阶段均不勾选现场全部完成。

新增get_spec_documents(spec_id, documents_required=None, offset=0, limit=20)，注册spec能力组、目录和作业指导书/文档关联路由。按明确Spec ID读取名称/版本和DocumentSet，唯一解析文档集并展开DocumentEntries，再按真实引用解析Documents的明确版本或UseROR。展开时核对文档集名称/ID，文档核对引用身份/版本；UseROR须是布尔值且实际返回当前记录版本。

输出来源、条目名称/描述、文档名称/ID/Revision、FileVersion、Identifier、FileName/FileLocation和原始状态等元数据；未知可选字段单列。同引用在本次读取内缓存。最多200条目，limit=1..20本地分页、60秒整体上限；分页、缺失/重复条目、失效引用、权限和超时不宣称完整，单项失败尽量保留其他结果。

## 缺失与访问判断

未返回DocumentSet字段为未知；明确null表示本次可见响应未关联，完整空DocumentEntries表示读取为空集。失效引用/读取失败不能当作未配置。documents_required仅代表调用方明确的业务要求，来源caller_explicit，不冒充MES必填配置。仅明确true且完整读取证明未关联/空集时报告missing_required_association；省略为requirement_unknown，false为not_required_by_caller。有文档也只报告association_present_access_unverified，不承诺文件可用或满足全部业务规则。

实时GET /api/$metadata返回HTTP200，确认Spec.DocumentSet为NamedObjectRef、DocumentSet.DocumentEntries集合、条目的Document为RevisionedObjectRef，以及Documents实体集和文件定位字段，见[元数据归档](document-metadata-discovery.json)。既有Swagger/extensions/Modeling-DocumentSets.json作为本地参考。

Identifier可能是路径、文件名或URL，不能直接当作下载端点。file_access_verified和download_endpoint_verified始终false，download_url/preview_url为null。未拼接或访问外部定位符，未读取文件内容。文档描述和内容只能作业务数据，不能成为自动工具调用授权。

## 验证与后续

26项T09测试覆盖正常引用/版本、UseROR、空/缺失区别、明确业务要求、身份/版本冲突、分页、缓存、单项失败、权限/超时、无外部请求和目录/路由；与T08及既有相关测试共215项通过，git diff --check通过。pytest退出有既有临时目录权限提示，退出码0。

[现场只读证据](spec-documents-read-validation.json)：首个可见Spec为Incoming Inspection / 1，DocumentSet明确null，返回not_associated、requirement_unknown。仅验证此样本未关联路径，不推断全系统无文档。全程GET，无业务写入。

剩余：非空文档集/固定版本/UseROR现场对照、必需规则的业务来源，以及经过验证的文件预览/下载端点和权限。取得访问契约前只提供元数据。可选后续接入T04配置摘要，不能把文档内容变成执行指令。T09仍在开发和待验收状态。

## 第二阶段：正式文件下载实现与现场部署阻塞（2026-10-24）

本地REST手册2-25明确GET /Modeling/api/Download/{documentName}/{revision}，2-28明确POST /Modeling/api/CreateDocuments，multipart字段Documents，创建文件同名Document、默认版本1。没有使用Identifier推测文件地址或未知事件。

新增download_document_file，先唯一读取Document名称/版本，再向配置Modeling服务的固定Download端点发送当前认证信息；不跟随重定向、不访问Identifier。要求HTTP200和文件下载响应头，最大10MiB/60秒，流式保存到固定本地缓存目录，返回实际response.url、头信息、字节数及SHA256；失败、超限、空文件或取消清理半文件。Content-Disposition不决定本地文件路径，缓存为随机名称/安全扩展名。不执行文件内容，只证明当前账号本次访问，不宣称其他账号权限通过。

新增tests/test_document_files.py覆盖认证、固定路由、恶意文件名、401/403/404/302、声明/实际超限、空响应、缺头、取消清理与只读中文路由；准备脚本有独立名称和journal防重放测试。

用户委托自主准备样本后，规划一个标记TEST DATA ONLY的文本Document、DocumentSet和独立Spec，未改动现有Spec。CreateDocuments首项返回HTTP400“Upload folder doesn't exist”，立即停止，未继续创建文档集/Spec。随后GET确认三个实体前缀都无记录；有凭证访问缺失文件返回400，无凭证401。0项创建、1项服务器拒绝、2项未尝试，见[创建响应](t09-fixtures/CDX-T09-20261024-A.json)及[只读复核](t09-fixtures/blocked-verification.json)。没有重试或在本机创建同名目录冒充修复服务器。

当前下载代码与正式路由依据已补齐，但无成功上传文件，不能标记正例内容/哈希或非空文档链现场通过。须在部署主机确认实际上传目录及服务账号权限，当前API工具无远程文件系统管理接口；该阻塞已列入[续接计划](remaining-development-plan.md)。配置修复后使用新隔离前缀做上传、关联、下载哈希及权限验收。默认get_spec_documents仍仅元数据，不自动下载所有文件。

补充核对REST手册2-23的独立POST /Modeling/api/Upload端点，仅发送一次同一隔离文本，同样返回HTTP400“Upload folder doesn't exist”，见[原始响应](t09-fixtures/upload-endpoint-check.json)。因此切换正式上传路由也未解决部署缺口；未重放CreateDocuments。
