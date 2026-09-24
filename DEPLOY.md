# 部署指南

三种方式，按场景选。

> ⚠️ **数据不在镜像里。** 镜像只装代码与工具链（**零业务数据**），
> 数据一律靠**运行时挂载**。原因：Docker 镜像层就是 tar 包
> （`docker save` 后 `tar xf` 即可取出任何文件），把机密数据烤进镜像
> **不等于保密**；而且删掉数据的后续层不会清掉更早层里的那份。
> 详见下面「方式一」与「四、受控交付」。

---

## 方式一：Docker（推荐）

```bash
cd pharma-cost-wiki          # 仓库根
cp .env.example .env         # 按需填 Key；不填也能起（部分功能不可用）
docker compose up -d --build
# → http://127.0.0.1:8765/
```

**数据从哪来**：默认 `WIKI_DATA_DIR=.`，即用**仓库内自带的样例数据**。
要跑**真实数据**，指向真数据目录即可（代码与镜像都不动）：

```bash
WIKI_DATA_DIR=/path/to/pharma-cost-wiki docker compose up -d
# 或写进 .env：WIKI_DATA_DIR=C:/Users/.../pharma-cost-wiki
```

compose 会挂两类目录，**缺一类就有功能坏掉**：

| 类型 | 目录 | 少了会怎样 |
|:---|:---|:---|
| 输入（只读） | `raw/ wiki/ state/ graph/` | 看板没数据、问答没料、图谱空 |
| 输出（可写） | `reports/ exports/` | 生成的报告只在容器里，宿主机看不见 |

⚠️ 逐目录挂载（而非整体挂数据根）是**故意的**：各脚本的
`WIKI_ROOT = HERE.parent` 写死在脚本位置，只有
`graph_build / ingest_tools / state / app.py` 认 `LLM_WIKI_WS`，
所以数据根无法整体挪走——逐目录挂载是唯一**不改代码**的切换方式。

**镜像里装了什么**（决定了哪些功能开箱可用）：

| 装了 | 为了 | 缺了会怎样 |
|:---|:---|:---|
| `fonts-noto-cjk` | matplotlib 画中文图 | Word 导出**显式报错**（不画方框图——废图更坏） |
| `pandoc` + `texlive-xetex` + `texlive-lang-chinese` | PDF 导出 | PDF 导出不可用（Word 仍可） |

代价是镜像约 1.5GB。要最小镜像就删掉 Dockerfile 里那几行，其余功能不受影响。

**首次启动会自举**：容器启动时若 `knowledge.db` 不存在，会先跑
`db_build.py`（幂等）。否则 `/api/route` 的取数层是死的。

常用命令：

```bash
docker compose logs -f          # 看日志
docker compose down             # 停止（**保留记忆卷**）
docker compose up -d --build    # 改代码后重建
docker compose down -v          # ⚠️ 连记忆一起删——不可恢复
```

### 记忆为什么不会丢

```
镜像   = 代码 + 知识库     ← 可重建，`--build` 就回来
卷     = SQLite 记忆库     ← **不可重建**，独立于镜像存活
```

`docker-compose.yml` 里用**命名卷** `pharma-cost-wiki-memory` 挂到 `/data`，
`LLM_WIKI_DATA=/data` 把**数据根**指过去（三个库都挂在它下面）。

**已实测**：写两条消息 + 一条偏好 → `docker compose down` → `up -d` →
再读，**消息与偏好完整存活**。

> ⚠️ 唯一会丢的场景是 `docker compose down -v`（`-v` 删卷）。
> 那是明确的手动操作，不是意外。

### 密钥怎么给

`docker-compose.yml` 用 `env_file` 指向工作区根的 `../../../.env`：

```yaml
env_file:
  - path: ../../../.env
    required: false
```

> **踩过的坑**：Compose **只在 compose 文件所在目录**找 `.env`，
> 而本项目的 `.env` 在三层之上。最初只写 `${VAR}` 插值，结果容器里
> `DEEPSEEK_API_KEY` 是**空的**——更隐蔽的是带 `:-默认值` 的变量
> （如 `BASE_URL`）会悄悄用上默认值，**看起来像配好了**。
> 直到真去对话才暴露。所以这里显式给路径。

`.env` 已在 `.gitignore` 与 `.dockerignore` 里——**密钥不进镜像、不进版本库**。

### 默认只绑回环

```yaml
ports:
  - "127.0.0.1:8765:8765"
```

默认不对局域网暴露。要给别人看，改成 `"8765:8765"`——
但**建议先加反代做鉴权**（见下）。

---

## 方式二：直接跑（无 Docker）

```bash
pip install -r requirements.txt
python server/app.py                 # → http://127.0.0.1:8765/
```

库路径按 `scripts/paths.py` 解析**数据根目录**（data root），三级回退：

| 顺序 | 来源 | 何时生效 |
|:--|:---|:---|
| ① | 环境变量 `LLM_WIKI_DATA` | 部署时的正规入口（指向整个数据根） |
| ② | `/data` | 容器（目录存在且可写） |
| ③ | 仓库内 `data/` | 便携默认 |

三个库都挂在数据根下，**按职责分开**：

| 库 | 路径 | 可否重建 |
|:--|:---|:---|
| 认证目录 | `<root>/auth.db` | 自动创建（`CREATE TABLE IF NOT EXISTS`） |
| 知识库镜像 | `<root>/shared/knowledge.db` | **可重建**：`python scripts/db_build.py` |
| 个人对话 | `<root>/users/<id>.db` | **不可重建**，靠卷存活 |

⚠️ **`LLM_WIKI_DB` 是历史遗留，改它没有任何效果。**
那是"单一数据库"时代的变量；现在 `paths.py` 只在 `describe()` 里读它做提示文字，
**不参与解析**。要切换数据位置，改的是 `LLM_WIKI_DATA`。
（陈旧配置最难查：设了、看着生效、其实被忽略。）

---

## 方式三：给别人临时看（ngrok）

本机有 ngrok（v3.39.9，已配置 authtoken）：

```bash
docker compose up -d
ngrok http 8765          # 输出一个公网 https 地址
```

> ⚠️ **这会把看板暴露到公网。** 当前**没有任何鉴权**——
> 任何拿到链接的人都能看数据、并向 agent 提问（消耗你的 Key 配额）。
> 临时演示可以，**长期别这么干**。要长期公网访问，见下。

---

## 四、受控交付：用**真实数据**起一套给评委看

> ⚠️ 本节只适用于**本次大赛**范围（评委、自己部署、现场演示）。
> 赛题明文：「所有赛题数据仅限本次大赛使用，严禁外传」
> 「不得将数据用于任何商业用途或向第三方传播」。
> 评委属于本次大赛，交付给他们是预期行为；**但不得再转发给任何人**。

做法很简单：**同一份镜像、同一份 compose，只换挂载源**。

```bash
# 在真数据仓库根目录（含 raw/ wiki/ state/ graph/ reports/）
cp /path/to/公开仓库/.env.example .env      # 或沿用你自己的 .env
cp /path/to/公开仓库/docker-compose.yml .
cp /path/to/公开仓库/Dockerfile .
# 需要 scripts/ server/ references/ 等代码，直接从公开仓库 clone 到同级即可
WIKI_DATA_DIR=. docker compose up -d
```

### ⛔ 三条硬纪律

1. **公开仓库构建出的镜像不含数据**——这是设计（`.dockerignore` 把
   `raw/ wiki/ state/ reports/ graph/ exports/ data/` 全部排除出**构建上下文**，
   所以 `COPY` 它们**物理上不可能成功**）。别去改它。
2. **别把数据目录当构建上下文**。有人会想"我把数据放进去就能 COPY 了"——
   那会造出一个**含机密数据的镜像**，它**不得推任何公开 registry**、
   不得转发、不得上传网盘。Docker 的镜像层是 tar 包，谁都解得开。
3. **要给人看，就给"能访问的服务"，不要给镜像文件**。
   临时演示用 `docker compose up` + 隧道（见方式三），
   比传一个 tar 出去安全得多——服务可以随时关，文件出去了收不回。

### 为什么不让真数据进镜像

| 想法 | 为什么不行 |
|:---|:---|
| "设个密码保护镜像" | 镜像不认密码。有 pull 权限的人 `docker save` 就解开了 |
| "在后续层里删掉数据" | 早先的层**仍然含**那份数据，白白泄漏。经典坑 |
| "只给内部 registry" | 凡是有 pull 权限的都是"内部"，但数据保密条款约束的是**数据**，不是你信不信他 |
| "镜像小，无所谓" | 这里卡住的是**保密**，不是体积（真数据才 3.7MB） |

---

## 生产化还缺什么（诚实清单）

这个部署**能跑、记忆不丢、健康检查是真的**，但离"生产"还差几件：

| 缺什么 | 影响 | 怎么补 |
|:---|:---|:---|
| **HTTPS** | 公网明文 | Caddy 一行配置自动签证书；并把 `LW_COOKIE_SECURE=1` 设上 |
| **单进程** | 多人同时提问会排队 | `--workers N`，但**先解决记忆库的并发写**（SQLite WAL 够用，但要注意写锁） |
| **日志只到 stdout** | 无集中收集 | Docker logging driver 或挂载日志卷 |
| **无备份** | `down -v` 就没了 | 定期 `docker run --rm -v pharma-cost-wiki-memory:/d alpine tar czf - -C /d .` |
| **镜像未上仓库** | 每次本地构建 | `docker tag` + `docker push` 到 GHCR/DockerHub（**此镜像零数据，可公开**） |
| **RPA mock 状态不持久** | 容器重启任务清空 | 是 mock 的设计（内存 dict）。要留记录就让别重启它 |

> 鉴权已不是缺口：注册/登录、HttpOnly Cookie 会话、CSRF 三层防护、
> 按用户隔离的数据库都已实现（见 `README.md`）。

反代示例（Caddy，自动 HTTPS）：

```caddyfile
wiki.example.com {
    basicauth {
        admin $2a$14$<bcrypt 哈希>
    }
    reverse_proxy 127.0.0.1:8765
}
```

---

## 本机踩过的坑（部署相关）

1. **Docker 守护进程没起** → `docker info` 报 `npipe://...` 找不到。
   启动 Docker Desktop 即可（本机 10 秒就绪）。

2. **`Dockerfile` 的 `COPY` 不支持 shell 重定向**。
   写成 `COPY reports/ ./reports/ 2>/dev/null || true` 会报
   `failed to calculate checksum ... "/||": not found`。
   要么保证目录存在（本项目是），要么用通配符。

3. **本地代理会干扰**。本机有 `HTTP_PROXY=127.0.0.1:5479`，
   `curl` 访问 localhost 返回 **502**——必须加 `--noproxy '*'`。
   > **502 未必是服务没起来，先查代理。**

4. **`.env` 找不到**（见上）。Compose 的 `.env` 查找范围是 compose 文件目录，
   不是执行目录，也不是父目录。

---

## 验证部署是否正常

```bash
# 1. 容器健康
docker compose ps

# 2. 端点全绿（11 个）
curl -s --noproxy '*' http://127.0.0.1:8765/api/health | python -m json.tool

# 3. 知识库规模对得上
docker exec pharma-cost-wiki python -c "
import sys; sys.path.insert(0,'scripts')
import paths; from agent_memory import Memory
print(paths.describe(paths.default_db()))
print(Memory().stats())"

# 4. 记忆能持久（最强验证）
docker exec pharma-cost-wiki python -c "
import sys; sys.path.insert(0,'scripts')
from agent_memory import Memory
m=Memory(); u=m.ensure_user('smoke'); c=m.new_conversation(u,'smoke')
m.append(c,'user','hello'); print('写入', m.stats())"
docker compose down && docker compose up -d && sleep 10
docker exec pharma-cost-wiki python -c "
import sys; sys.path.insert(0,'scripts')
from agent_memory import Memory
print('重建后', Memory().stats(), '← messages 应 ≥1')"
```
