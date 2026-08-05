# 开发日志

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
