# 第三方开源组件与许可证

> 依据赛题「六、6.3 开发约束」：*引用开源代码需明确标注来源与许可证。*

本仓库自有的全部代码为参赛作品；下列组件为**第三方开源**，按各自许可证使用，
版权归原作者所有。前端依赖以**本地 vendor 文件**形式随仓库分发（无需 npm、无需构建）。

## 一、运行时依赖（Python）

完整清单见 `requirements.txt`。逐项标注如下（版本为本作品开发验证时的版本）：

### 1.1 Agent 编排与 LLM 调用 —— **LangChain 生态**

Agent 编排使用 **LangGraph**（LangChain 家族的 Agent 框架），
LLM 客户端为 `langchain-openai`（OpenAI 兼容协议）。这也是赛题
「6.1 必选技术栈 · RAG 框架：支持 LangChain 等主流框架」的满足方式。

| 组件 | 版本 | 许可证 | 版权 |
|:---|:---|:---|:---|
| `langgraph` | 1.2.11 | **MIT** | Copyright (c) 2024 LangChain, Inc. |
| `langchain-core` | 1.6.3 | **MIT** | Copyright (c) LangChain, Inc. |
| `langchain-openai` | 1.6.2 | **MIT** | Copyright (c) 2023 LangChain, Inc. |
| `langchain` | 1.4.0 | **MIT** | LangChain, Inc. |

- 使用方法：`langgraph.prebuilt.create_react_agent` 构建 ReAct Agent；
  `langchain_core.tools.tool` 装饰工具函数；`langchain_openai.ChatOpenAI` 建客户端。
  使用位置见 `scripts/agent_runtime.py`、`scripts/report_agent.py`、
  `scripts/ingest_agent.py`、`scripts/benchmark_engine.py`。
- MIT 全文：<https://opensource.org/licenses/MIT>

### 1.2 其余运行时依赖

| 组件 | 版本 | 许可证 |
|:---|:---|:---|
| FastAPI | 0.141.1 | **MIT** |
| Starlette | 1.6.0 | **BSD-3-Clause** |
| Pydantic | 2.10.3 | **MIT** |
| uvicorn | 0.52.3 | **BSD-3-Clause** |
| httpx | 0.28.1 | **BSD-3-Clause** |
| python-dotenv | 1.1.0 | **BSD-3-Clause** |
| python-docx | 1.2.0 | **MIT** |
| python-multipart | — | **Apache-2.0** |
| openpyxl | 3.1.5 | **MIT** |
| xlrd | 2.0.2 | **BSD-3-Clause** |
| matplotlib | 3.10.8 | **PSF-based**（matplotlib 自有许可） |
| pandas | 3.0.2 | **BSD-3-Clause** |
| numpy | 2.4.4 | **BSD-3-Clause** |

### ⚠️ 1.3 需特别说明的依赖：PyMuPDF

| 组件 | 版本 | 许可证 |
|:---|:---|:---|
| `pymupdf`（代码中 `import fitz`） | 1.27.2.2 | **AGPL-3.0 或 Artifex 商业许可**（双许可） |

PyMuPDF 是本作品依赖中**唯一的强 copyleft（AGPL-3.0）组件**，因此单独列出：

- **用途**：仅用于 PDF 的**文本提取**（赛题 5.1.2「知识库文档格式支持 PDF」），
  在 `scripts/ingest_raw.py` 中把 PDF 转为可检索文本。
- **调用方式**：以普通 Python 库方式导入调用，**未修改其源码**。
- **影响**：AGPL-3.0 的传染性针对**修改并分发**该库本身的情形；
  本作品未修改 PyMuPDF 源码。若后续需要商用闭源分发，
  应替换为 pypdf 等宽松许可方案，或取得 Artifex 商业许可。
- 许可证全文：<https://www.gnu.org/licenses/agpl-3.0.html>

## 二、随仓库分发的前端库（`server/static/vendor/`）

| 文件 | 组件 | 版本 | 许可证 | 版权 |
|:---|:---|:---|:---|:---|
| `echarts.min.js` | Apache ECharts | 5.5.1 | **Apache-2.0** | Apache Software Foundation |
| `react.js` | React | 18.3.1 | **MIT** | Meta Platforms, Inc. and affiliates |
| `react-dom.js` | React DOM | 18.3.1 | **MIT** | Meta Platforms, Inc. and affiliates |
| `htm.js` | htm | 3.x | **MIT** | Jason Miller (developit) |

- **Apache-2.0** 全文：<https://www.apache.org/licenses/LICENSE-2.0>
  ECharts 的许可证声明随文件保留在其头部注释中（`Licensed to the Apache Software Foundation`）。
- **MIT** 全文：<https://opensource.org/licenses/MIT>
  React 的许可证声明保留在 `react.js` 头部注释中。

ECharts 内部打包了 zrender（同属 Apache-2.0）。

## 三、外部工具（不随仓库分发）

| 工具 | 用途 | 许可证 |
|:---|:---|:---|
| Pandoc | 报告 Markdown → PDF | GPL-2.0+ |
| XeLaTeX / MiKTeX | PDF 排版 | LPPL / MIT 等 |
| matplotlib | 服务端图表重绘 | PSF-based |
| PowerShell + Word COM | 文档渲染验证 | — |

> Pandoc 以**独立可执行程序**方式调用（`subprocess`），未链接、未修改其源码；
> 不使用 Pandoc 时，Word 导出路径仍可单独工作。

## 四、第三方与本作品的边界（重要）

为免误解，明确区分「用了什么」与「自研了什么」：

### 4.1 使用了第三方框架的部分

| 能力 | 第三方组件 | 用途 |
|:---|:---|:---|
| Agent 编排 | **LangGraph**（`create_react_agent`） | 三个 Agent 的 ReAct 循环 |
| Agent 工具定义 | **langchain-core**（`@tool`） | 工具函数声明与 schema |
| LLM 调用 | **langchain-openai** | OpenAI 兼容协议的模型客户端 |

### 4.2 自研的部分（未借助上述框架的现成实现）

| 能力 | 自研文件 | 说明 |
|:---|:---|:---|
| **混合检索** | `scripts/retrieval.py` | BM25（中文 bigram，纯 Python 实现）+ 向量相似度 + RRF 融合（k=60），**未使用 LangChain 的 Retriever 抽象** |
| **向量存储** | 同上 | 手写 JSON 索引（`.index/vectors.json`），**未使用 ChromaDB / Milvus / FAISS** |
| **文档切分** | `scripts/retrieval.py` | 语义边界切分与锚点生成，**未使用 LangChain 的 TextSplitter** |
| **知识图谱** | `scripts/graph_build.py` | 纯脚本从 wiki 表格抽取节点与边，**未使用 GraphRAG / Neo4j** |
| **事实引擎** | `scripts/report_facts.py` | 报告的全部数值（环比/同比/占比/贡献度/预算偏差）由此脚本确定性计算，LLM 不参与算数 |
| **算术复算** | `scripts/check_math.py` | 用 `Decimal` 独立复算 wiki 中的派生值，与声称值比对——**"字面量在 raw 里"和"这个字面量算对了"是两件事**，本脚本查后者 |
| **三级校验** | `check_evidence.py` / `check_math.py` / `check_report.py` | 机械校验器，非 LLM 判定 |

> **一句话**：**框架用于 Agent 编排；检索与知识层是自研的。**
> 赛题 6.1 要求「支持 LangChain/GraphRAG/LlamaIndex 等主流框架」——
> 本作品以 LangGraph 满足该条；赛题同时注明向量数据库「**可选用**」，
> 本作品选择自研实现（理由：可控、可复算、可针对中文专业术语做适配）。

除上表所列外，无其他第三方代码引入。
