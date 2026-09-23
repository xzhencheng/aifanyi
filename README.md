# AI 翻译平台

准确性优先的企业翻译 Agent：**Hy-MT2-7B 初译 → 程序检查 → 现有 Qwen 3.5 对照原文审校 → 有限修复 → 结果或人工待审**。一期支持粘贴文本、批量文本与对话，暂不做任何文件翻译。

## 当前状态

已开始按 [Spec v1.1](docs/spec.md) 实施，交付 **v0.1 工程实现**：FastAPI 后端、持久化 LangGraph 翻译流程、Worker、对话 API/SSE、React 聊天页、基础知识治理与模型配置接口，以及迁移和部署配置。

**这不是完整一期验收。** 本次自动化验证使用受控模型响应；尚未连接真实 Hy-MT2-7B / Qwen 3.5，没有业务准确率、GPU 容量或生产性能结论。全部已做、未做和验证边界见 [实施状态](docs/engineering-status.md)。

## 启动开发环境

需要 Python **3.12**、Node **24**。先安装依赖、创建本地配置：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
npm --prefix apps/chat ci
npm --prefix apps/chat run build
cp .env.example .env
```

在 `.env` 中填入至少 24 字符的随机 `AIFANYI_DEV_TOKEN`、两个私有模型端点及 Qwen 的实际 model ID。模型密钥只写本地配置或部署 Secret。随后执行：

```bash
aifanyi migrate
aifanyi init
aifanyi validate-models
```

`init` 创建 `project_default` 和**草稿**模型配置。`validate-models` 会真实调用模型；探测失败时配置不会放行。当前适配器要求 `/v1/models`、`/v1/chat/completions` 和包含聊天模板的 `/tokenize`，详见 [运行手册](docs/runbook.md)。

分别在两个终端运行：

```bash
# 终端一，先激活 .venv
uvicorn aifanyi.api:app --host 127.0.0.1 --port 8000
```

```bash
# 终端二，先激活 .venv
aifanyi worker
```

打开 <http://127.0.0.1:8000>，填入本地开发令牌，选择语言并粘贴文本。API 交互文档在 `/docs`。开发令牌只适用于本地开发，生产模式要求企业 OIDC、PostgreSQL 和 Celery。

没有模型服务时，可以完成迁移、查看页面和运行测试；业务翻译会明确拒绝未验证配置。运行时没有模拟翻译或备用公共模型。

## 验证与部署

```bash
ruff check src tests migrations
pytest -q
alembic check
npm --prefix apps/chat run build
```

仓库提供 `Dockerfile` 与 [compose.yaml](compose.yaml)，包括 PostgreSQL、Redis、API、Worker 和调度器；默认仅将应用绑定到服务器本机端口。启动顺序、模型接入、身份登记、知识审核及回调见 [运行手册](docs/runbook.md)。Docker 配置不包含 GPU 推理服务，也不启动或改动现有 Qwen。

Hy-MT2-7B BF16 的低并发起步预算为 **1 张 24 GB GPU、64 GB RAM、16 vCPU、200 GB 可用 NVMe**，先用 8K 总窗口、并发 1；48 GB GPU 档留有更大余量。这是待实测预算，完整估算见 [模型部署与服务器要求](docs/model-deployment.md)。

## 文档

| 文档 | 用途 |
| --- | --- |
| [一期需求基线](docs/requirements.md) | 范围和业务目标 |
| [技术方案](docs/technical-design.md) | 架构、准确性流程与模型选择 |
| [实施 Spec](docs/spec.md) | 完整一期契约和验收要求 |
| [运行手册](docs/runbook.md) | 本版实际可执行的启动与操作步骤 |
| [实施状态](docs/engineering-status.md) | 代码覆盖、测试证据、限制和剩余工作 |
| [实施与开发清单](docs/implementation-plan.md) | 分阶段完成条件 |
| [模型部署与服务器要求](docs/model-deployment.md) | Hy-MT 资源与已有 Qwen 接入 |

需求来源为用户提供的《日立电梯 AI 翻译业务方案》V1.1。客户原始方案、语料、词表、真实译文与生产配置不进入仓库。开发按用户要求直接提交 `main`。
