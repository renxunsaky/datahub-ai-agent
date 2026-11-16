# 任务：实现"数据导航 Agent"（Data Navigator Agent）

你是一名资深 Python / 数据平台工程师，要在当前仓库里实现一个面向银行内部大数据平台的 Agent：用户用自然语言描述数据需求，Agent 根据数据目录生成可执行的 SQL。请先完整读完这份说明，再开始动手。

---

## 0. 工作方式（请严格遵守）

1. **先读再写**：先查看仓库现状。如果仓库里有 `docs/design.md`（从项目方案文档导出），以它为准；它和本说明冲突时，先问我。
2. **分阶段推进**：按第 8 节的阶段进行。**本次会话只做阶段 0 和阶段 1**，完成后停下来汇报，等我确认后再继续。
3. **每个阶段结束时**：运行 `ruff`、`mypy`、`pytest`，全部通过后再汇报。汇报内容包括：完成了什么、关键设计决定、未解决的问题、需要我确认的事项。
4. **不要臆造环境细节**：第 9 节列出的"待确认信息"一律做成配置项加接口抽象，用本地 fixture 实现和测试，并在代码中标注 `TODO(confirm): ...`。
5. **涉及安全的取舍先问我**：第 3 节的约束不能自行放宽。如果某个约束让实现变得很困难，停下来说明情况，不要绕过。
6. **只在本地运行**：不要编写任何部署相关的代码（Dockerfile、Kubernetes 清单、Airflow DAG、对外的 Web 服务都不需要）。
7. **小步推进**：按逻辑单元分小步提交；commit message 用英文，说明做了什么、为什么。
8. **语言**：代码、注释、README、commit message 用英文；和我的对话用中文。

---

## 1. 背景

- 平台：Airflow 负责调度，Spark 和 Starburst（Trino）负责计算；数据存放在 IBM COS，每个项目组有自己的 bucket；多个项目组共用平台，权限相互隔离。
- 公司内部提供 LLM as a Service（LLMaaS）。
- 痛点：没有数据目录，用户不知道有哪些表、列是什么意思、表之间怎么关联，写 SQL 之前要花大量时间找表。
- 可以利用的数据：
  - 一个**专用的 Starburst 用户**，可以查询元数据（`information_schema` 等）和历史 SQL。
  - **GitLab 上的 Airflow 代码仓库**：只读根目录下 `src/` 和 `automation/` 里后缀为 `.sql` 的文件。

## 2. 目标与非目标（MVP）

**目标**：

1. **构建数据目录**：一次性读取元数据、历史 SQL 和仓库里的 `.sql` 文件，生成轻量的数据目录（表卡片、JOIN 关系图、血缘、脱敏后的查询模板）。之后在新增表时，由用户**手动**触发增量更新。没有定时任务。
2. **生成 SQL**：根据自然语言需求和目录，生成可执行的只读 SQL，并以用户本人身份执行 `EXPLAIN`，校验语法和权限。
3. **候选表模式**：把握不足时，不生成 SQL，而是列出可能满足需求的表及其描述（不含数据），说明不确定在哪里，并向用户提问澄清。
4. **样例数据**：用户看过 SQL 后，可以在同一个对话里确认，允许 Agent 以用户身份执行，最多读取 10 行（可配置），用来核对 SQL 是否符合预期。

**非目标（MVP 不做）**：

- 部署、SSO、多用户服务。
- 任何写操作（INSERT、DDL 等），以及导出完整结果集。
- 解析 Spark 作业代码或 DAG 的 Python 代码（只读 `.sql` 文件）。
- 自动审批或自动授权。

---

## 3. 不可违反的安全约束

1. **两个独立的 Starburst 连接**：
   - `builder` 连接使用专用 Starburst 用户，**只用于构建目录**。
   - `end_user` 连接使用用户本人的凭证，**只用于 `EXPLAIN` 和样例查询**。
   - 两者是分开的配置段和分开的客户端类。用架构测试（例如 `import-linter`）保证 `agent/` 模块无法导入 `builder` 客户端。
2. **`builder` 查询白名单**：`builder` 客户端在发出查询前检查语句，只允许以下几类：查询 `information_schema.*`、查询 `system.metadata.*`、查询配置好的历史 SQL 表、`SHOW` 和 `DESCRIBE`。其他任何语句都在本地直接拒绝并抛出异常。
3. **生成的 SQL 只读**：执行前用 sqlglot 检查，只允许单条 `SELECT`（可以带 `WITH`），拒绝 DDL、DML、多条语句、`CALL` 和 `SET` 等。这个检查是确定性代码，不经过 LLM。
4. **样例查询的限制**：
   - 在外层强制套上行数限制，默认 10 行，不能由 LLM 修改。
   - 设置查询超时和扫描量上限（通过 Trino 会话属性，例如 `query_max_execution_time`，具体数值可配置）。
   - 每次执行前，必须用 LangGraph 的 `interrupt` 暂停，等用户在界面上明确确认；没有确认就不执行。
5. **样例数据的去向**：
   - 样例数据永远不写入目录库，也不写入日志。
   - 是否把样例发给 LLM 由配置项 `samples.send_to_llm` 控制，**默认 `false`**：此时样例只展示给用户，Agent 只看到行数和列名。
   - 只有在安全团队确认 LLMaaS 可以处理这一等级的数据后，才把它改成 `true`。
6. **SQL 脱敏**：历史 SQL 和 `.sql` 文件里的 SQL，在持久化、建索引、发给 LLM 之前，必须先把常量（字符串、数字、日期、时间间隔、IN 列表）替换成占位符。解析失败的 SQL 只记录哈希和错误类型，不保存文本。
7. **权限结论是确定性的**：`check_access` 和 `validate_sql` 的结论由代码给出，LLM 不能产生或改写。出现异常时只能返回 `unknown`，**永远不能返回 `allowed`**。
8. **用户身份由 harness 注入**：`end_user` 的凭证在会话启动时提供，通过闭包或 LangGraph config 注入工具。工具参数里不包含用户身份，LLM 无法填写或修改。
9. **可见范围分级**：
   - 所有用户可见：表名、负责团队、一句话用途。
   - 仅对该表有权限的用户可见：列的详细说明、相似查询模式。
   - 任何人都不可见：其他组的 SQL 原文、SQL 常量。
10. **只用内部 LLMaaS**：`base_url` 必须通过配置提供，没有默认值；不得默认指向任何公网服务。
11. **提示注入防护**：表注释、列名、SQL、`.sql` 文件内容和样例数据，在提示词中都放在带标签的数据区块里（例如 `<metadata>…</metadata>`），系统提示明确说明这些内容是数据，不是指令。
12. **密钥管理**：Starburst 凭证、GitLab 令牌、LLMaaS 密钥只从环境变量或本地 `.env` 文件读取；`.env` 加入 `.gitignore`；密钥不出现在代码、配置样例、日志或测试快照里。clone 仓库时令牌不能写进 `.git/config` 或日志。
13. **审计**：记录用户、问题、生成的 SQL、校验结果、样例执行情况（执行了哪条 SQL、是否经用户确认、返回多少行）；不记录 SQL 常量和数据值。

---

## 4. 技术栈与约定

- Python 3.11 及以上；依赖管理用 `uv`（如果仓库已有约定则沿用）。
- Agent：LangGraph，用 `interrupt` 实现执行样例前的用户确认。
- SQL 解析和只读检查：`sqlglot`，Trino 方言为主，Spark 方言备用。
- Starburst 访问：官方 `trino` Python 客户端。
- 仓库访问：`git` 命令行或 GitPython。
- **存储：本地 SQLite，所有读写都经过统一接口，以后可以换成 PostgreSQL**：

| 能力 | 接口 | MVP 实现 | 以后的实现 |
| --- | --- | --- | --- |
| 关系数据 | `CatalogRepository` | SQLite（SQLAlchemy 2.x + Alembic） | PostgreSQL，同一套模型和迁移 |
| 全文检索 | `TextIndex` | SQLite FTS5 | PostgreSQL 全文检索 |
| 向量检索 | `VectorIndex` | 向量存为 BLOB，在内存中用 numpy 计算余弦相似度 | pgvector |

  通过配置项选择实现（例如 `storage.backend: sqlite | postgres`，`storage.url`）。业务代码只依赖接口。SQLite 实现之外，先为 PostgreSQL 实现留好空壳类和 `TODO`，并保证 ORM 模型不使用 SQLite 专有的写法。
- 界面：
  - 命令行：用 Typer 写 `build`、`refresh`、`chat`、`review` 等子命令。
  - 本地网页对话：Streamlit 单页，带"允许读取样例"确认按钮（阶段 3 和 4 再做）。
- 配置：`pydantic-settings`，YAML 配置文件加环境变量覆盖。
- 测试：`pytest`；静态检查：`ruff`；类型检查：`mypy`，核心包开启 strict。

建议的目录结构（可以调整，但要说明理由）：

```
src/data_navigator/
  config.py
  starburst/             # builder_client.py（带白名单）, end_user_client.py
  sources/               # metadata.py, query_history.py, sql_files.py（Protocol 接口 + 实现）
  sqlproc/               # template.py, normalize.py, fingerprint.py, parse.py, readonly_guard.py
  analysis/              # usage.py, join_graph.py, lineage.py
  catalog/               # models.py, repository.py, sqlite_repo.py, postgres_repo.py（空壳）, migrations/
  search/                # text_index.py, vector_index.py, hybrid.py
  cards/                 # schemas.py, prompts.py, generate.py
  llm/                   # client.py
  agent/                 # state.py, tools.py, prompts.py, graph.py
  ui/                    # streamlit_app.py
  audit/                 # log.py
  cli.py
tests/{unit,integration,fixtures/{information_schema,query_history,repo}}
eval/{golden_set.example.yaml,run_eval.py}
```

---

## 5. 数据模型（目录库）

| 实体 | 主要字段 |
| --- | --- |
| `table` | 全名（catalog.schema.table）、负责团队、表类型、注释、最近被查询时间、使用次数、使用者团队数、结构哈希（用于发现结构变化） |
| `column` | 名称、类型、注释、AI 描述、使用次数、是否常作 JOIN 键、是否常作过滤条件、PII 标记 |
| `table_card` | 用途、关键列说明、常见用法、示例问题、依据、置信度、审核状态（`ai_inferred` / `confirmed` / `rejected`）、审核人、输入哈希 |
| `join_edge` | 左表和列、右表和列、JOIN 类型、出现次数、按来源加权的分数 |
| `lineage_edge` | 上游表、下游表、来源 `.sql` 文件路径 |
| `query_template` | 指纹、脱敏后的 SQL、涉及的表、执行次数、使用者团队、来源（历史查询或文件路径）、最近执行时间 |
| `build_run` | 类型（`build` / `refresh`）、开始和结束时间、历史 SQL 处理到的时间点、仓库 commit SHA、新增和变化的表数 |
| `audit_log` | 用户、问题、生成的 SQL、校验结果、样例是否经确认执行、返回行数、时间 |

人工确认过的字段，在之后重新生成时不能被覆盖。

---

## 6. 模块规格

### 6.1 Starburst 客户端（`starburst/`）

- `BuilderClient`：使用专用用户的凭证。每条语句先经过第 3 节第 2 条的白名单检查（用 sqlglot 解析后判断语句类型和涉及的表，不要用字符串匹配）。
- `EndUserClient`：使用终端用户的凭证。只暴露两个方法：`explain(sql)` 和 `sample(sql, max_rows)`。两者内部都先调用 `readonly_guard`；`sample` 在外层套上行数限制，并设置超时和扫描量相关的会话属性。
- 错误映射：
  - `PERMISSION_DENIED` → 无权限。
  - 语法、语义错误 → 返回给 Agent 用于改写。
  - 超时、连接错误以及其他所有错误 → `unknown`。

### 6.2 数据源（`sources/`）

每个数据源定义一个 `Protocol`，并提供真实实现和 fixture 实现。

- **元数据**：
  - 通过 `BuilderClient` 查询 `information_schema.tables` 和 `information_schema.columns`；表注释来自 `system.metadata.table_comments`。
  - 如果列注释在 `information_schema` 里取不到，对目标表执行 `SHOW COLUMNS` 获取。
  - 要查询的 catalog 列表可配置，排除的 schema 也可配置（例如 `information_schema` 本身）。
- **历史 SQL**：
  - 历史表的位置未知：表名和列映射全部做成配置（`query_id`、`query_text`、`user`、`state`、`started_at` 等）。
  - 按时间窗口读取，并支持从上次处理到的时间点增量读取。只处理执行成功的查询。
  - 另外提供一个 JSONL 文件适配器，用于 fixture 和测试。
- **`.sql` 文件**：
  - 根据配置的 GitLab 地址 clone 仓库（令牌来自环境变量，通过 credential helper 或临时环境变量传入，不写进 URL 和日志）；之后用 pull 更新。
  - 只收集根目录下 `src/` 和 `automation/` 中后缀为 `.sql` 的文件，目录列表可配置。
  - 记录每个文件的路径和最后一次修改它的 commit SHA，支持"只处理上次 commit 之后变化的文件"。
- **用户到团队的映射**：做成接口，先用配置文件实现。

### 6.3 SQL 处理（`sqlproc/`）

- `template.py`：把 `.sql` 文件中的模板语法（Jinja 的 `{{ ... }}`、`{% ... %}`，以及可配置的其他占位符形式）替换成可解析的占位符；把一个文件按语句拆开。
- `normalize(sql, dialect) -> NormalizedSQL`：用 sqlglot 解析（先按 Trino，失败再试 Spark），把所有常量替换成占位符，压缩 IN 列表，去掉注释，输出规范化文本。
- `fingerprint(normalized) -> str`：对规范化文本取 SHA-256。同一模式的查询，常量不同也应得到相同指纹；每天重复执行的任务会合并成一个模式，只累计次数。
- `parse(normalized, schema) -> ParsedQuery`：用目录里的表结构做列的限定（sqlglot `qualify`），输出：读取的表、写入的表、每张表用到的列、JOIN 边（左表.列、右表.列、JOIN 类型）、过滤列、GROUP BY 列、规范化后的聚合表达式。
- `readonly_guard(sql) -> GuardResult`：只允许单条 `SELECT`（可以带 `WITH`），其他一律拒绝，并给出原因。
- 解析失败时不能让构建中断：记录哈希、方言、错误类型，然后继续。
- 测试必须覆盖：CTE、子查询、`UNNEST`、窗口函数、`INSERT ... SELECT`、CTAS、`MERGE`、带引号的标识符、三段式表名、带模板变量的文件。

### 6.4 分析（`analysis/`）

- 使用统计：每张表、每一列的使用次数、不同用户数、不同团队数、最近被查询时间。
- JOIN 关系图：按表对和关联列聚合，记录出现次数和加权分数。权重按来源区分：仓库 `.sql` 文件最高，反复执行的临时查询次之，只执行过一次的最低；权重值可配置。
- 血缘：根据 `.sql` 文件中的写入语句（`INSERT`、CTAS、`MERGE`）生成 `lineage_edge`。
- 负责团队推断：根据 schema 或 bucket 命名规则推断（规则可配置），标记为"推断"。

### 6.5 构建命令（`cli.py`）

- `build`：完整构建一次。依次执行：抽取 → 模板处理 → 脱敏和去重 → 解析 → 分析 → 写入目录库 →（阶段 2 起）表卡片 → 索引。
- `refresh`：增量更新。
  - 对比 `information_schema` 和目录库，找出新增、删除、结构变化（结构哈希不同）的表。
  - 只读取上次 `build_run` 之后的历史 SQL，以及上次 commit 之后变化的 `.sql` 文件。
  - 支持 `--tables a.b.c,...` 只更新指定的表。
  - 只为输入有变化的表重新生成表卡片。
- `build` 和 `refresh` 都要能重复执行而结果一致（幂等），并在结束时打印摘要：处理了多少条查询和文件、新增和变化的表、解析失败数。
- 提供 `--dry-run`：只打印将要处理的内容，不写库。

### 6.6 表卡片与检索（`cards/`、`search/`）——阶段 2

- 按使用量选出前 N 张表（N 可配置）；为每张表构建有大小上限的上下文包：表结构、统计、权重最高的 JOIN 边、最多 K 条脱敏后的代表性查询、写入这张表的 `.sql` 片段。
- 用 Pydantic schema 约束 LLM 的结构化输出：`purpose`、`key_columns[{name, meaning, confidence, evidence}]`、`common_usage`、`example_questions`、`overall_confidence`、`evidence`。输出不合法时重试，有次数上限。
- 只有上下文包的哈希变化时才重新生成；人工确认过的字段一律保留。
- 命令行 `review` 子命令：列出待审核的卡片，支持确认、编辑、驳回。
- 混合检索：FTS5 加向量相似度，用 RRF 融合。embedding 模型可配置；如果没有可用的 embedding 模型，退化为只用全文检索，并在日志里提示。

### 6.7 LLM 客户端（`llm/`）

- 薄封装，默认使用 OpenAI 兼容接口；`base_url`、模型、超时都来自配置。
- 包含重试和退避、并发与速率限制、结构化输出校验。
- 日志只记录 token 用量和耗时，不记录提示词或回复内容。

### 6.8 Agent（`agent/`）——阶段 3、4

**流程**：

1. 检索候选表，读取表卡片和关联路径，参考相似的查询模式。
2. 判断把握。以下条件全部满足才生成 SQL：需求里的每个概念都对应到具体的表和列；多张表之间在关系图中有关联路径；没有关键歧义（例如同一个指标有几种口径）。
   - 不满足时进入候选表模式。
   - 判断结果和理由记录在 state 里，供评测使用。
3. 生成 SQL，然后调用 `validate_sql`。失败时根据报错改写，最多重试 3 次（可配置）；仍然失败就进入候选表模式。
4. 把 SQL、用到的表和关联方式、假设与口径、EXPLAIN 结果展示给用户。
5. 用户要求查看样例时：`interrupt` 暂停 → 界面展示 SQL 和行数上限 → 用户确认后调用 `run_sample` → 展示结果。
   - 如果 `samples.send_to_llm` 为 `true`，Agent 可以根据样例检查 SQL 并提出修改。
   - 每次执行都需要用户单独确认。

**工具**（用户身份由 harness 注入，不出现在参数里）：

- `search_tables(query: str, limit: int = 8)`
- `get_table_card(table: str)`：返回内容按可见范围裁剪。
- `get_join_path(tables: list[str])`：只基于关系图，不允许 LLM 猜测关联键。
- `similar_queries(question: str, limit: int = 5)`：只返回所涉及的表用户全部有权限访问的查询模板。
- `validate_sql(sql: str)`：先做只读检查，再以用户身份执行 `EXPLAIN`。
- `check_access(table: str)`：以用户身份对 `SELECT * FROM <table> LIMIT 0` 执行 `EXPLAIN`，返回有权限、无权限或无法判断；用于候选表模式下标注权限。表名必须先和目录库核对，并正确加引号。
- `run_sample(sql: str)`：只接受已经通过 `validate_sql` 的 SQL（用 SQL 的哈希核对）；必须经过 `interrupt` 确认。

**其他要求**：

- 工具出错时把错误作为文本返回给模型，不抛异常。
- 每轮的工具调用步数有上限（可配置，默认 10 步）。
- 系统提示要求：只根据工具结果回答；没有通过 `validate_sql` 的 SQL 不能称为"可执行"；"AI 推断"的描述要明确标出；不确定时直接说明，并提出澄清问题。

### 6.9 界面（`ui/`）——阶段 3、4

- 命令行：`chat` 子命令提供交互式对话；遇到 `interrupt` 时用 `[y/N]` 提示确认。
- Streamlit 单页：对话区、SQL 代码块、"允许读取样例"按钮、样例结果表格。
- 启动时从环境变量读取用户的 Starburst 凭证。

---

## 7. 测试与评测

- **单元测试**：
  - 脱敏是性质测试：输入中的任何常量都不能出现在输出里。
  - 指纹稳定性。
  - 模板处理。
  - 两种方言的 fixture 解析，覆盖 6.3 列出的所有语法。
  - JOIN 边和血缘提取。
  - `builder` 白名单：各种越界语句都被拒绝。
  - `readonly_guard`：写操作、多条语句、注释里藏语句等各种注入样例，要求 100% 拦截。
  - 错误映射，并验证"异常时绝不返回 allowed"。
  - 样例查询的行数限制不能被绕过。
  - `similar_queries` 的可见范围过滤。
  - 用假 LLM 测试 Agent 的模式判断和 interrupt 流程。
  - 增量 `refresh` 能正确识别新增、删除和结构变化的表。
- **安全测试**：
  - 架构约束测试：`agent/` 不能导入 `BuilderClient`。
  - 持久化层中不会出现任何原始常量或样例数据。
- **集成测试（可选，阶段 3）**：用 docker compose 启动一个 Trino 容器，配置文件访问控制规则，模拟两个权限不同的用户，端到端验证 `validate_sql`、`check_access` 和 `run_sample`。
- **评测**：
  - `eval/golden_set.example.yaml` 的格式为 `question`、`user`、`expected_sql` 或 `expected_mode: candidates`、`expected_tables`。
  - `run_eval.py` 输出：SQL 执行正确率、候选表召回率（前 5 名）、模式判断准确率、EXPLAIN 一次通过率。

---

## 8. 阶段与验收标准

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| 0 | 仓库骨架、配置、存储接口和 SQLite 实现骨架、两个 Starburst 客户端的接口（含白名单和只读检查）、fixture、CI 基础（ruff、mypy、pytest）、README 草稿；整理一份需要我确认的问题清单 | 空的 `build --dry-run` 能跑通；检查全部通过 |
| 1 | `sources`、`sqlproc`、`analysis`、`build` 和 `refresh` 命令；基于 fixture 运行，并能在配置好真实连接后运行 | 测试通过；能从 fixture 生成使用量、JOIN 图和血缘的示例报告；`refresh` 能识别新增的表 |
| 2 | 表卡片生成、`review` 命令、全文和向量检索 | 用假 LLM 和真实 LLM 配置各跑通一次；人工确认的字段不会被覆盖 |
| 3 | Agent：生成 SQL、`validate_sql`、候选表模式、命令行对话、评测脚本 | 只读检查 100% 拦截；评测脚本能产出报告 |
| 4 | 样例数据：`interrupt` 确认、`run_sample`、Streamlit 界面、审计 | 没有确认就无法执行；行数限制无法绕过；样例不落盘 |

**本次会话：完成阶段 0 和阶段 1，然后停下来汇报。**

---

## 9. 待确认信息（一律做成配置项，并标 `TODO(confirm)`）

- 历史 SQL 在 Starburst 里的表名、字段和保留期。
- 专用 Starburst 用户能看到哪些 catalog；它是否只能读元数据和历史表。
- Starburst 的连接方式：主机、端口、认证方式（用户名密码、JWT 或其他）。
- GitLab 仓库地址和令牌；`.sql` 文件里用了哪些模板语法。
- LLMaaS 是否兼容 OpenAI 接口、有没有 embedding 模型、限流；允许处理的数据等级（这决定 `samples.send_to_llm` 能否打开）。
- 超时和扫描量上限的合适数值。
- 负责团队的判定规则（schema 还是 bucket 命名，或者另有登记）。
- 用户到团队的映射来源。

---

## 10. 完成标准（每个阶段）

- `ruff`、`mypy`、`pytest` 全部通过；`sqlproc/` 和 `starburst/` 的测试覆盖率不低于 90%。
- README 说明：本地运行方法、全部配置项、安全设计摘要、如何把存储切换到 PostgreSQL。
- 仓库里没有任何密钥，也没有真实的 SQL、真实的元数据或真实的数据。
- 汇报时列出：新增的 `TODO(confirm)` 项，以及你所做的、偏离本说明的决定和理由。
