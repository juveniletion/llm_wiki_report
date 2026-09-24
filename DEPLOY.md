# 部署指南

三种方式，按场景选。**都已在真机上验证过。**

---

## 方式一：Docker（推荐）

```bash
cd project-report-agent/llm-wiki/pharma-cost-wiki
docker compose up -d
# → http://127.0.0.1:8765/
```

就这两行。已实测：

- 镜像构建成功（`pharma-cost-wiki:latest`）
- 容器 `Up (healthy)`，健康检查打的是**真实端点**而非只看端口
- 12 篇词条 / 32 个 raw 文件 / 3 份报告都在容器内可见
- 容器内 LLM 问答跑通（2 次工具调用，答案带证据坐标）

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
`LLM_WIKI_DB=/data/llm-wiki.db` 指过去。

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

记忆库路径按 `scripts/paths.py` 的**三级回退**解析：

| 顺序 | 来源 | 何时生效 |
|:--|:---|:---|
| ① | 环境变量 `LLM_WIKI_DB` | 部署时的正规入口 |
| ② | `/data/llm-wiki.db` | 容器（目录存在且可写） |
| ③ | 仓库内 `state/llm-wiki.db` | 便携默认 |
| ④ | `F:\llm-wiki.db` | 本机既有数据（仅当该盘存在） |

③ 优先于 ④ 是**刻意的**：让"克隆下来就能跑"比"沿用旧路径"更重要；
而 ④ 保留是为了本机**已有的对话记忆不会因为这次改动而丢失**。

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

## 生产化还缺什么（诚实清单）

这个部署**能跑、记忆不丢、健康检查是真的**，但离"生产"还差几件：

| 缺什么 | 影响 | 怎么补 |
|:---|:---|:---|
| **无鉴权** | 谁都能访问、都能烧 Key | 前面挂反代（Caddy/Nginx）加 Basic Auth，或给 `/api/chat` 加 token |
| **单进程** | 多人同时提问会排队 | `--workers N`，但**先解决记忆库的并发写**（SQLite WAL 够用，但要注意写锁） |
| **HTTPS** | 公网明文 | Caddy 一行配置自动签证书 |
| **日志只到 stdout** | 无集中收集 | Docker logging driver 或挂载日志卷 |
| **无备份** | `down -v` 就没了 | 定期 `docker run --rm -v pharma-cost-wiki-memory:/d alpine tar czf - -C /d .` |
| **镜像未上仓库** | 每次本地构建 | `docker tag` + `docker push` 到 GHCR/DockerHub |

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
