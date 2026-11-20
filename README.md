# Web语义抽取与内容理解中心

面向Web结构化数据采集场景，提供页面DOM解析、实体识别、关系抽取、内容分类和知识图谱构建的语义抽取平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| DOM解析 | `dom_parser` | 解析配置 | 配置名称、目标URI、解析引擎 |
| 实体抽取 | `entity_extract` | 实体条目 | 实体ID、实体类型、实体名称 |
| 关系图谱 | `relation_graph` | 关系边 | 边ID、源实体、目标实体 |
| 内容分类 | `content_classifier` | 分类标签 | 标签ID、页面URI、一级分类 |
| 模板规则 | `template_rule` | 规则模板 | 模板名称、适用站点、提取字段 |
| 文本分块 | `text_segment` | 文本块 | 块ID、来源页面、块类型 |
| 情感标注 | `sentiment_tag` | 情感记录 | 记录ID、文本片段、情感倾向 |
| Schema对齐 | `schema_align` | 对齐规则 | 规则名称、源Schema、目标Schema |
| 种子调度 | `feed_scheduler` | 调度任务 | 任务名称、URL种子、抓取深度 |
| 质量检查 | `quality_check` | 检查规则 | 规则名称、检查字段、检查类型 |
| 探针选择 | `probe_selector` | 探针配置 | 探针名称、目标页面、选择器列表 |
| 页面快照 | `page_snapshot` | 快照记录 | 快照ID、页面URI、DOM哈希 |
| 抽取指标 | `extract_metric` | 指标记录 | 记录时间、所属任务、成功页面数 |
| 语义结果 | `nlu_result` | 语义记录 | 记录ID、输入文本、意图标签 |
| 知识链接 | `knowledge_link` | 链接记录 | 链接ID、源实体、外部URI |
| 输出目标 | `output_target` | 输出配置 | 配置名称、输出类型、目标地址 |
| 抽取工坊 | `extract_studio` | 交互任务 | 任务名称、示例页面、标注区域 |
| 聚合视图 | `aggregate_view` | 视图配置 | 视图名称、统计维度、聚合字段 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 实体质量审计与补证

质量检查模块对实体抽取结果做来源完整性把关（`页面来源`、`原文片段`为必备来源字段，
缺失即不能算通过），并提供以下接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/quality_check/run_audit` | 运行一轮实体批次校验。`{"entity_ids": [...]}` 指定审计范围（兼容条目 id 与历史实体ID），缺省为全部实体；同一范围重复提交沿用同一笔审计批次，异常中断后按原范围重试对应同一批次 |
| `GET /api/quality_check/audit_dataset` | 实体质量审计数据集：每条明细可追到具体实体和规则，并列出已查完/缺证据/已入库条目；支持 `?batch=` 按批次过滤 |
| `GET /api/quality_check/supplement_ledger` | 补证清单：逐项登记缺失的来源字段，跟踪待补证/已补齐/已入库 |
| `POST /api/entity_extract/{entry_key}/supplement` | 实体抽取按条目标识接收待补资料（页面来源/原文片段），保留原实体ID |
| `GET /api/entity_extract/graph_feed` | 已入库实体对关系图谱的供给，重复运行仍对上原实体ID |

实体「确认入库」按本轮审计结论放行：缺来源字段的条目留在补证清单等待补证；
入库后实体质量审计数据集与补证清单同步标明实际入库。审计台账存放在仓库
台账区（`store.ledger_rows`），不计入业务模块总览。

领域：数据工程
子方向：数据采集、爬虫与RPA
