#
# pharma-cost-wiki —— 生产镜像（**零数据**）
#
# 核心决定：镜像里不放业务数据
# ----------------------------
# Docker 的镜像层**就是 tar 包**——`docker save` 后 `tar xf` 就能取出其中任何文件。
# 所以"把机密数据烤进镜像"**不等于保密**；一旦推送到公开 registry 就是外传。
# （而且删掉数据的后续层不会清掉更早层里的那份——Docker 经典的密钥泄漏坑。）
#
# ⇒ 本镜像只装**代码 + 工具链**；`raw/ wiki/ state/ reports/ graph/`
#   只建空目录，内容一律靠**运行时挂载**（见 docker-compose.yml）。
#
#   于是同一份镜像可以跑两种数据：
#     公开用户 → 挂载仓库内的**样例**数据
#     自己/评委 → 挂载**真**数据目录
#   而镜像层里**一个字节的业务数据都没有**，可安全推 registry。
#
# ⚠️ 例外：从**真数据目录**构建时若有人手动 COPY 数据，镜像就会带上机密内容。
#    那类镜像**不得推任何公开 registry**。纪律写在 DEPLOY.md。
#
# 其余设计取舍
# ------------
# 1. **不用 node 构建阶段**。前端是 vendor 版（React UMD 本地文件），
#    没有构建步骤——刻意的选择，见 server/static/index.html 的说明。
# 2. **记忆（SQLite）挂卷**：`auth.db` / `users/*.db` **不可重建**，必须独立于镜像存活。
# 3. **非 root 运行**。
# 4. 基础镜像用 `-slim` 而非 `-alpine`：本项目不依赖编译扩展，
#    Debian slim 的 glibc 与 CPython 官方 wheel 兼容性更好。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 数据根：三个库（auth/shared/users）都挂在它下面。挂挂的卷。
    # ⚠️ 是 LLM_WIKI_DATA 而非 LLM_WIKI_DB——后者自三库拆分后就不再参与解析
    #    （paths.py 只在 describe() 里读它做提示），设了也没用。
    LLM_WIKI_DATA=/data

WORKDIR /app

# ---- 系统依赖（单独一层，改动代码不会让它失效）----
# ⚠️ 这一层是"导出功能能不能用"的分水岭，缺一不可：
#
#   fonts-noto-cjk      matplotlib 画图要中文字体。缺了**不会**画成方框，
#                       而是 chart_render._setup_font() 按设计**显式抛错**——
#                       因为"静默画出方框图"比报错更坏（废图会进交付文档）。
#   pandoc              markdown → PDF 的转换器（export_doc.to_pdf 直接 which 它）
#   texlive-xetex       PDF 排版引擎（pandoc 的 --pdf-engine=xelatex）
#   texlive-lang-chinese  xeCJK：中文断行与字体（少了中文会挤成一团或报错）
#
# 代价：镜像约增大 1.5GB。这是"别人 clone 后 Word/PDF 导出开箱可用"的票价。
# 要最小镜像就把这几行删掉——但那时导出功能不可用（其余功能不受影响）。
# ⚠️ 拆成两层 + 加重试，是实测踩出来的，不是洁癖：
#    texlive 那套约 1GB，**经代理下载会抖**——首次构建报
#    `E: Unable to fetch some archives`（apt 默认不重试、超时也短）。
#    拆层的好处：texlive 失败时，前面的字体/pandoc 已经缓存，不必重下。
#    （实测容器里能连到本机代理 127.0.0.1:5479，所以不是代理不通，
#      是"大文件 + 代理"下的超时抖动。）
#
# ---- 层 1：中文字体 + pandoc（小，~50MB）----
RUN apt-get update && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=8 \
        -o Acquire::http::Timeout=90 \
        -o Acquire::https::Timeout=90 \
        fonts-noto-cjk \
        pandoc \
    && rm -rf /var/lib/apt/lists/*

# ---- 层 2：LaTeX（大，~1GB，最易抖的就是它）----
#
# ⚠️ 这两个包都是**实测补上的**，不是可选项——pandoc 的默认 LaTeX 模板
#    依赖它们，缺一个 PDF 就导不出来。两处报错都很"专业"，不看日志根本猜不到：
#
#    lmodern      模板开头就 `\usepackage{lmodern}`
#                 → `! LaTeX Error: File `lmodern.sty' not found.`
#    fonts-recommended  hyperref 要 zapfding 字体（`pzdr`）
#                 → `! I can't find file `pzdr'.` → `! Emergency stop.`
#
#    ⚠️ 而 `texlive-xetex` / `texlive-latex-recommended` **都不含** lmodern——
#       Debian 把它放在**独立的 `lmodern` 包**里（试过 fonts-recommended，里面没有）。
#       `pzdr` 则确实在 fonts-recommended 里。两个都要。
RUN apt-get update && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=8 \
        -o Acquire::http::Timeout=90 \
        -o Acquire::https::Timeout=90 \
        texlive-xetex \
        texlive-lang-chinese \
        texlive-fonts-recommended \
        lmodern \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ----
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- 代码 ----
COPY scripts/    ./scripts/
COPY server/     ./server/
COPY references/ ./references/
COPY SKILL.md INGEST_AGENT.md AGENT_RUNTIME.md README.md ./

# ---- 空的数据目录（内容靠挂载，见文件头说明）----
# ⚠️ 必须显式建：docker 挂载一个**不存在的容器内路径**时，
#    新建出来的挂载点属主是 **root**，而非 root 运行的服务就写不进去
#    （只在写输出目录时才暴露，读的场景看不出来）。
RUN mkdir -p raw wiki state reports graph exports

# ---- 记忆卷 + 运行用户 ----
RUN mkdir -p /data && useradd -m -u 10001 wiki && chown -R wiki:wiki /app /data
USER wiki

EXPOSE 8765

# ---- 健康检查：打真实端点，不是只看端口 ----
# /api/health 会数 wiki 词条、raw 文件、报告数——能真的反映"数据挂上没有"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=4).status==200 else 1)"

# ---- 启动自举 ----
# ⚠️ app.py **没有任何 startup hook**，knowledge.db 也不会自动创建
#    （只有 auth.db / users/*.db 是 lazy 建表的）。
#    没有它，容器起来后：
#      · /api/route 的零 token 直答层整个是死的（router.db_ready() 返回 False）
#      · 问答里的"从关键数值表取数"也用不了
#    所以首次启动先跑 db_build（幂等，已有则更新）。
#    用 shell 形式以便支持 `if`；`exec` 让 uvicorn 成为 PID 1，能正确收信号。
CMD ["sh", "-c", "if [ ! -f /data/shared/knowledge.db ]; then \
      echo '[bootstrap] 知识库镜像不存在，先跑 db_build.py'; \
      python scripts/db_build.py || echo '[bootstrap] 构建失败（缺数据？）——服务继续起，相关接口会返回明确错误'; \
    fi; \
    exec python server/app.py --host 0.0.0.0 --port 8765"]
