# 💊 PharmaCost-AI: 制药企业产品成本智能分析与报告系统

> 🏆 **2026年第二届重庆市AI大模型创新应用大赛企业出题赛项参赛作品**  
> *基于「RAG 检索增强 + 多维成本归因 + 对标三步法 + RPA 整改闭环」的企业级智能财务分析工作台。*

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version">
  <img src="https://img.shields.io/badge/Framework-FastAPI%20%7C%20React%2018-green?logo=fastapi" alt="Framework">
  <img src="https://img.shields.io/badge/LLM-DeepSeek%20%7C%20Qwen-orange" alt="LLM">
  <img src="https://img.shields.io/badge/Architecture-llm--wiki%20%2B%20RAG-purple" alt="Architecture">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker">
  <img src="https://img.shields.io/badge/License-Apache%202.0-lightgrey" alt="License">
</p>

---

### 💡 核心设计哲学
> **「大模型不该算数，它该写文章。」**  
> 凡涉及精确统计、环比波动、结构拆解、合规校验等确定性计算，100% 由底层分析引擎硬编码复算；LLM 的核心职责严控于「深度因果推演、专业分析叙述与改进建议生成」，杜绝商业报告中的数据幻觉。

📢 **评委极速测评入口**：请查阅 👉 [【评委使用与运行说明指南】](评委使用与运行说明指南.md)（含 30 秒 Docker 真实数据挂载与自动化验收指引）

⚠️ **赛题数据不随本仓库分发**：依据赛题保密条款，出题方提供的原始敏感业务数据不进公开仓库，详见 [`DATA_NOTICE.md`](DATA_NOTICE.md)。  
📚 **核心规则与约定**：规则见 [`SKILL.md`](SKILL.md) ｜ 词条骨架见 [`references/词条骨架.md`](references/词条骨架.md)。

---

## 🏛️ 系统核心四大模块

- 📊 **单品成本看板与波动归因**：支持 6 个月历史趋势折线、瀑布图贡献度分解、环比变动（±10%）阈值告警。
- 📑 **智能分析报告一键导出**：融合企业知识库与实时数据，动态解析 Word 模板，支持 Word / PDF 专业排版双格式导出。
- ⚖️ **对标分析「三步法」引擎**：找差异 → 拆结构 → 拆原因，快速定位同集团多厂（中药一厂 vs 中药二厂）成本差异根因。
- 🤖 **RPA 整改任务闭环**：异常归因直达行动，自动生成结构化任务 JSON 并对接模拟 RPA 与微信推送。

---

## 📖 llm-wiki 知识引擎架构

本系统采用 **llm-wiki** 构建证据可查的制药行业专属知识库，核心承诺：
> `wiki/` 里每一个数字、日期、直接引语，都能在 `raw/` 原始材料中找到**坐标级**出处，作为报告生成 Agent 可信事实的唯一来源。

## 目录结构

```
pharma-cost-wiki/
├── SKILL.md                      ← 规则层（铁律 + 领域硬约束）。改规则要人批准。
├── INGEST_AGENT.md               ← 摄入 agent 提示词（无记忆，冷启动读规范）
├── README.md                     ← 本文件。
├── inbox/                        ← 待摄入投放区（拖文件进来即可）
├── references/
│   ├── schema.yaml               ← 机器可读 schema（目录/主题/元数据/链接/hook）
│   ├── 命名与链接约定.md          ← 主题决策表、命名、**链接深度对照表**、日志格式
│   └── 词条骨架.md               ← A–G 七类词条的编译骨架
├── scripts/
│   ├── ingest_raw.py             ← **新文件采集器**（多格式 → raw/，产出编译简报）
│   ├── hooks.py                  ← hook 契约（CLI/inbox/**网站上传**共用）
│   ├── check_evidence.py         ← 机械证据校验器（7 类，只报不改）
│   ├── check_math.py             ← 算术复算（复算 wiki 派生值）
│   ├── check_report.py           ← 报告合规性校验
│   ├── finalize.py               ← 确定性收尾流水线（state→DB→校验→算术）
│   ├── report_facts.py           ← 报告事实引擎（确定性取数，带溯源坐标）
│   ├── report_build.py           ← 6 章报告生成（数字归脚本，文字归 LLM）
│   ├── agent_runtime.py          ← **主 agent 运行时**（记忆+压缩+工具+事件流）
│   ├── agent_memory.py           ← 记忆层（写 SQLite 权威区）
│   ├── agent_compact.py          ← 上下文压缩（切点/摘要/token 估算）
│   ├── agent_tools.py            ← 工具层（截断/错误回喂/循环防护/钩子）
│   ├── demo_web_upload.py        ← 网站/后台集成示例
│   └── test_agent_retrieval.py   ← Agent 检索能力实测
├── server/                        ← 看板后端（FastAPI，只读）+ vendor 版前端
│   ├── app.py                    ← 只读 API（复用 report_facts，自己不重算）
│   ├── static/                   ← React 18 + htm（本地 vendor，无需 npm）
│   └── requirements.txt
├── reports/                       ← 生成的 6 章月度报告
├── raw/                          ← 源材料层（权威件不可变）
│   ├── csv/cost_data/            成本数据
│   ├── csv/market_data/         行情与行业基准
│   ├── pdf/pharma_docs/          制药知识 PDF + 同名 .txt
│   ├── templates/                报告模板
│   ├── rpa/                      RPA 接口文档与 mock 服务
│   ├── meta/                     数据包元信息
│   ├── docs/ · tabular/          新文件的兜底落位区
│   └── .hashes.json · .ingest-audit.jsonl   ← 去重索引 / 采集审计
└── wiki/                         ← 编译知识层（12 篇词条）
    ├── index.md                  全局索引（从这里开始）
    ├── log.md                    操作日志
    ├── products/ · costs/ · equipment/ · market/ · gmp/ · benchmarking/ · interfaces/
```

## 摄入新文件

三种入口，**共用同一个采集函数**——入口变了，规范不变。

```bash
python scripts/ingest_raw.py <文件>              # 单个
python scripts/ingest_raw.py inbox/              # 目录（递归）
python scripts/ingest_raw.py --inbox             # 投放区批量
python scripts/ingest_raw.py <文件> --json       # 输出编译简报
```

支持 `.md .txt .csv .xlsx .xls .docx .pdf .py .json`。
二进制格式（PDF/Word/Excel）会自动生成同名 `.txt` 派生文本供 grep 校验。

采集完成后，把**编译简报**交给 [`INGEST_AGENT.md`](INGEST_AGENT.md) 定义的摄入 agent
完成 分诊 → 编译 → 级联 → 收尾。

**网站拖拽 / 管理后台**接入见 [`scripts/demo_web_upload.py`](scripts/demo_web_upload.py)：

```python
from ingest_raw import ingest_file
from hooks import CallbackHook

r = ingest_file(uploaded_bytes, filename=name, hooks=[CallbackHook(on_upload)])
```

## 怎么用

**把它跑起来** —— 见 [`官方运行指南.md`](官方运行指南.md)：从取代码、投放赛题数据包、
构建知识库到启动服务，四步走完。也可以只读 [`交付说明.md`](交付说明.md) 了解
本作品对照赛题要求的完成情况。

**作为报告生成 Agent 的知识源** —— 按 `wiki/index.md` 找词条，用**行内证据坐标**
（如 `[成本汇总_2026.csv:单位成本(元/盒):银黄&2026-05]`）回溯到具体列与筛选条件。

**查一个问题** —— 读 `index.md` 定位，再读对应词条。

**加一份资料** —— 按 `SKILL.md` 第五节的流程走：采集 → 分诊 → 编译 → 级联 → 记日志。

## 12 篇词条导航

| 词条 | 什么时候看 |
|:---|:---|
| [成本数据口径与结构](wiki/costs/成本数据口径与结构.md) | **动任何数字之前先看这个**——4 个口径陷阱、5 条自检恒等式 |
| [三产品成本基线](wiki/costs/三产品成本基线.md) | 取成本数值 |
| [成本异动登记册](wiki/costs/成本异动登记册.md) | **做归因分析之前必看**——官方根因、归因操作准则、禁止事项 |
| [银黄口服液](wiki/products/银黄口服液.md) / [板蓝根颗粒](wiki/products/板蓝根颗粒.md) / [六味地黄胶囊](wiki/products/六味地黄胶囊.md) | 查配方、工艺、物料构成 |
| [设备台账与维修历史](wiki/equipment/设备台账与维修历史.md) | 查设备编号、折旧、故障记录 |
| [药材行情与行业基准](wiki/market/药材行情与行业基准.md) | 做行情引用 / 行业分位定位 |
| [GMP质量约束与成本关联](wiki/gmp/GMP质量约束与成本关联.md) | 写整改建议时找 GMP 依据 |
| [一厂vs二厂对标分析](wiki/benchmarking/一厂vs二厂对标分析.md) | 做对标 |
| [RPA接口契约](wiki/interfaces/RPA接口契约.md) | 对接下游工单系统 |
| [报告模板契约](wiki/interfaces/报告模板契约.md) | 生成报告时对照验收 |

## 三条最容易踩的坑

1. **单位**：成本汇总一律 `元/盒`；行业基准是 `元/支|袋|粒`。**严禁擅自换算。**
   （1 盒 = 10 支 / 20 袋 / 60 粒）
2. **设备编号**：真实格式 `EQ-TQ-001`。**不存在 `TQ-01`。**
3. **4-6 月银黄口服液的根因是「提取工艺收率波动」，不是设备故障。**
   维修历史里 2026 年只有 3 月的胶囊填充机一条，提取罐全年无故障。

## 已知的源数据问题（12 处）

本库在交叉核对时发现 **12 处 raw 自身的不可自洽**——不是本库写错，是原始数据矛盾。
全部以 `> **Status: Disputed**` 块登记在对应词条，并附「工程判定」说明采信哪一方。

这些登记的价值：**让 Agent 不去踩坑**。例如官方说"金银花涨 12%"，但任何可复现口径都算不出 12%——
报告若照抄就会引用一个无法验证的数字。

汇总表见 [index.md](wiki/index.md) 末节。

## 多模型路由（赛题加分项）

```bash
python scripts/db_build.py                  # 先建知识库镜像（路由从库取数）
python scripts/router.py --selftest         # 看路由裁决（不调模型）
python scripts/router.py --ask "二厂单位成本"
```

三层成本阶梯，**顺序不可颠倒**：

| 档位 | 何时用 | 成本 |
|:---|:---|:---|
| ① **库直答** | 提问命中**唯一**数值 → 脚本直接答 | **0 token** |
| ② **小模型**（Qwen2.5-7B） | 只读 / 单源 / 问答 | 便宜 |
| ③ **大模型**（deepseek-chat） | 写入 / 跨源 / 报告 / RPA / 需推理 | 贵 |

> 多数人只想到 ③（换便宜模型）。其实 **① 和 ② 省得多得多**——
> 成本大头是**上下文长度**，不是模型档位。

**两条底线：**

1. **路由是脚本，不是模型。** 只管确定性信号（写入意图 / 库能否直答 /
   数据源数量 / 输出类型），每个决策都带 `reasons` 可打日志。
   把"要不要用大模型"交给 LLM 判 = 不可复算的判断，判错只会**悄悄变差**。
2. **小模型必须过校验，不过就升级。** 校验抓两类失败：
   - **幻觉**：答案里的数字在参考数据里查不到 → 升级
   - **假阴性**：参考数据明明有，却答"没有该数值" → 升级
   （第二类是实测发现的：Qwen2.5-7B 面对库里有 6 行数据的提问仍会拒答。）

**写权限不给弱模型**：小模型只读；`mirror_metrics` 以 `mode=ro` 打开。
库若**落后于 wiki**（签名不符）直接拒绝取数——「正确但过时」是最难发现的错。

## 主 agent（记忆 / 压缩 / 工具）

```bash
python scripts/agent_runtime.py -q "银黄口服液 5 月为什么涨？"
python scripts/agent_runtime.py --user demo --set-pref default_product '"银黄口服液"'
python scripts/agent_runtime.py --user demo --show-memory
```

三层记忆（会话历史 **永不删除** / 压缩摘要 / 跨会话偏好），
上下文压缩（**切点只在消息边界**，摘要保留数值与坐标），
工具层（截断**可续读**、错误**回喂**不抛出、**循环防护**）。

> 循环防护是本库特有的补丁：实测有过 agent **连查 58 次全部落空、
> 零产出后崩溃**的先例。现在同样参数重复第 4 次即拦截。

设计说明（含与 Pi agent 的取舍差异）见 [`AGENT_RUNTIME.md`](AGENT_RUNTIME.md)。

## 看板（前端）

```bash
pip install -r server/requirements.txt
python server/app.py              # → http://127.0.0.1:8765/
```

打开后在首页**注册 / 登录**即可使用问答、上传、导出等功能。
图表、知识图谱、数据说明等**公开数据无需登录**就能看。

六个页签（首页把「问答」「上传」整合进来了，不再是独立页签）：

| 页签 | 作用 |
|:---|:---|
| **首页** | ① 智能文档（对话）② 拖拽上传 ③ 成本分析图表 + 知识图谱 |
| **成本趋势** | 逐月折线 + 明细表 |
| **对标分析** | 三步法引擎的差异与归因 |
| **数据说明** | 资料口径矛盾（按影响分级）——**只呈现，不改数** |
| **更新记录** | `log.md` 时间线 |
| **整改闭环** | 报告第六章的任务下发到责任人 + 任务追踪 |

### 用户与隔离

| 项 | 说明 |
|:---|:---|
| 注册 | 用户名 + 密码（≥8 位）。用户名 3–32 字符，中文/字母/数字/下划线 |
| 会话 | 服务端 session + **HttpOnly Cookie**（JS 读不到，刷新不掉线） |
| 密码 | 标准库 **PBKDF2-SHA256**（60 万次迭代），不引第三方依赖 |
| 数据隔离 | **一人一个目录** `data/users/<用户名>/`，物理分开 |
| CSRF | SameSite=Lax + Origin 校验 + double-submit token |
| 防爆破 | 连续失败 5 次锁 5 分钟 |

命令行/脚本仍可用 `python scripts/agent_auth.py --issue <用户>` 签发令牌，
带 `Authorization: Bearer <令牌>` 访问（认证走两路，行为一致）。

```bash
python scripts/agent_auth.py --users          # 列出用户
python scripts/agent_auth.py --set-pw alice   # 给已有用户补设密码
```

> ⚠️ **生产部署必须设 `LW_COOKIE_SECURE=1`** 让 cookie 带 `Secure`
> （本地 HTTP 开发时不能开，否则 cookie 发不出去）。

- **知识库只读**：看板不改共享 `wiki/` 或 `raw/`。写操作只有两处，都与知识库隔离：
  ①「问答」写 **agent 自己的记忆**（SQLite 权威区）；
  ②「控制面板」写 **上传者的个人工作区**（`data/users/<id>/`，他人不可见）。
- **前端已 vendor**（React 18 + htm 本地文件，约 146KB）——**无需 npm、无需构建步骤**。
  克隆下来 `pip install` 就能跑。

## 校验

```bash
python scripts/check_evidence.py .     # 机械校验（7 类，只报不改）
python scripts/check_math.py           # 算术复算（复算 wiki 里的派生值）
python scripts/check_report.py <报告>  # 报告合规性（模板契约 §8）
python scripts/finalize.py             # 确定性收尾：state → DB → 机械校验 → 算术复算
```

**为什么校验要分三层**——因为它们能查的东西**互不重叠**：

| 校验器 | 查得出 | 查不出 |
|:---|:---|:---|
| `check_evidence` | 字面量在不在 raw、死链、索引缺失、换行符卫生 | **算错**（`0.71 ÷ 17.38` 算式对、结果舍入错） |
| `check_math` | 派生值算得对不对 | 归因是否合理、断言范围是否夸大 |
| `check_report` | 章节/表头/占位符/RPA 字段合规 | 内容质量 |

实测教训：六味同比写成 `+4.08%`（应为 `+4.09%`）**只有 `check_math` 抓得到**，
`check_evidence` 完全看不见——因为 `52%`/`0.71` 这些**字面量确实在 raw 里**。
**"字面量在 raw 里" ≠ "这个数算对了"。**

可疑点只是候选，判读真伪是人的责任——**已知假阳性类别**见 `SKILL.md` 7.2 节。

## 当前状态

- **建立日期**：2026-09-20
- **raw/**：32 个文件（25 个源 + 7 个 PDF 文本提取）
- **wiki/**：12 篇词条，覆盖全部 25 个源文件
- **核验**：全部关键数字已复现；发现并登记 12 处源数据不一致
- **未覆盖**：GMP 全文（93 页）仅提取结构并复用了摘要的成本关联，未逐条通读

## 与其他目录的关系

| 目录 | 说明 |
|:---|:---|
| `../pharma-wiki/` | 早期骨架（`编译约束规范.md` + `raw_extractor.py`）。本库的 6 类词条骨架源自它，并新增 G 类补其缺口。**保留未动。** |
| `../agents/` | LangChain wiki 检索工具（`create_wiki_tools`）。可作为消费本库的读取层。 |

