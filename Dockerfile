#
# pharma-cost-wiki 看板 —— 生产镜像
#
# 设计取舍
# --------
# 1. **不用 node 构建阶段**。前端是 vendor 版（React UMD 本地文件），
#    没有构建步骤——这是刻意的选择，见 server/static/index.html 的说明。
#    好处：镜像更小、构建更快、不依赖 npm（本机 npm 已损坏）。
#
# 2. **记忆（SQLite）挂卷，不烤进镜像**。知识库（wiki/raw）是只读的、
#    可从 git 重建；但**对话记忆不可重建**。两者生命周期不同：
#        镜像重建  → 知识库随代码回来，记忆必须存活
#    所以 DB 路径走 /data 卷（LLM_WIKI_DB=/data/llm-wiki.db）。
#
# 3. **非 root 运行**。看板只读知识库，没有理由给 root。
#
# 4. 基础镜像用 `-slim` 而非 `-alpine`：本项目不依赖编译扩展，
#    Debian slim 的 glibc 与 CPython 官方 wheel 兼容性更好，省去踩坑。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LLM_WIKI_DB=/data/llm-wiki.db

WORKDIR /app

# ---- 依赖单独一层（改动代码不会让依赖层失效，加快重建）----
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- 代码 ----
COPY scripts/   ./scripts/
COPY server/    ./server/
COPY references/ ./references/
COPY SKILL.md INGEST_AGENT.md AGENT_RUNTIME.md README.md ./
# 知识库内容（12 篇词条 + raw 源 + 派生 state + 报告）
COPY wiki/      ./wiki/
COPY raw/       ./raw/
COPY state/     ./state/
COPY reports/   ./reports/

# ---- 记忆卷 ----
# 属主给运行用户，否则首次写入会失败
RUN mkdir -p /data && useradd -m -u 10001 wiki && chown -R wiki:wiki /app /data
USER wiki

EXPOSE 8765

# ---- 健康检查：打真实端点，不是只看端口 ----
# /api/health 会数 wiki 词条、raw 文件、报告数——能真的反映"知识库挂上没有"
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=4).status==200 else 1)"

# 绑 0.0.0.0 才能在容器外访问（服务器/健康检查都在容器内，无暴露风险）
CMD ["python", "server/app.py", "--host", "0.0.0.0", "--port", "8765"]
