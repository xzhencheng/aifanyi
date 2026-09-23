# v0.1 运行与接入手册

日期：2026-09-23。本文对应当前代码；完整一期验收标准仍以 [Spec](spec.md) 为准，差距见 [实施状态](engineering-status.md)。

## 1. 运行组成

API 和 Worker 使用同一数据库。写请求在数据库事务内保存任务和 `work` Outbox；Worker 执行模型调用，结果、状态与事件持久化。LangGraph 使用独立的持久检查点。重放先检查业务状态和模型调用记录，已完成节点的成功结果可复用。

- 本地：SQLite + 持久 SQLite 检查点 + `aifanyi worker` 数据库轮询，适合开发。
- 部署：PostgreSQL + PostgreSQL 检查点 + Redis/Celery Worker + Celery beat。beat 每 5 秒扫描数据库 Outbox；Redis 中丢失的通知可从数据库重新派发。
- 浏览器：React 薄客户端，由 API 提供构建后的静态文件。令牌只保存在页面内存；刷新后重新输入。会话 ID 可保存在浏览器，内容从后端读取。
- 模型：两个现有/独立部署的私有端点。应用不下载权重、不部署 GPU 服务、不调用公共备用模型。

进程重启的业务恢复方式是“重新进入图 + 持久节点结果重放”，不是恢复一个仍在等待的远端 HTTP 请求。模型响应未确认的请求记为 `outcome_unknown` 并消耗调用预算。

## 2. 本地启动

依赖和基本命令见根目录 README。复制 `.env.example` 后，可用以下脚本生成两个随机值，再编辑模型字段：

```bash
python - <<'PY'
import secrets
from pathlib import Path
p = Path('.env')
s = p.read_text()
s = s.replace('AIFANYI_DEV_TOKEN=\n', 'AIFANYI_DEV_TOKEN=' + secrets.token_urlsafe(32) + '\n')
s = s.replace('POSTGRES_PASSWORD=\n', 'POSTGRES_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
p.write_text(s)
PY
```

配置由 Python 读取 `.env`；不用将整个文件 `source` 到 shell。Secret 引用从进程环境优先读取，本地再读取 `.env`，不会写入模型配置、任务快照或接口返回。

`aifanyi migrate` 初始化业务表及检查点；`aifanyi init` 幂等创建默认项目、开发身份及草稿模型组合。之后修改 `.env` 中的模型地址或参数时，对尚未验证的草稿运行：

```bash
aifanyi configure-models
aifanyi validate-models
```

验证成功只代表接口和输出协议通过最小探测，不代表业务质量达标。已经验证的 profile 不通过此命令原地改参数；通过 `POST /v1/model-profiles` 新建配置并验证。

开发页面热更新可另开终端执行 `npm --prefix apps/chat run dev`，Vite 将 `/v1` 和 `/health` 代理到本机 API 的 8000 端口。生产构建使用 `npm --prefix apps/chat run build` 后由 API 统一提供。

## 3. 模型接入条件

| 角色 | 默认约束 | 必须核实 |
| --- | --- | --- |
| Hy-MT 初译 | 单片段纯文本；总窗口 8192、输出预留 4096；并发 1；BF16 | 实际 model ID、权重 revision、后端支持、官方聊天模板、采样参数和显存 |
| Qwen 审校 / 修复 | JSON 对象；严格 Pydantic 校验；实际源译证据定位 | 现有型号、精度、revision、JSON mode、实际可用窗口和响应结束标志 |
| Qwen 对话 | 结构化意图和原文 Unicode 切片；不直接生成正式译文 | 同一端点、模型与共享并发上限；思考字段配置 |

所有 base URL 以 `/v1` 结尾。服务必须支持：

1. `GET /v1/models`，返回包含配置 model ID 的 `data[].id`。
2. `POST /tokenize`，接受 `model`、`messages`、`add_generation_prompt`，返回正整数 `count` 或 token ID 数组 `tokens`。计数必须包含与推理相同的模板。
3. `POST /v1/chat/completions`。Hy-MT 接收纯文本翻译请求；Qwen 接收 `response_format: {"type":"json_object"}`。最终内容在 `choices[0].message.content`，完整输出须 `finish_reason="stop"`。

这是当前 vLLM 风格适配器的明确能力要求。若已有 Qwen 网关不暴露 `/tokenize`，应给网关增加使用**同一 revision、同一模板**的 tokenizer 接口或另写适配器；不能用字符数估算后谎称已验证。原文、强制约束或跨段审校超预算时本版明确失败，不静默截断。

`.env` 中 Qwen 总窗口和输出预留默认 16384 / 4096，是待核实起始值，**不是对用户现有环境的已知规格**。`provider_default` 不主动发送思考开关；显式开关可选 `chat_template_kwargs` 或 `enable_thinking`。响应中分离的 reasoning 字段不当作译文；content 中含 `<think>` 标签、空输出或触顶输出均拒绝。

同一端点用数据库槽位控制并发。Qwen 配额大于 1 时，对话只使用最后一个槽位，为审校/修复保留至少一个槽位。槽位等待超时返回 `MODEL_BUSY`；没有跳过审校的降级路径。

任何正式发布前均需固定真实 revision、精度、模型模板及业务质量报告。新 profile 的真实型号、量化和提示变化应重新验证与评测。

## 4. 容器试点部署

先按上述说明编辑 `.env`，尤其是随机的 `POSTGRES_PASSWORD`（使用 URL 安全字符）和开发令牌/企业身份配置。Compose 会覆盖数据库、检查点和调度地址：

```bash
docker compose up --build -d
docker compose run --rm initialize aifanyi validate-models
docker compose logs --tail=100 api worker scheduler
```

初始化完成后 API 才启动。应用提供 `/health/live` 和 `/health/ready`；后者只检查数据库迁移可用，模型状态必须通过 profile 验证查看。

模型服务不在 Compose 中，容器内的 `localhost` 指容器自身；应使用应用网络能访问的内网 DNS/IP。可用 `AIFANYI_ALLOWED_MODEL_HOSTS` 登记其他允许的新模型主机，参数为 JSON 数组。

生产需要同时配置：

- `AIFANYI_ENVIRONMENT=production`、`AIFANYI_DEV_AUTH=false`。
- `AIFANYI_OIDC_ISSUER`、`AIFANYI_OIDC_AUDIENCE`、`AIFANYI_OIDC_JWKS_URL`。
- `AIFANYI_BOOTSTRAP_OIDC_SUBJECT=<issuer>|<sub>`，用于**首次**登记管理员；不是访问令牌。
- PostgreSQL、PostgreSQL 检查点和 `AIFANYI_DISPATCH=celery`。
- 已通过业务评测门槛并发布的 profile、实际 model revision/precision、有效语言方向及领域范围。

Compose 基础镜像锁定大版本，尚未锁定生产镜像 digest；镜像扫描、真实 PostgreSQL/Celery 故障恢复、TLS、企业登录接入、备份和资源压测仍属部署验收项。**本版不是已验收的生产发行版。**

应用默认只监听服务器本机暴露端口。企业入口由反向代理提供 HTTPS、请求大小和速率控制。SSE 禁用代理缓冲，读取超时应大于 15 秒心跳间隔。不要把模型、Redis、数据库端口暴露给普通用户。

## 5. 身份、角色和机器调用

JWT 的签名、issuer、audience、exp、iat、sub 都会验证；租户和项目角色取自后端登记，不信任用户请求中的权限声明。首个管理员通过初始化登记。其他人/调用系统由有服务器运维权限的人登记：

```bash
aifanyi grant-principal --subject 'https://id.example/realm|employee-sub' --project project_default --role reviewer
```

此命令把指定项目角色设为 `user` 加显式 `--role` 列表，并记录审计；不授予全局管理员。机器身份先通过管理员 `POST /v1/clients` 创建调用系统，再用同一命令追加 `--client-id <client_id>`。使用企业身份服务签发的对应 JWT 调用 API。不存在随意自报 `client_id` 或任意项目的 API Key 路径。

当前没有完整组织管理页面、多租户开户流程及身份撤销管理 UI。数据库/CLI 是受信任的运维边界；不得向普通用户开放数据库写权限。

## 6. 翻译与对话操作

所有业务写请求携带 `Authorization: Bearer ...`、`Content-Type: application/json` 和 `Idempotency-Key`。相同键相同请求在 7 天内返回原响应；同键改内容返回冲突。同步接口等待超时仍返回可查询 job 引用，不取消已创建任务。

```bash
curl -X POST http://127.0.0.1:8000/v1/translation-jobs \
  -H 'Authorization: Bearer <你的令牌>' \
  -H 'Idempotency-Key: example-translation-001' \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"project_default","source_language":"zh-CN","target_language":"en","input":{"kind":"text","text":"设备处于待机状态。"}}'
```

按返回 `status_url` 查询，`GET /v1/translation-jobs/{id}/result` 只在全部放行后返回结果。待审草稿通过 `/segments` 查看，不能视为最终译文。每次模型审校必须完整列出被检查的片段，问题 quote 与 Unicode 码点范围须逐字匹配。

聊天流程：创建 conversation → `POST /v1/agent/messages` → 查询 run / 读取 SSE。`client_message_id` 在会话内独立去重，调用方即使换 Idempotency-Key 也不能重复创建同一消息。一个会话同时只允许一个非终态 run。

缺信息时运行停在 `awaiting_input`；使用 `/runs/{id}/responses` 携带问题 ID、运行版本和会话版本继续。纯文本入口直接给出精确原文范围；自然语言对话由 Qwen 解析范围，后端校验原文未被改写。原文偏移按 Unicode 码点计算，前端使用展开字符串计数。

SSE 使用 Bearer 认证，支持 `Last-Event-ID`；过期游标返回 410，改查 run 和消息。网络连接断开不取消任务。前端用 fetch 读取 SSE，不将令牌拼接到 URL。

局部重译使用 `POST /v1/translations/{id}/retranslations`，明确 `base_revision`、`segment_ids` 和 instruction。默认继承快照；`knowledge_mode=latest` 才显式重建。未选中的合格候选沿用文本，但会重新检查，旧结果和原文不覆盖。聊天中也可勾选片段后要求重译。

## 7. 人工审阅和知识维护

`awaiting_review` 必须由项目 reviewer 处理。`POST /translation-jobs/{id}/review-decisions` 携带任务版本、译文版本及片段候选版本，支持：

- `replace_translation`：新建候选版本并重新检查，旧审批失效。
- `mark_false_positive`：附原文/知识证据及原因，仅能裁决语义问题，不能跳过数字/锁定术语等硬检查。
- `accept_minor`：只能接受 minor 问题，不能把真实 critical/major 当作风格意见放行。
- `approve_segment`：须当前版审校完成且没有未处理问题。维修等风险命中时，即使自动检查无误也需要人工确认。

`finalize=true` 将本轮人工裁决重新交给 Worker 校验，不直接生成成功结果。跨段一致性审校提出的问题也进入同一裁决流程。

知识维护的顺序为：

1. `POST /v1/knowledge/entries` 建草稿，明确 term / tm / policy / domain_excerpt、来源、语言方向与作用范围。
2. `/knowledge/proposals/{entry_id}/submit` → `/approve` 或 `/reject`，带 `expected_version` 和理由。
3. `POST /knowledge/releases` 指定已批准的 `entry_version_refs`。
4. 更新通过 `/knowledge/entries/{id}/versions` 建新版本；`/knowledge/releases/{id}/rollback` 创建新的回滚发布，保留历史。

企业锁定项优先于个人与项目项。同优先级冲突不猜测。TM 仅在人工批准、原文和背景指纹精确匹配、当前适用术语仍满足时复用，复用后仍要 Qwen 审校。任务快照在创建时固定，不因后台新发布而变化。

反馈 API 保存 once / personal / project / enterprise 的明确选择。持久范围的反馈可由有权维护者调用 `/v1/feedback/{id}/knowledge-proposals` 提炼成知识草稿，再按上述流程审核发布。本次反馈目前需显式发起重译才影响译文，不会自动生效；自动提案、来源更正的专门入口与影响分析仍待实现。

## 8. 模型组合和质量报告

`POST /v1/model-profiles` 建新配置；`/{id}/validate` 实测协议；`/{id}/publish` 检查人工批准的独立质量报告。`/{id}/activate` 切换项目默认配置或回到已有的合格旧配置，仅影响新任务。配置快照和旧调用记录保留。

**本版尚无离线 E0–E4 评测执行器。** 新增的 `POST /v1/evaluation-reports` 仅供 reviewer 登记已完成、有人审阅的外部报告，不等价于 Spec 的 `POST /v1/evaluations` 发起评测。报告可通过 `/v1/evaluations/{id}/report` 读取。登记接口不校验外部 report_uri 的内容；真实性、盲评与数据隔离仍需人工流程及后续执行器落实。

发布检查至少要求：报告对应当前 profile 版本、固定测试/标注/分集清单、人工审阅、无 critical、重大错误率不劣于固定单模型基线、成对差异区间上界小于零、术语准确率 ≥99%、自动覆盖和审校召回/误报满足明确阈值。生产任务只使用发布覆盖的方向和领域。

这些是配置闸门，不是模型自行打分的“准确率保证”。未经独立业务评测，不应把新 profile 当作已证明更准确的生产模型组合。

## 9. 外部回调

管理员登记调用系统和 HTTPS 回调端点；同时配置主机允许清单及允许的解析 IP 网段。每次发送重新解析并校验全部 IP，连接固定到通过校验的 IP，保留原始 Host 和 TLS 主机名校验。关闭重定向和环境代理，拒绝环回、链路本地等地址。

回调端点使用 `AIFANYI_CALLBACK_<NAME>` Secret 引用，值至少 32 字符。签名原文是 UTF-8 编码的：

`timestamp + "." + event_id + "." + raw_body`

发送 `X-Translation-Key-Id`、`X-Translation-Timestamp`、`X-Translation-Event-Id`、`X-Translation-Signature: v1=<HMAC-SHA256 hex>`。接收端使用原始 HTTP 正文验证签名、限制时间偏差并按事件 ID 去重。每次重试重新签名，事件 ID 与正文不变。

2xx 确认投递；408、429、5xx 和网络异常最多重试 6 次，基础间隔 1m / 5m / 15m / 1h / 6h / 24h，加正向抖动，并尊重有界 Retry-After。其他响应或重定向终止投递。回调失败不改变成功的翻译状态。

`GET /v1/callback-deliveries` 查看记录，`/{id}/redeliver` 人工重发 dead 项。停用 client 或 callback-endpoint 后，后续尝试停止。已在网络上传输的请求无法撤回；接收端仍需幂等。

## 10. 运维边界

- 目前没有自动保留期清理任务；不得认为声明 30 天就会自动删除数据。生产启用前需补齐业务表、模型响应、会话、检查点、备份和审计的协调保留策略。
- 模型配置使用逻辑模板版本；应用升级时提示正文也属于质量变更，需升级模板版本、重新评测和记录应用 commit。
- PostgreSQL 与 Celery 路径有代码和 CI 配置，生产故障/并发/备份恢复与实际 GPU 压测需要在部署环境验证。
- 不记录原始模型响应或 Secret 到应用错误消息，但业务数据库会保留原文、候选及成功模型响应。应由企业实施访问控制、备份加密与磁盘保护。
- 当前没有完整指标导出、告警、速率限制服务或生产 SLA；查看 job/run 状态、模型调用账本、Outbox 和审计记录定位问题。
