# Archive match 改成 agentic RAG：设计

日期：2026-11-19 · 状态：待确认

## 为什么

Archive match（首页 `/`）现在是 classic RAG：代码固定做一次 BM25 检索，取前 7–12 个职位，再调一次模型打分（`resume_match.match_resume`）。按 Week 4 课件第 20–21 页的划分，这不算 agent。课程评分看「为什么需要 agent 而不是脚本」，队友同意改。

Archive 库有 2,228 个带 JD 的职位，一次检索没命中的职位进不了候选，这是 agent 能补的地方：模型自己写搜索词、多搜几次、读过再决定。

## 目标

- 模型自己决定搜什么、搜几次、细读哪些职位，再交一份候选名单（Week 4 课件第 20 页的 agentic RAG）。
- 用户看到的结果格式不变：原文依据、五维雷达图、资格筛选、排序都照旧。
- 原流水线保留，既是 agent 出错时的兜底，也是评测里的 classic RAG baseline。

## 不做

- 反向匹配候选人（`candidate_match`）和 New US jobs 都不动。
- 不改最终输出的格式和校验。
- Archive 的评测任务集另做，不在这次范围内。

## 不能出现的情况

- 网页因为输出格式变化而显示出错。
- agent 编出来的职位编号进入最终结果。
- 一次匹配的花费失控。

## 选定的做法：A（agent 负责找职位，最后打分保持不变）

考虑过的三种做法：

| 做法 | 结论 |
|---|---|
| **A. agent 找职位、交候选名单，再用现有的单次打分步骤生成依据和雷达图** | **选定**。输出不变，校验全复用，改动小 |
| B. agent 从头做到尾，最后自己输出全部依据和雷达图 | 不选。最终输出格式大，证据编号随读过的职位变化，每步都要带着；更易出错、更费 token，校验和网页要大改 |
| C. 每个职位单独调一次模型打分 | 不选。调用次数太多，太贵 |

## 怎么做

### agent 循环（新文件 `archive_agent.py`）

每轮模型输出一个动作（JSON），代码执行后把结果写回历史。循环本身（拼 prompt、预算、token 统计、执行记录、兜底时机）放在共用的 `agent_loop.py`，New US jobs 和 Archive 两个 agent 都用它，各自只提供工具和提交后的处理。

| 动作 | 做什么 |
|---|---|
| `search_jobs(query)` | 用现有的 FTS5 BM25 索引按模型写的搜索词检索，资格筛选和去重照旧，返回前 10 个：编号、标题、公司、地点、筛选结论、命中段落标题 |
| `read_job(job_ref)` | 返回该职位的标题、公司、地点和 JD（长的截中间，和 New US jobs 相同） |
| `submit(job_refs)` | 交候选名单，最好的在前。数量在 top_k 到 shortlist 之间，编号必须是搜到过的、不能重复。不合格就把错误发回给模型重交 |

- 职位编号跨多次搜索保持稳定：第一次搜到时编为 J001、J002……
- 预算（我的取值，可调）：最多 20 步、12 次 `read_job`、15 万 token。
- 模型调用失败、超预算或用完步数 → 退回原流水线，结果里注明 `fallback`。

### 复用和小改

- `SectionSearch.retrieve` 加一个可选参数 `query`：给了就按这段文字算检索词权重，不给就和现在一样从简历里挑。资格筛选仍按简历做。现有调用不受影响。
- `match_resume` 拆成两段：检索 + `score_shortlist(resume, jobs, retrieval, client, top_k)`。原流水线 = 检索 + 打分；agent = agent 名单 + 同一个打分。打分代码一行不改，只是挪进函数。
- 结果里加：`method` 写成 `agent_bm25_then_<provider>`；新增 `agent` 字段（步数、调用次数、token、每步记录、是否兜底）。`usage` 仍是打分那一次调用的（三家模型的 usage 格式不同，硬加容易出错），agent 的 token 记在 `agent` 字段里，网页一起显示。
- `retrieval` 字段在 agent 模式下记录：搜索词列表（每次命中几个）、候选名单、资格画像。

### 入口

- 网页 `/api/match`：配了 API key 时默认用 agent；请求里带 `"method": "pipeline"` 走原流水线（给评测和对比用）。
- 命令行 `match` 加 `--method agent|pipeline`，默认 `agent`。
- 网页上原来的「RAG retrieval」面板，在 agent 模式下改成显示「Agent · N 步」、搜过的词和候选名单；其余结果区不变。

### 测试

用假的模型客户端（不调 API）覆盖：正常的 搜索 → 读 → 提交 → 打分；名单不合格被打回后改正；模型出错和超预算时退回原流水线；`retrieve` 不带 `query` 时结果和现在一样；现有的回归测试全部通过。

## 留给实现时决定

- 搜索结果和 JD 展示的具体文字格式、提示词措辞。
- `.gitignore` 是白名单，新文件 `archive_agent.py` 要加进去。
