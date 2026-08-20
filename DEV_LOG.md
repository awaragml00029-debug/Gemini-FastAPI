# 开发日志

## 2026-08-20（下午）第二步：整体迁移到上游基线

分支 `migrate/upstream-20260819`，**未合入 main**。合并上游 `7d1744e`，50 个文件、**+11530 / −1918**。

### 做法：chat.py 取上游整份，再逐条重接

27 处冲突原地解会在最不容易发现错误的文件里留下半合并状态（上游重写了 2802 行）。改为取上游整份作基线，把我们的 delta 逐条重新接线。

### gems 按新契约重写

上游模型系统改为动态 `AvailableModel`，`_get_model_by_name` 已删，`_resolve_model_name` 返回**名字字符串**而非 `Model` 对象。

新的 `_resolve_model_and_gem(pool, name)` 返回 `(resolved_model, gem_id, conversation_key)`：拆别名 → 用上游方式解析基础模型名 → gem 会话用**完整别名**作 LMDB 键，与普通会话隔离。键透传进 `_find_reusable_session` 和两个流式构建器（其 `resolved_model` 参数改名 `conversation_key`，名副其实）。

### 重接的加固（全部仍是 fork 独有）

| 项 | 说明 |
|---|---|
| 输入预处理超时 | `_process_conversation_with_timeout`；新增 `_process_conversation_for_client` 下发账号自己的代理+指纹 |
| 流式空闲超时 | `_stream_with_idle_timeout` + `: ping` 心跳。两个 SSE 消费点学会把 `None` 当"暂无数据"；`_send_and_await_first_chunk` 跳过心跳，保证起始错误仍在响应头提交前暴露 |
| **`request_scope`** | **合并后的树里一处都没有** —— `active_requests` 会恒为 0，auto-close 不等在途请求、池的"忙客户端拒绝重启"永不触发。新增 `_hold_request_scope` 让 scope 覆盖整个流消费期，而不只是发送 |
| `mark_unavailable` | 端点错误路径上恢复 |
| 后台启动 | 保留我们的后台 init（上游改回阻塞，会让慢账号导致服务永不就绪）；吸收上游新增的 `prune_stale_indexes` |
| `pool.py` | 保留我们的 `_restart_client`（拒绝重启有在途请求的客户端、先 close、init 带超时），上游的 `_init_attempt` 三样都没有 |

### 从上游拿来的

`/v1beta` 原生接口、guest/temporary 聊天模式、动态模型注册表、多阶段 Dockerfile、**1726 行测试**。

**上游自己的 SSRF 防护弃用** —— 它只校验初始 URL 且让 curl 跟重定向，公网 URL 仍可 302 进内网；保留我们的逐跳校验。

### 合并中发现并修掉的三个问题

1. `config.py` 自动合并出**重复的 `url_fetch_timeout`**（pydantic 里后者静默覆盖前者）。去重，保留 30s（上游 15s）—— 这是本 fork 实际在跑并验证过的值。
2. `allow_private_url_fetch` 是**死配置**（上游有字段，我们的校验器不认）。已接入 `_validate_remote_url`。
3. 上游新增的 **file 类型 URL 抓取没传代理/指纹**（`client.py`），账号隔离在那里有洞。已补。
4. `lint.yaml` 被上游的 `ci.yaml` 取代（超集，多跑 pytest），删除以免重复；守卫步骤并入 `ci.yaml`。

### 验证

- **上游测试 188/188 全通过**
- `ruff` / `ty` / `pyright` 全清
- 守卫 **26/26**
- 路由含 `/v1/gems` 与 `/v1beta/*`，无 `/v1/v1/health`
- 别名拆分正确（普通模型键用解析名，gem 会话键用完整别名），畸形别名拒绝
- 远程抓取：正常下载通过、超限中途拦截、`127.0.0.1` 与 `169.254.169.254` 拒绝、计时埋点 `perf_counter` 仍为 10

### 产出

镜像 `ghcr.io/awaragml00029-debug/clean:20260820-120423`
digest `sha256:e26942a6f9e8094c626ed18b090be2150484ec08a0a4a63bbd41e6718c50039c`，已推 ghcr，镜像内 22/22 文件与分支 HEAD 一致。**未上线，未合 main。**

回滚：`clean:20260820-113832`（第一步产物）或 `clean:20260805-091753`（当前线上）。

---

## 2026-08-20

上游追赶第一步：只摘不碰模型系统的抓取路径修复，gems 与全部加固零风险。完整迁移留到第二步。

### 上游现状复查

`luuquangvu/Gemini-FastAPI` @ `7d1744e`（8-19）。**落后 28 个提交**（上次记录是 8 个），我们领先 7 个。规模 34 个文件、**+7369 / −1899**，只读试合 **10 处冲突**，承载加固的 6 个文件（`main.py` / `chat.py` / `health.py` / `client.py` / `pool.py` / `helper.py`）全部中招，其中 `chat.py` 变动 **2802 行**，基本重写。

**阻塞项：模型系统被重写。** 上游删除了 `_get_model_by_name`，换成 `_resolve_model_name(pool, name) -> str`（返回字符串，不再是 `Model` 对象），并用动态 `AvailableModel` 取代硬编码 `Model`（`c8e0a83`）。我们的 `_resolve_model_and_gem` 正依赖旧契约 —— **直接合会把 gems 合坏**，必须按新契约重写。

上游仍然**没有**我们任何一个加固符号（8 项全 0），也没有 gems。但它新增了 1726 行测试（`tests/` 5 个文件），第二步可以直接用来验证。

### 上游自己实现了 SSRF 防护（`85d7a89`）

命名不同（`reject_unsafe_url` / `MAX_REMOTE_FETCH_BYTES=20MB`），逐点对比：

| | 我们 | 上游 |
|---|---|---|
| 重定向校验 | `allow_redirects=False` **逐跳校验** | `CurlFollow.SAFE`，**只校验初始 URL**，302 到内网可绕过 |
| 大小限制 | 事后查 content-length + body | **边下边计数**，更好 |
| 超时 | 硬编码 30s | 可配置，更好 |
| 每客户端代理+指纹 | **有** | 无，仍是通用 `chrome` 无代理 |
| `http_version` | 写死 `V3` | `NONE`（QUIC 修复） |

### 本次改动（`f0f4720`）

摘上游三样，保留我们更强的两样：

**摘过来**
- `http_version: V3 → NONE`（上游 `7b2b32f`）。强制 HTTP/3 正是上游 QUIC 空闲超时的成因（`cc837f9`），改为让 curl 协商。这是上游 diff 里对我们价值最高的一项。
- 大小上限改为 `content_callback` **流式执行**，超限中途即断，不再整body缓冲后才拒绝。注意：callback 里抛的异常被 curl 吞掉、只报笼统 `RequestException`，所以用 `oversized` 标志把真实原因带出来。
- 抓取超时从硬编码常量改为 `gemini.url_fetch_timeout`，**默认保持 30s**（上游默认 15），升级后行为不变。

**保留我们的**
- `allow_redirects=False` + 逐跳 `_validate_remote_url`
- 每客户端 proxy / impersonate 下发

**实测**：5430 字节正常下载通过；把上限调到 100 字节能中途断开并抛出 `ValueError("Remote media is too large")`；`127.0.0.1` / `169.254.169.254` / `[::1]` 仍拒绝；`ruff` / `ty` / `pyright` 全清；守卫 **24 项断言**全绿（`REMOTE_FETCH_TIMEOUT_SECONDS` 断言换成 `url_fetch_timeout`，新增 `content_callback` 断言）。

### 产出

镜像 `ghcr.io/awaragml00029-debug/clean:20260820-113832`
digest `sha256:dfe62ea2b7f5c539ff58e8db7faf4b0c57dd07d780f3692870d7a736b00cf648`，已推 ghcr，镜像内 20/20 文件与 `HEAD` 一致。**上线由本人手动执行。**
回滚可选 `clean:20260805-091753`（当前线上）或 `clean:rollback-20260528`。

### 第二步待办（整体迁移）

以上游 `7d1744e` 为基线重建，按难度顺序重接加固：

| 加固块 | 难度 | 原因 |
|---|---|---|
| `request_scope` / `active_requests` / `mark_unavailable` | 低 | 纯 client 生命周期，与模型系统无关，`client.py` 仅改 65 行 |
| `_restart_client`（pool） | 低 | 同上，66 行 |
| `_run_pool_init_in_background`（main.py） | 低 | 启动逻辑，28 行 |
| 抓取加固（逐跳校验 + 每客户端代理指纹） | 中 | 上游已有 SSRF 骨架，我们往上补两块 |
| `_stream_with_idle_timeout` / `_send_stream_with_split` / `_process_conversation_with_timeout` | 高 | 在被重写的 2802 行 `chat.py` 里，流式逻辑全变 |
| gems | 高 | 需按 `_resolve_model_name` 新契约重写 |

好消息：加固挂载的底层函数都还在（`process_conversation`、`process_message`、`_extract_content_and_files`、`save_url_to_tempfile`），是重新接线不是重新设计。

**约定：第二步在新分支上做，不动 `main`，跑通上游 `tests/` 再合。**

---

## 2026-08-05

本轮目标：仓库归位（main = 线上代码）、清理历史分支、建立上游持续追踪、找回被静默丢失的自定义功能。

### 起始状态

- fork 上 12 个分支，`main` 自 2026-04-24 冻在 `6231aa0`，之后四个月的工作全堆在 `sync/upstream-main-*` 系列，从未合回。
- **线上跑的不是 main**：容器 `gemini_rev-gemini-fastapi-1`（端口 8092）实际是 `sync/upstream-main-20260527` @ `0a75b0e`。用容器内 22 个运行时文件逐一哈希比对确认，22/22 一致；对 `origin/main` 则有 10 个不同。
- 镜像 `ghcr.io/awaragml00029-debug/clean:latest` 的 OCI 标签 `revision=135a3636…` 指向 `astral-sh/uv`，是基础镜像继承来的，**不能用它判断代码版本**（该 commit 在本仓库不存在）。
- 无任何 upstream remote，无任何仓库级同步自动化。

### 关键发现：手工同步造成两次静默丢失

每次"同步上游"都是人肉重放 + 手写一个 squash 提交，没有任何自动校验。这个流程已经丢过两批东西：

| 丢失内容 | 写于 | 丢于 | 后果 |
|---|---|---|---|
| `app/server/gems.py` + 模型别名 | — | `sync/upstream-main-20260527` 组装时 | `/v1/gems` 在线上 404 数月无人发现 |
| 远程媒体抓取加固（`781c54b`） | 2026-05-25 | 三天后组装下一分支时 | 线上无 SSRF 防护、无超时、无大小上限、账号身份不一致 |

第二批的账号隔离问题最严重：三个账号各自配了 SOCKS 代理和指纹（chrome146 / firefox147 / chrome145），但媒体抓取路径**从服务器本机 IP 直出、用通用 `chrome` 指纹** —— 同一个会话两个身份。

### 本轮完成

**仓库**（4 个提交，均已推送，`main` = `7f19e4d`）

| 提交 | 内容 |
|---|---|
| `3021fa5` | `upstream-sync.yml` 每日拉上游开 PR（永不自动改 main）+ `check_deltas.sh` 守卫；删除失效的 `track.yml` |
| `8c758b7` | gems 回补（5 个 CRUD 路由 + `<model>-gems-<id>` 别名 + LMDB key 隔离） |
| `6700fa0` | 守卫扩展到稳定性加固（`a32782d`）的 10 个 fork 独有符号 |
| `7f19e4d` | 远程媒体抓取加固回补（SSRF / 超时 / 大小上限 / 逐客户端代理+指纹） |

分支从 12 个清到只剩 `main`，保留 tag `v1.1.0`。

**镜像**：`ghcr.io/awaragml00029-debug/clean:20260805-091753`
digest `sha256:d01c93cadfcdec4b7e0b88d96cefac6129ceb95f8f000839afa163afc5b09b39`，已推 ghcr，由本人手动上线。回滚锚点 `clean:rollback-20260528`（= `b35638741f86`）。

**守卫**：`scripts/check_deltas.sh` 共 23 项断言，已接入 lint CI。任一 fork 自定义资产缺失即 CI 失败。

### 移植时踩到的坑（记录备查）

1. **gems 源要取 `structured-json` 而不是 `origin/main`** —— 前者的 `gems.py` 是新版（3720B vs 2943B），多了 `request_scope` 包裹、loguru 日志、异常不回显 `str(e)`。
2. **不能整份搬 helper.py** —— `65ef702`（5-27）早于加固 `a32782d`（5-28），整份覆盖会抹掉计时埋点。必须逐块合。合并后 `perf_counter`(10) / `download_started`(2) / `decode_started`(2) / `write_started`(2) / `total_elapsed`(1) 计数与原版一致。
3. **不要照搬 `main.py` 的 `include_router(health_router, prefix="/v1")`** —— 基线 `health.py:10-11` 已自带双装饰器，再加前缀会造出 `/v1/v1/health` 废路由。
4. **`curl_cffi_fetch_options` 的取值顺序** —— 原实现是 `self.impersonate or self._cfg_impersonate`，但父类在 `__init__` 就把 `self.impersonate` 设成 `"chrome"`（真值），配置值只有 `init()` 跑完才轮得到。已反转为配置优先。

---

## 待办：上游同步（下次更新）

上游 `luuquangvu/Gemini-FastAPI` @ `7b2b32f`。我们领先 6 个（自定义工作），落后 8 个 —— 但其中 `a1d9c53` 就是我们的 `0a75b0e`（cherry-pick 来的，SHA 不同），**实际新增 7 个**，时间跨度 2026-07-29 ~ 08-01。

### 上游改了什么

**① 只升依赖，零代码（3 个）**

| 提交 | 实际内容 |
|---|---|
| `cc837f9`「Resolve QUIC Idle Timeout」 | fastapi `0.140.13→0.141.1`、uvicorn `0.51.0→0.52.0`。修复在库里 |
| `0327f99` / `62f159b`「dynamic models loading」 | 各只改 `uv.lock` 1 行 |

注意：这几个提交标题写着功能，实际只是版本号，**不要被提交信息误导**。

**② 依赖 + Dockerfile（`ac53ed0`）**

fastapi / lmdb / pydantic-settings / uvicorn 升级，外加：
- `uv sync --no-cache` → `--refresh`
- 新增 `ENV PATH="/app/.venv/bin:$PATH"`
- `CMD ["uv","run","--no-dev","run.py"]` → `CMD ["python","run.py"]`

**③ 真实 bug 修复（2 个）**

- `0a88e67` **Responses API** —— 新增 `_nest_flat_function` / `_flatten_nested_function` 两个 pydantic validator。Responses API 的 tool 定义是扁平的 `{name, description, parameters}`，Chat Completions 是嵌套的 `{type, function:{...}}`，之前缺转换，`/v1/responses` 带 tools 会出错。
- `7b2b32f` **HTTP 版本** —— `CurlHttpVersion.V3` → `CurlHttpVersion.NONE`，媒体抓取不再强制 HTTP/3 改为自动协商；角色映射改查表实现。

**④ 大搬家重构（`926befc`）**

576 行变动看着吓人，主体是搬家：11 个函数从 `chat.py` 移到 `helper.py` 并去掉 `_` 前缀改公开（`calculate_usage`、`process_llm_output`、`convert_to_app_messages`、`build_tool_prompt`、`canonicalize_structured_output` 等），`StructuredOutputRequirement` 移到 `models.py`。

真正的修复藏在类型里：`tools: list[FunctionTool | ChatCompletionFunctionTool | ImageGeneration]` → `tools: list[dict[str, Any]]`，放宽成裸 dict 后 pydantic 不再因 null 字段拒绝请求。

### 与我们的对比

上游**完全没有**我们的任何自定义资产：

| 符号 | 我们 | 上游 |
|---|---|---|
| `_validate_remote_url` / `_is_public_ip` / `MAX_REMOTE_MEDIA_BYTES` / `allow_redirects=False` | 有 | **0** |
| `curl_cffi_fetch_options` | 有 | **0** |
| `_process_conversation_with_timeout` / `_stream_with_idle_timeout` / `_send_stream_with_split` | 有 | **0** |
| `INPUT_PREPROCESS_TIMEOUT_SECONDS` / `STREAM_CHUNK_HEARTBEAT_SECONDS` | 有 | **0** |
| `request_scope` / `active_requests` / `mark_unavailable` / `_restart_client` | 有 | **0** |
| `_run_pool_init_in_background` | 有 | **0** |
| `_resolve_model_and_gem` / `app/server/gems.py` | 有 | **0** |

只读试合 `main <- upstream/main` 结果：**4 个文件冲突** —— `chat.py`、`helper.py`、`pyproject.toml`、`uv.lock`。前两个正是承载全部加固的文件。

### 下次的取舍

| 优先级 | 内容 | 理由 |
|---|---|---|
| **最高** | `7b2b32f` 的 **V3 → NONE** | 只有 1 行，直接命中我们 `helper.py:355`。上游是踩了 QUIC 空闲超时才改的，我们线上仍写死 `V3`，同一个坑埋着 |
| 高 | `0a88e67` Responses API tools 格式转换 | 真 bug，我们也开着 `/v1/responses` |
| 中 | `926befc` 的 `tools` 类型放宽 | 有价值，但捆着巨型搬家重构 |
| 低 | 依赖升级 | 风险低，fastapi 跨两个小版本需验证 |
| **不要** | Dockerfile 的 `--no-cache` → `--refresh` | 与本项目"构建必须清缓存"的规矩冲突 |

**结论：不整体合并，按优先级 cherry-pick，从 V3→NONE 开始。** 每次改完必须跑 `scripts/check_deltas.sh`（23 项断言）确认加固未丢，再出带时间戳的镜像。

### 流程约定

- 镜像必须带时间戳标签，构建用 `--no-cache`。
- 我只负责构建 + 推 ghcr，**上线由本人手动执行**。
- `upstream-sync.yml` 每日 01:30 UTC 跑，只开 PR，永不自动改 `main`；冲突时把上游原样推成分支并打 `conflict` 标签，让冲突暴露在 PR 界面。
