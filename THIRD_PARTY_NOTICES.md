# 第三方开源组件与许可证

> 依据赛题「六、6.3 开发约束」：*引用开源代码需明确标注来源与许可证。*

本仓库自有的全部代码为参赛作品；下列组件为**第三方开源**，按各自许可证使用，
版权归原作者所有。前端依赖以**本地 vendor 文件**形式随仓库分发（无需 npm、无需构建）。

## 一、运行时依赖（Python）

见 `requirements.txt`。主要组件：

| 组件 | 许可证 |
|:---|:---|
| FastAPI / Starlette / Pydantic | MIT |
| uvicorn | BSD-3-Clause |
| httpx / requests | BSD-3-Clause / Apache-2.0 |
| python-docx | MIT |
| python-multipart | Apache-2.0 |
| pandas / numpy | BSD-3-Clause |

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

## 四、自研部分说明

本作品**未使用** LangChain / LlamaIndex 等 RAG 框架，也未使用 ChromaDB / FAISS 等
向量库——检索（BM25 + 向量混合、RRF 融合）为自研实现，见 `scripts/retrieval.py`。
知识图谱部分为自研派生视图，见 `scripts/graph_build.py`。

因此除上表所列外，无其他第三方代码引入。
