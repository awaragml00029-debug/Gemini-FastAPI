# 开发日志

## 2026-10-26 同步上游 66dc3f7 → 86e192a（Python 3.14、gemini-webapi 42f89a7、auto_delete）

分支 `migrate/upstream-20261025`，在 `583948e` 之上 4 个提交 + 开启 auto_delete，**已 fast-forward 合入 `main`（`99f4d1e`）并推送；镜像已推 ghcr；上线待本人在 Portainer 操作**（见本节「镜像与发布」）。

### 上游改了什么（13 个提交，真正动代码的 3 个）

| 提交 | 内容 |
|---|---|
| `2e719d6`（10-01） | gemini-webapi 从 PyPI 2.1.1 改成 git 源 `luuquangvu/Gemini-API@42f89a7`；builder 镜像装 `git`；`chat.py` 删掉 instruction 的 `type != "message"` 过滤 |
| `1070d08`（10-14） | Python 3.13 → **3.14**；ruff 开 `D`（google docstring）并去掉 `E501` 的 ignore；全仓补 docstring、长行换行；依赖整体升级（fastapi 0.143.3、uvicorn 0.54.0、lmdb 3.0.0…） |
| `dc0f1af`（10-17） | 新 `chat_mode: auto_delete`：会话过期后**同时删 Google 账号里的会话和本地 LMDB**。默认仍是 `normal`，不改配置不生效 |
| 其余 10 个 | dependabot 升 action 版本、merge 提交 |

**库 `42f89a7` 的来历已核实**：= v2.1.1 + 1 个提交（网络瞬断重试 + cookie 缓存按 base PSID 固定，防 PSID 轮换后缓存分叉）。该提交已作为 PR #361 合入官方 `HanaokaYuzu/Gemini-API` master（10-08），但**还没有 PyPI 发布、也没有 tag**。

### 合并：12 处冲突，全部保留加固

`main.py` 3、`chat.py` 4、`client.py` 2、`helper.py` 2、`config.yaml` 1，外加 `track.yml`（我们删、上游改 → 保持删除）。原则：**冲突处一律保留我们的加固，只吸收上游的措辞/换行**。

- `chat.py`：保留瞬断/拒答重发循环、回退路径里的 `_process_conversation_for_client` 与 `request_scope`、`![Image]` 替代文本（防 Google 看到它取不到的文件名）
- `helper.py`：保留软化版工具提示词（`11dcba0`），上游只是换行
- `config.yaml`：保留 `url_fetch_timeout: 30` 和重启参数，加入上游 auto_delete 说明
- `main.py`：保留我们的后台 init / 恢复任务的关停顺序，再加上游的 `cleanup_task.cancel()` —— 放在 `pool.close()` **之前**，因为 auto_delete 会从清理任务里调 pool

### 两处「自动合并干净、运行时才炸」的坑（已修 + 测试 + 守卫）

1. **`close()` 覆写 × 新库 `save_cookies` 签名**。新库把签名改成 `save_cookies(cookies, base_psid=None, verbose=False)`，我们的覆写还是按位置传 `self.verbose`。`verbose` 默认 `true` → `base_psid=True` → 缓存路径哈希里 `True.strip()` 抛 `AttributeError`。而新库的 `@running` 重试**每次重试都会调 `close()`**，正好落到这里。已改为关键字传参并固定到 `_base_psid`，同时补上新库 `close()` 的「无 PSID 不写缓存」守卫。
   证据：签名由 `inspect.signature` 在装好的新库上实测确认；崩溃路径是读代码推出来的。
2. **`pool.delete_chat()` 调了不存在的方法**。上游代码调 `self._ensure_client_ready()`，我们重写过的 pool 没有这个方法 → `AttributeError` 被 `except Exception` 吞掉 → 每次远程删除都「失败」。改法遵守我们的加固：**不在这里重启 client**（重启只由恢复任务做），client 不可用或已退役就记一次失败、下轮再试（最多 5 次）；删除放进 `request_scope()`，后台重启和 auto-close 不会中途掐断。

新增 `tests/test_cookie_cache_pinning.py`、`tests/test_remote_chat_deletion.py`；逐条把修复撤掉验证过这些测试确实会红。`check_deltas.sh` 加 2 条断言（拿纯上游代码跑，两条都 FAIL）。

### 加固完善：响应格式 JSON Schema 的远程 `$ref` SSRF

全量测试卡死，faulthandler 栈：`canonicalize_structured_output` → jsonschema `_warn_for_remote_retrieve` → `urlopen` → `socket.create_connection`。

`_bounded_validator_for()` 构造校验器时没给 registry，jsonschema 就退回到已废弃的默认行为：**远程 `$ref` 用裸 `urlopen` 去抓，没有超时、没有地址校验**。`response_format` 是 API 调用方给的，所以 `{"$ref": "http://169.254.169.254/latest/meta-data"}` 会让服务器去请求云元数据地址（盲 SSRF），对方不应答就把请求永久挂住。上游自己的测试用的正是这个 schema，只在 connect 很快失败的机器上才能通过。这段代码是 `1115894` 随 08 月迁移进来的，**main 上一直存在**。

修法：传一个空的 `referencing.Registry()`。它从不自动抓取（python-jsonschema #1065），本地 `$ref`（`$defs`、draft-04 `definitions`、指回自身 `$id`）照常解析，远程的立刻报错，落进已有的「不校验、不判失败」分支。实测（jsonschema 4.26.0 / referencing 0.37.0）：`urlopen` 调用 0 次、耗时 0.0s；去掉 `registry=` 新测试即变红。守卫 +1 条。

### main 上已有、这次顺带查清的问题

1. **两个断连测试必挂**（`test_stream_disconnect_cancels_pending_media_tasks` 及 Responses 版）。四组对照（各 3 次）：我们的 main 在 py3.13+库 2.1.1、py3.14+库 2.1.1 都挂，纯上游通过 → 与合并、Python 版本、库版本都无关。`git bisect` 定位到 `583948e`（09-09「拼接回复」修复）。原因是**测试夹具不真实**：后续 chunk 的 `.text` 只有 `"more"`，而真实库的 `.text` 总是累计全文，`_delta_continues` 正确地把它们当「回复被替换」全部丢弃（6 秒 51 条警告），测试就永远等不到第三帧。**加固本身是对的、不动**；夹具改成累计文本后 10/10 通过。
2. **main 的 CI 自 09-09 起一直红**：`ruff format --check` 会格式化 `DEV_LOG.md` 里的 Python 代码块，三处手工对齐的注释不合格。
3. **`Sync Upstream` 自 10-01 后连续失败 24 次**，所以这次更新没有自动 PR。原因：有冲突时工作流把上游原样推成分支，而上游提交改了 `.github/workflows/ci.yaml`，GitHub 不允许 `GITHUB_TOKEN` 推送工作流文件（需要带 `workflow` 权限的 PAT）。**这是仓库凭据问题，本次未处理，待本人决定。**

### lint 收尾

上游新开的 docstring 规则在我们 fork 独有代码上报 115 处：插入 101 条一行 docstring（只增不删），10 处长行换行（逐文件比对字符串常量多重集，前后完全一致），`DEV_LOG.md` 代码块只改空白。另修 `test_refusal_retry.py` 的 8 个类型错误（main 上就有）。

### 验证

| 项 | 结果 | 证据等级 |
|---|---|---|
| pytest | **466 passed**，无任何 deselect，约 8s | 实测 |
| CI 七步（守卫 / uv sync / ruff check / ruff format / ty / pyright / pytest），干净 worktree 复跑 | 全部 PASS | 实测 |
| 守卫 `check_deltas.sh` | **42/42** | 实测 |
| 新库 `save_cookies` 签名 | `(cookies, base_psid=None, verbose=False)` | 实测 |
| 与真实 Google 的交互（新库重试、auto_delete 远程删除） | **未验证** | 需上线后看日志 |

### 开启 auto_delete（本人决定，`99f4d1e`）

`config.yaml` 的 `chat_mode` 改为 `"auto_delete"`，TTL 保持默认 86400s。生产容器没有 `CONFIG_GEMINI__CHAT_MODE` 环境变量，所以镜像里的 `config.yaml` 生效。读代码确认的行为：

- 新会话仍是普通 Google 会话。最后一次活动 24h 后从 LMDB 删除并排队删 Google 端；清理每 6h 一轮，实际 24~30h；删除失败最多重试 5 次
- **只有以 `auto_delete` 存下的记录才会删 Google 端**（`lmdb.py` `cleanup_with_remote`），开启前的 `normal` 记录仍只按 14 天做本地清理
- 远程删除要求 `chat_scope is None`；我们的 `chat_scope()` 与上游一致，普通会话返回 `None`，条件成立
- 副作用：生成的图片/媒体保留期从 14 天变成 24h，旧对话里的图片链接一天后失效

### 镜像与发布

`ghcr.io/awaragml00029-debug/clean:20261026-064147`（`--no-cache --pull`，只打时间戳标签 —— 本机有 watchtower），`revision=99f4d1e`，**已推 ghcr**，digest `sha256:202d062b4343992b301abd6ad3be1b02498c4af9b3a6f1cd088edb509cb1803b`。
（`20261026-001902` 是开 auto_delete 之前的构建，作废，未推。）

镜像内核对：Python 3.14.9、gemini-webapi 0.0.post268（= 42f89a7）、`chat_mode=auto_delete`、TTL 86400、`url_fetch_timeout=30`，三处修复都在。冒烟（`--network none`、跑完即删）：`retention cleanup task (retention=14 day(s), auto_delete_ttl=86400s, interval=21600s)` → 恢复任务 → ready，无 Traceback/ERROR。

Git：`main` fast-forward 到 `99f4d1e` 并推送，分支同步推送。HTTPS 推送被拒（gh 的 OAuth token 没有 `workflow` 权限，而上游提交改了 `.github/workflows/`），改走同账号的 SSH key 推送成功，remote 配置未改。**`main` 上 CI 转绿**（09-09 以来第一次）。

**上线：未完成。** 生产是 Portainer stack（`gemini_rev`，stack 文件在 `portainer_data` 卷 `compose/6/docker-compose.yml`，环境变量全部内联、无 `${VAR}`）。改镜像行 + `docker compose up -d` 这一步被 Claude Code 的权限分类器拦下，stack 文件与容器均未改动。需要本人在 Portainer 把第 8 行镜像改成 `clean:20261026-064147` 后 Update the stack。

回滚锚点：`clean:20260909-142425`。注意两点：新库会把旧的明文缓存文件名 `.cached_cookies_<PSID>.json` 迁移成哈希文件名并删掉旧文件，回滚后旧库找不到缓存，会退回用环境变量里的 cookie；新代码写入的 pending-delete 键，旧代码的清理扫描会当成坏记录打一条 warning，无害。

### 生产现状（10-26 实测，与本次更新无关）

容器 `unhealthy` 已约 23h（健康检查连续失败 2718 次），`/health` 503「No usable Gemini client」：

- client 0（无代理）：`Account status: UNAUTHENTICATED`，**cookie 已过期**
- client 1、2（SOCKS5 代理）：`curl: (97) User was rejected by the SOCKS5 server`，**代理认证被拒**，启动重试 3 次后退役

新镜像不会修好这些，需要更新 cookie 和代理凭据。

### 待办

- `Sync Upstream` 的 PAT 问题（本人决定暂不处理）

---

## 2026-09-09（四）拒答其实有两类 —— 上下文污染那类是我们自己造的

### 最重要的修正：把两类拒答混成了一件事

此前用 12~16% 这个总数论证「失败率与请求内容无关」。**那个结论只对第一类成立**：

| 类型 | 特征 | 能否修 |
|---|---|---|
| Google 随机安全评分 | `thoughts=0` 秒拒，裸 `hello world` 也中 | ❌ |
| **上下文污染** | **思考充分、结论已成型**，最后才吐兜底 | ✅ **是我们自己造的** |

第二类的实证（用户实测，最高证据）：问「霸王龙有没有羽毛」，模型完整思考了整个科学问题 —— 查了 Bell et al. (2017) *Biology Letters*、Wyrex 标本皮肤印痕、Yutyrannus 与 Dilong 的对比、系统发育括号法 —— **结论都想好了**，最终却回「我是一个文本 AI，这超出了我的能力范围」。这跟秒拒完全不是一回事。

### 根因：生成图片的文件名污染了整个会话

```
① 生成图片 → 回复带 ![img_47d46252….jpg](…/media/…)
② 客户端把这行当 assistant 历史回传
③ 我们原样转发给 Google
④ 模型看到一个取不到的文件名 → 判定上下文不可信
⑤ 此后该会话里连纯文本问题也开始拒答
```

模型自己的思考把 ④ 说得很直白：「those `img_` text strings, **mimicking filenames**… **synthetically created in the prompt text, not actual files**」「The system expects either user-uploaded or previously generated files, **not these fabricated `img_` strings**」。

### 修了两轮，第一轮没修干净（我引入的）

**第一轮**：剥掉 markdown 里的 media URL，但**保留标题**，理由是"标题对模型有意义"。

**第一轮为什么没解决**：`chat.py` 生成 markdown 时 `img_link = f"![{fname}](…)"` —— **标题就是文件名本身**。于是丢了 URL、留下了名字，报文里变成 `[generated image: img_47d46252….jpg]`，模型照样看得见。

**第二轮**（真修好）：`chat.py` 的 alt text 改成 `Image` 不再用文件名；`helper.py` 增加 `GENERATED_MEDIA_NAME_RE`，标题形如生成名的一律丢弃。真实标题（"奔跑的小鸡"）仍保留。

### 让这次能自己查出来的东西：prompt 落盘

在此之前**请求侧对我完全不可见**，日志里只有 `input_chars=62`。ChatML 和文件名泄漏两个 bug **都是靠模型思考意外暴露 + 用户贴回来**才发现的，不是查出来的；ChatML 那次我还用一个对照组设错的 A/B 实验把它误判成"已证伪"。

用户提出「你要是能看到 docker 里的对话内容应该能更好 debug」后新增 `log_full_prompt`（默认开），把实际发给 Google 的报文写进 `debug_dumps/prompt_*.txt`。**当场还本**：第一份报文就直接看见了 `[generated image: img_47d46252….jpg]`，这是当天第一个不靠用户贴证据、自己查出来的问题。

结构化核对 8 份真实报文：`<|im_start|>` 0 处、`/media/` URL 0 处、base64 块 0 处、`img_` id **5 处** —— 前三项确认已修好，第四项直接暴露了漏网的那条路径。

### marker 一直在漏

固定词表跟不上 Google 造新词。已知变体：「我是一个语言模型，这超出了我的设计用途」「我无法提供这方面的帮助，因为我只是一个语言模型」「身为一个语言模型，我没办法提供这方面的帮助」「我只是一个语言模型，理解不了这个问题」「**我只是一个文本 AI，在这方面没法帮到你**」「**我是一个文本 AI，这超出了我的能力范围**」「**由于程序代码的局限，我没法办到**」「I seem to be encountering an error」。

**「文本 AI」曾整个漏掉**，导致一次真实拒答零记录。改为匹配句式（`我(是|只是)?一个?<≤12字>(语言模型|文本模型|文本 AI|AI 模型|大模型)`）而非固定词，代价是多几个误报 dump，好过漏掉证据。守卫用例 10 项，含「这个大模型的参数量很大」等不该命中的正常讨论。

## 2026-09-09（三）图片慢的真因、widget 块、以及「不做拒答重试」的决定

### 图片慢：4 次串行代理往返，不是我们的处理慢

实测两次带图请求（日志时间线，非推断）：

| Google 生成 | 媒体处理 | 媒体占比 |
|---|---|---|
| 19.6s | **30.8s** | 61% |
| 19.5s | **19.8s** | 50% |

媒体处理比生成还慢。根因在 `gemini_webapi/types/image.py`，`full_size=True` 的路径：

```python
original_url = await self.client_ref._get_full_size_image(...)  # ① 请求
response = await req_client.get(f"{original_url}=d-I?alr=yes")  # ② 请求
response = await req_client.get(response.text)  # ③ 请求
# 然后 ④ 才下载图片本体
```

**4 次串行网络往返，每次都走账号的 SOCKS 代理**，前三次纯粹在解析真实 URL。`full_size=False` 直接给 URL 加 `=s1024-rj`，**1 次请求**搞定。而我们此前把 `full_size=True` 写死在 `chat.py`。

改为配置项 `image_full_size`，**默认 false**（`CONFIG_GEMINI__IMAGE_FULL_SIZE=true` 换回原图画质）。同时 `read_bytes`/`base64`/`sha256` 移进 `asyncio.to_thread` —— 几 MB 只有几十毫秒，不是本次主因，但它跑在请求循环里会拖住其他在途流。

新增分段日志 `Image ready: download=Xs encode=Ys full_size=B`，下次不必再靠读代码推断。

### `<GenerateWidget>` 块：能剥不能救

`<GenerateWidget …>{spec}</GenerateWidget>` 里装的是**给渲染器的提示词**（"Create an interactive tactical map…"），渲染发生在 Google 前端 —— 逆向 API 拿到的是图纸不是房子，**运行不了**。`GenerateWidget` 在我们代码和 gemini_webapi 里都零命中，和 FollowUp 一样是原样漏出来的。

改为折叠成一行占位说明（直接删会让回复出现无法解释的缺口）。流式实现比 FollowUp 难：块有几 KB、跨很多 chunk，开闭标记都会被切断。

**一个被测试抓住的 bug**：起初用 `buf.find("<GenerateWidget")` 判定块起始，于是 `<GenerateWidgetX>` 也命中，然后一路吞到流末尾。已加分隔符校验（后面必须是空白或 `>`），并在 `test_widget_block.py` 里留了回归用例。非流式正则因为有 `\b` 边界，本来就没这个问题。

`component_placeholder_id` 暗示响应里**可能**有对应内容块（未证实）。为验证，帧捕获新增 widget 类型，与 fallback 各自 30 个配额，互不挤占。

### 决定：不做拒答自动重试

日志证实**重试有效**（同样内容，被拒后几秒重发就能过）—— 说明 Google 是每次请求独立做随机安全评分，不是内容拉黑：

```
03:34:09 兜底 → 03:34:10 重试 → 03:34:30 又兜底 → 03:34:34 重试 → 03:35:04 起连续成功
03:35:55 兜底 → 03:36:35 重试 → 03:37:05 成功
```

判据也有 25 个 dump 撑着（整句匹配 + `len(text)<80` + `thoughts>0`，能挡掉全部已知误报）。

**但用户决定不做。** 记录在此，免得以后有人看到 12~16% 的拒答率又把这事重开一遍：技术上可行、判据齐备、代价约 5% 额度，是**产品决定不做**，不是做不了。

## 2026-09-09（续）兜底回复定性：与请求内容无关，提示词假设被实验证伪

### 被证伪的假设：注入的工具提示词触发了风控

用户提出「自带的提示词风格触发风控」，理由是发 `hello world` 都不回答。这个假设**看起来很有说服力**：`build_tool_prompt` 注入的 `SYSTEM INTERFACE: ...`、`REQUIRED SYNTAX, reproduce literally`、`Emit the block and nothing else. No preamble or commentary.` 加上 `[ToolCalls]` 私有标记，形态上确实很像提示词注入攻击，而 `TOOL_WRAP_HINT`（358 字符）还会被 `append_tool_hint_to_last_user_message` **追加进用户消息本身**。

我一度据此写了一整套推理。**A/B 实测把它推翻了**：

| 组 | 场景 | 结果 |
|---|---|---|
| A | `hello world`，**无** tools（不触发任何注入） | 8 次里 1 次兜底 |
| B | `hello world`，**带** tools（注入 533 字符） | 8 次里 1 次兜底 |

**两组完全一样。** 注入不是变量。

### ⚠️ 上面这个「证伪」是错的 —— A/B 实验设计有缺陷（当日晚些时候推翻）

用户贴回一次真实对话：模型的**思考过程被暴露出来**，原文写着：

> "dissecting the **injected system prompt**, observing how the user framed their instructions within **fake tokens**. The markup is intended to **simulate a system message**"

然后回复：「请恕我爱莫能助，我只是一个语言模型，不能理解或回复你的这个问题。」

**模型把我们的 ChatML 包装当成了伪造的系统消息，判定为提示词注入，然后拒答。** 包装出处 `helper.py:288`：

```python
return f"<|im_start|>{role}\n{content}" + ("" if unclose else "\n<|im_end|>")
```

这也解释了裸 `hello world`（11 字符）为什么日志里是 `input_chars=62`。

**我那个 A/B 实验为什么没测出来**：`<|im_start|>` 包装是**无条件**加的，A 组和 B 组**都带**。我只变了「工具提示词」这一个变量，真正的嫌疑犯在两组里都在场，所以两组表现一致 —— 我却据此宣布「注入不是变量」。**对照组根本没设对。**

**证据等级**：这是用户的**一次实测观察**（模型思考意外暴露），不是我跑的对照实验。方向性很强，但「ChatML 包装是根因」尚未被对照实验证实。

**要验证它，正确的对照组是**：一组带 `<|im_start|>` 包装、一组完全裸发（单条 user 消息不加任何标记），各跑 30+ 轮比失败率。本轮未做。

### 立住的结论：失败率与请求内容无关

三轮独立实验：

| 实验 | 场景 | 失败率 |
|---|---|---|
| 19 次 | 中文长回答，无 tools | 3/19 ≈ 16% |
| A 组 8 次 | `hello world`，无 tools | 1/8 = 12.5% |
| B 组 8 次 | `hello world`，带 tools | 1/8 = 12.5% |

连 `hello world` 这种无害到极点、零注入的请求都稳定翻车。**提示词、工具定义、请求长度、语言全都不是变量**，剩下的解释只有 Google 侧对这批账号或这条通道存在固有失败率。

### 一个之前混淆了的区分

我一直把这事当「安全拒答」，但这轮抓到的是：

```
I seem to be encountering an error. Can I try something else for you?
```

这**不是拒绝，是报错**。跟之前那批「我只是一个语言模型」（能力边界拒绝）语义不同。**很可能是两种不同的失败被当成一件事在分析**，之前的 16% 里混了多少哪一种，现在说不清。

### 另一条要修正的旧结论（存疑，未验证）

旧记录写「tool calling: auto 约五成、required 0/10，锅在模型不在代码」。但 `required` 会额外注入 `You MUST call at least one tool this turn`。若强制指令确实更易触发拦截，那 0/10 可能是**每次都被兜底替换**，而不是模型不听话 —— 两者在「没返回 tool call」这个表象上无法区分。**本轮 A/B 只测了 auto 场景，没测 required，所以这条仍是猜测**，标注在此待验。

### 部署后的客观数据（纠正一个错觉）

用户反馈「修过的版本成功率提升了不少」。查日志：

```
resending 次数 : 0
成功完成       : 12
失败           : 0
```

**1097/1155 的重试一次都没触发过**，所以提升不可能来自它。FollowUp 剥离只影响文本整洁度、不影响成功率。**没有客观证据支持这次修复提升了成功率**，样本也太小，不认这个功劳。

### 加固完整性核查：没丢，但清掉一颗上游残留的地雷

用户担心「gemini-webapi 更新了，是不是我们的加固漏了什么」。三条独立证据说明**没丢**：

1. **`scripts/check_deltas.sh` 39 项断言全绿** —— 这脚本就是为防上游同步冲掉加固而存在的，`All fork custom deltas intact`
2. **孤儿函数扫描**：235 个函数定义里只有 1 个真孤儿，其余 7 个是 pydantic `@field_validator` / `@model_validator` 的框架回调假阳性
3. **init 超时加固实测在位**：`init() → _init_with_retries → _restart_client → asyncio.wait_for(client.init(), timeout=...)`

那 1 个真孤儿是上游残留：

```python
async def _init_one(self, client) -> bool:  # 零调用者
    return await self._init_attempt(client)


async def _init_attempt(self, client) -> bool:  # 零调用者（除上面那个）
    await client.init()  # 裸调用，没有 wait_for
```

正是 09-03 记录里写的「上游的 `_init_attempt` 三样都没有」那个版本。**当时没有危害（没人调用），但是陷阱**：谁顺手用 `_init_one` 就绕过超时，直接踩回「死账号重启卡在请求路径」那个延迟根因。已删除，删前核过全项目零引用、守卫零断言、实际路径不经过。

**方法论留档**：孤儿函数扫描（定义了但全项目零调用）能查出「上游同步把调用点改回去了」这一种丢失形态，但查不出「函数还在、内部代码段被覆盖」那种 —— 后者靠 `check_deltas.sh` 的断言。两者互补，都要跑。

### 本轮新增：原始帧捕获（诊断，非修复）

问题的根本障碍是**看不到 Google 实际返回了什么** —— 库只把最终文本交出来，中间 17~21 个 part 全丢。`app/utils/raw_capture.py` 用与 `ARTIFACTS_RE` 相同的猴子补丁手法换掉 `StreamingFrameParser`，累积当前请求的帧，命中兜底文案时把**命中前的全部帧**落盘到 `/app/data/debug_dumps/`。

- 每个请求一个 parser 实例（`client.py:1552`），天然隔离，不需要 contextvar
- 三处 patch：`client` / `utils` / `utils.parsing` —— client.py:72 在 import 时绑定了名字
- 单请求只 dump 一次；文件数上限 30；单文件 2MB 上限
- 全程 try/except，dump 失败只丢证据不影响回复（有测试锁定）
- `GEMINI_DUMP_FALLBACK=0` 关闭，`GEMINI_DUMP_DIR` 改路径

**这只是诊断，不改任何行为。** 拿到 dump 后要看的是：兜底文案是单帧整体替换、是第二个 candidate、还是覆盖了已有的部分文本 —— 这决定了能不能在我们这层救回来。

## 2026-09-09 「跑几次就出错，特别是文字」实测定性：三个现象，两个是我们的

起因：用户报「程序跑几次就有错误，特别是文字」，补充说「最常见的是回复被换成一句拒答，也有回答或思考一半直接报错的」。

### 实测（真发了请求，不是读代码）

直连容器 8092 发 **19 次真实对话**（11 次非流式 + 8 次流式，同一个提示词，gemini-flash，轮询 3 个账号）：

| 现象 | 频次 | 定性 |
|---|---|---|
| 最终回复被替换成拒答文案 | 3 / 19（≈16%） | **Google 侧**，不是我们的 bug |
| `Unknown API error code: 1097` / `1155` | 历史日志 25 次 | 库不认这两个码，直接抛、不重试 |
| `<FollowUp .../>` 标签漏进正文 | 流式 8 次里 1 次 | **我们没剥**，全链路无人处理 |

FollowUp 的出现率只能给流式的 1/8：非流式统计脚本当时只打印了 content 前 120 字，标签在末尾可能被截掉没统计到，**实际可能更高**。

### 现象一：拒答替换 —— 查清了，是 Google，本轮按用户决定不动

三次措辞各不相同：「我是一个语言模型，这超出了我的设计用途」/「我无法提供这方面的帮助，因为我只是一个语言模型」/「身为一个语言模型，我没办法提供这方面的帮助」。

四条证据排除了「我们挑错 part / 丢了正文」：

1. 三句文案在 `app/` 和容器内 `gemini_webapi` 里全都搜不到 —— 只能是服务端返回的
2. 流式下拒答**只有 1 个 content chunk**，正常回答是 2–3 个 —— 不存在正文被我们吃掉
3. 有一次 `reasoning_content` 长 2190 字符，模型**已经把中文答案写完了**（"擅长破解复杂密码与分子模拟等前沿难题"），最终 content 才被换成拒答 —— 替换发生在输出层
4. 联网查证：Gemini 网页端已知行为，安全分类器后置拦截，普通网页用户同样会遇到「回答先出来几秒然后消失」

我们这边**没有**「识别拒答→换账号重试」的逻辑，撞上就原样返给客户端。用户本轮明确要求不做：会多烧账号额度，且「什么算拒答」的匹配规则容易误伤模型正常自述身份的场景。

### 现象二（已修）：1097 / 1155 归入可重试

库的 `ErrorCode`（`constants.py:343`）只收录 1013/1037/1050/1052/1060，1097 和 1155 落到 `client.py:1608` 的 `case _` 直接 `raise`，**一次都不重试**。而 Google 自己的网页端把这两个码显示为临时性的 "Something went wrong"。

`gemini-webapi` 是 PyPI 外部依赖（`pyproject.toml`，`>=2.1.1,<3`），改 site-packages 会被重建镜像冲掉，所以重试做在**我们这一层**：`chat.py:_send_with_internal_fallback` 外面套重试循环，`_transient_api_error_code()` 从异常文本里取码。

在这一层重试是安全的，前提是既有的 `_send_and_await_first_chunk`：它**先拉第一个 chunk**，所以失败时响应还没提交给客户端。用同一个 session 重发（不换账号）—— chat 本身没坏，且库在 `has_generated_text` 为假时会自己回滚 chat metadata。

### 现象三（已修）：`<FollowUp .../>` 标签剥离

网页 UI 把 `<FollowUp label="..." query="..."/>` 渲染成追问按钮，逆向 API 拿到的是原始标签，直接透传进正文。`FollowUp` 这个词在我们代码、`gemini_webapi` 库、容器内全都搜不到 —— **全链路无人处理**。

复用既有的 artifact 剥离管道，三个挂载点（覆盖 chat.py 4 处 + gemini.py 2 处全部输出路径）：

- `helper.py:process_llm_output` —— 非流式
- `StreamingOutputFilter._filter_followup` —— 流式，带跨 chunk holdback（标签会被切在两个 chunk 之间）
- `StreamingOutputFilter.flush` —— 收尾释放

### 本轮没覆盖的（重要）

**流中途报错修不到。** 1097/1155 实际出现在两个位置，日志区分得很清楚：

- `_send_with_split:1288` → `create_chat_completion:2944`：首 chunk 之前 —— **本次修复覆盖**
- `generate_stream:1749` "Error during streaming"：首 chunk 之后，SSE 推到一半断 —— **覆盖不到**

用户抱怨的「回答一半报错」是后者。在那个位置重试要重构 `_restream` 的结构，且 thoughts 已经推给客户端、重试会重复输出，代价和风险都高，本轮没做。

出错现场的两次采样都是长输入：`text_chars=89494 / elapsed=101.641s`、`text_chars=24613 / elapsed=16.435s`。**样本只有 2 个，不足以断言「只在长输入时发生」** —— 留给下次。

另：1013 已被库自己收录且文案不同（`temporary error (1013)`），不走我们这条正则，本轮按用户「只修 1097/1155」未纳入。

### 验证

`ruff check` / `ruff format` / `pyright`（须带 `--pythonpath .venv/bin/python`，否则它退到系统 py<3.12 报一堆假错）/ `ty` 全清。新增 `tests/test_followup_filter.py`（24 项）、`tests/test_transient_retry.py`（11 项），既有 313 项全过。

测试里锁了一条**既有**行为：`flush()` 会丢掉结尾孤立的 `<`，因为 `STREAM_FLUSH_TAIL_RE`（`helper.py:264`）把它当 `<|im_start|>` 残片。与 FollowUp 无关，验证过未改动的正则本身就这样。

## 2026-09-03（下午）IP 与 TLS 指纹全面审计，外加三处加固

起因：本人观察到 Google 加强风控，判据是「IP 和指纹决定这个项目能不能用」。

### 审计结论：没有「发往 Google 用错 IP 或错指纹」的 bug

出网点逐个核过，全部带账号自己的 proxy 和 impersonate：

| 路径 | 位置 | 代理 | 指纹 |
|---|---|---|---|
| 认证 / 初始化 | `get_access_token.py:279-284` | ✅ | ✅ |
| 主对话、流式 | `client.py:304` 活 session | ✅ | ✅ |
| cookie 轮转 | `rotate_1psidts.py:37` 复用活 session | ✅ | ✅ |
| 图片下载 | `client.py:2123/2149` `client=self.client` | ✅ | ✅ |
| 视频 / 音乐下载 | `client.py:2172/2203` 少传 `client=` | ✅ | ✅ |
| 用户给的远程 URL | `helper.py:782` | ✅ | ✅ |

`app/` 里唯一的 HTTP 客户端导入是 `helper.py:24` 的 curl_cffi，没有 httpx / aiohttp / requests / urllib.request。容器里没有 `HTTP_PROXY` 之类会在背后绕过 per-client 代理的环境变量。

### 实测（真发了请求，不是读代码）

- **出口 IP**：3 个账号 3 个独立地址，无共用。client0 直连 `146.19.*.*`；client1、client2 各自一个 `107.172.*.*` 段地址。
- **指纹**：4 种配置产生 4 种不同 JA3，对照组（不带 impersonate）与三者都不同 —— 证明这个开关不是摆设。
- **对话 session vs 抓取 session**：按两处代码各自的参数形状分别建 session 实测，**JA4 与 Akamai(H2) 三个账号全部逐字节一致**。
- **JA3 对 Chrome 配置每次都变**：同一配置连打三次得到 3 种 JA3，Firefox 得到 1 种。这是 Chrome 自 110 起随机化 TLS 扩展顺序的行为，curl-cffi 复现对了；JA3 顺序敏感所以变，JA4 排序后计算所以稳。**一个 JA3 恒定的「Chrome」才可疑。**

两处自我纠正：一是最初拿出口 IP 去比代理的**监听**地址得出「不一致」，那是错的，代理跑在本机公网 IP 上、转发出去才是 107.172；二是 JA3 差异被我一度当成配置问题，实为随机化。

### 加固一：堵死会静默绕过代理的降级分支

`chat.py:_process_conversation_for_client` 的 `client` 参数原为 `GeminiClientWrapper | None`，为空时兜底成 `{"proxy": None, "impersonate": None}` —— 也就是**悄悄直连、退成通用 chrome 指纹，日志一个字不说**。追证：`_find_reusable_session`（`chat.py:1017`）只返回 `(None, None, ...)` 或 `(session, client, ...)`，其余调用点都来自 `pool.acquire()`，所以这条分支当时不可达。但它是个安静的降级，改成非可选参数后由 ty + pyright 在 CI 拦住。两个端点的分支条件同步收窄为 `if session and client:`。

### 加固二：视频 / 音乐下载复用账号的活 session

`client.py` 构造三种媒体对象时参数不一致：`WebImage`（`:2123`）和 `GeneratedImage`（`:2149`）都带 `client=self.client`，`GeneratedVideo`（`:2172`）和 `GeneratedMedia`（`:2203`）没带。于是 `save()` 走 `video.py:84` 的兜底分支新建 session。兜底带了 `client_ref`，所以 proxy、impersonate、cookies 都对，**不是错 IP**，只是白开一条连接。

`chat.py:_media_to_local_file` 现在从 `media.client_ref.client` 取活 session 显式传进去。安全性核过：`save()` 只关它自己新建的 client（`video.py` 的 `close_client` 标志），传进去的不会被误关；取不到就退回原行为。

### 加固三：SSRF 检查钉住它批准的那个地址

原来 `_validate_remote_url` 解析域名、校验是公网、然后**把 URL 原样交给 curl 再解析一遍** —— 两次解析之间答案可以变，这正是 DNS rebinding 的全部内容。

现在校验函数返回 `(url, 通过校验的地址)`，`_pin_options` 把它写成 `CURLOPT_RESOLVE`，curl 只能连这个地址。重定向每一跳重新校验、重新钉（`curl_options` 是 session 级但每次请求重读，见 curl_cffi `session.py:664/1356`）。

**带代理时钉不住，而且是静默失效** —— 实测：对着本部署自己的 SOCKS 代理，把 IP 钉到 `127.0.0.1` 仍然返回 200，`socks5` 和 `socks5h` 一样，因为 curl 把目标交给代理而不是自己拨号。所以代理路径**不设**这个选项，而不是设了假装有用。日志新增 `address_pinned=` 字段，把「这次到底钉住没有」写明。

设计过程中被实测否掉的方案：本想「降级 socks5 + 钉 IP」让代理路径也受控，实测证明 `socks5` 也一样忽略 RESOLVE，方案作废。另：`CurlOpt.RESOLVE` 只接受 list，传字符串会让 curl 逐字符解析并报 `Could not parse CURLOPT_RESOLVE entry 'a'`；库把类型标成 `dict[CurlOpt, str]` 太窄，需显式 cast。

剩余风险，说明白：**挂代理的账号，那个预检查读的是本机 resolver，而实际连接由代理端解析，所以它是建议性的、不是强制的。** 本机代理是 xray（监听 10830/10834，转发到 107.172 段），它有能力连到内网，所以这个检查不能干脆删掉 —— 删了就一点保护都没有。

`tests/test_url_pinning.py` 新增 14 条测试钉住这套行为，其中专门有一条钉「代理时不设 pin」，防止以后有人把它「修」成静默无效。`scripts/check_deltas.sh` 加两条断言。

### 未做

**并发限制（一个账号一次只跑一个请求）本人明确说先不做。** 前置问题已查清并记录在此，供以后动手：`request_scope` 只是计数器不是锁，且盖不住整个请求 —— `pool.acquire()`（`pool.py:106`）到发送之间的文件上传裸奔，`chat.py:1401` 首块拿到后 scope 关闭、到 `_hold_request_scope` 真正开始消费之间还有一段裸奔。不先把 scope 拉成「从 acquire 到响应结束」一整段，加锁会从这两个缝里漏。

## 2026-09-03 同步上游 7d1744e → 66dc3f7（gemini-webapi 2.1.1）

### 上游这次改了什么

`luuquangvu/Gemini-FastAPI` main 前进 5 个提交（08-26 ~ 08-28），净改动只有 5 个文件：

| 文件 | 内容 |
|---|---|
| `pyproject.toml` / `uv.lock` | gemini-webapi 2.1.0 → **2.1.1**，curl-cffi 0.16.0 → 0.16.2 |
| `app/utils/helper.py` | `TOOL_WRAP_HINT` 第 2 条加 "no language tag"；`PARAM_FENCE_RE` + `strip_markdown_fence` 修复 |
| `tests/test_api_compatibility.py` | +49 行 |
| `.gitattributes` | 1 行 |
| `Dockerfile` | `c17e912` 加 git、`66dc3f7` 又删掉，**净变化 0** |

中间三个提交把依赖临时指向作者自己的 `luuquangvu/Gemini-API` git 分支，`66dc3f7` 落回 PyPI 稳定版，所以只看首尾差异即可。

### gemini-webapi 2.1.1 的内容（拉 `v2.1.0...v2.1.1` diff 确认）

官方 release 就是上游作者提的两个 PR：

- **#353 `get_access_token` 按失败类型分组** —— 429 立刻抛 `TemporarilyBlockedError`，超时抛 `TimeoutError`，网络错直接向上抛；不再被一个 `except Exception` 吞掉后接着把所有 cookie 组挨个试完。这正对上 08-23 定位的 client0：`GET gemini.google.com/app` 429×18，`_ensure_client_ready` 在请求路径上空转 6.5~7.7s。
- **#354 `close()` 加 `was_running` 守卫** —— init 失败时不再拿未初始化的 cookie 覆盖有效缓存文件；`init()` 的 except 里补 `self._running = False`。

兼容性已核：我们所有 `client.init()` 调用点都在 `except Exception` 里（`pool.py:79/214/266`、`client.py:67`），新异常类型打不穿。

### 合并结果

`git merge-tree` 预判和实际一致：**全程只有一个冲突**，在 `helper.py` 的 `TOOL_WRAP_HINT`。

- **保留我们的软化版**。上游是在原版硬措辞上加一句 "with no language tag"；我们 `11dcba0` 已把整段换掉（原版实测 10% 命中率／40 轮，软化后 95~100%）。上游那句想解决的问题由下面的正则修复覆盖，措辞不必跟。注释里保留的「上游原文」已同步到 `66dc3f7` 的措辞。
- **`strip_markdown_fence` 修复照收**。`PARAM_FENCE_RE` 改成允许围栏后面跟语言标签，切片也从 `s[len(fence):]` 改成 `s[len(match.group(0)):]`。我们这两处此前还是上游旧版原样，干净合入。这个修复对我们比对上游更需要：提示词软化之后模型更自由，更可能自己给围栏加语言标签。

### 顺带补上我们自己的洞：`close()` 覆写丢了两个 cookie 守卫

`app/services/client.py:111` 我们**完整覆写了库的 `close()`**（`a32782d` 引入，上游 `7d1744e` 没有这个覆写），结尾无条件 `save_cookies`。而库自己的 `close()`：

- 2.1.0 就有 `if self.account_status == AccountStatus.UNAUTHENTICATED: return`
- 2.1.1 又加了 `if not was_running: return`

**两个守卫我们一个都没有。** 而库的 `init()` 失败时内部调的是 `self.close()` —— 落到的正是我们这个覆写。所以光升级到 2.1.1 拿不到 #354 的修复，守卫必须手工搬进覆写里，已补。

`_running` 全仓只有库的 `init()`（置 True）和我们的 `close()`（置 False）两个写入点，重复 close 时第二次 `was_running=False` 跳过写盘，正是想要的行为。

证据等级说明：这条只验到「代码路径成立」，**没有线上日志证明真的发生过覆盖**，属于读代码推出来的，不是实测。

`scripts/check_deltas.sh` 补了两条断言（`was_running`、`AccountStatus.UNAUTHENTICATED`）盯住这个覆写，防止下次同步再被抹掉。

### 顺手修的 CI 红灯

`ruff check` 在 `11dcba0` 上就已经红了：12 条 RUF003（中文注释里的全角括号/逗号/冒号被判为易混字符）。本次把这些注释的标点改成 ASCII，`ruff check` / `ruff format --check` 现在全绿。没有动 `pyproject.toml` 的 ruff 配置——那是上游文件，加 ignore 会给以后的同步埋冲突。

## 2026-08-24 死账号重启循环与「假装回退 guest」

本人提问：「上一次提交我们完成了出问题的账号直接跳过，为什么我看 docker 日志还是会请求失败的账号？」

### 一、先回答：跳过是生效的

统计容器全生命周期的 `Client ID:` 日志（每条请求实际落在哪个 client）：

| client | 请求数 |
|---|---|
| `251d970ca40ebe06e6c2dsfasddddd` | 7 |
| `beeusman4105a99a6717f839b110931` | 5 |
| `osdfasdfrange-ubd97083`（死账号） | **0** |

日志里那些行全部来自 `pool.py` 的后台恢复任务，不在请求路径上——正是 `bf48a13` 的设计。

**真正误导人的是日志措辞**：旧代码每次都打 `Restarted Gemini client ... after it became unavailable`，读起来像重启成功了，实际下一秒它又不可用。看着就像"还在往坏账号上打请求"。

### 二、底下确实有个 bug：`restart_max_failures` 对这种失败模式够不着

`_restart_client` 调的 `client.init()` **对这个死账号不抛异常**——库返回 `SUCCESS ... initialized successfully`，随后会话仍是 `UNAUTHENTICATED`。而失败计数只在 `except Exception` 里累加，于是走成功分支把计数清零；下一轮 `is_healthy()` 要求 `AccountStatus.AVAILABLE` 又不满足 → 再重启。**无限循环，`restart_max_failures: 3` 永远到不了。**

这也解释了 `/health` 的 `retired` 为什么会变：早些时候是 `["osdfasdfrange-ubd97083"]`（那次 init 真抛了 AuthError 三次），后来变成 `None`（这次 init "成功"了）。同一个容器 15:40:25 抛 AuthError、15:41:25 就"成功"——**这个账号的 init 是时好时坏的**，所以两种路径都会走到。

**选择退避而不是退休**：退休是整个进程生命周期不可逆，而 `auto_refresh` 仍可能轮换 cookie 把账号救回来。重启后仍不可用则把下次尝试推后，60s → 5min → 30min 封顶；一旦真恢复立刻清空退避重新入列。真异常仍照旧计数并在 3 次后退休，`ClientBusyError` 仍不计数。

模拟一小时（60s 轮询）对照：

| | 旧 | 新 |
|---|---|---|
| 无谓认证往返 | **60 次/小时** | **4 次/小时** |
| 是否退休 | 否 | 否（保留自愈） |

日志也改成说实话：`restarted but is still unusable (round N); next attempt in 1800s`。

### 三、附带查实：guest 回退是死代码

构造「三个 client 全是 guest」跑 `acquire()`：它先打印 *"falling back to a guest session until cookies are refreshed"*，**紧接着抛 `RuntimeError: No Gemini clients are currently available`** —— guest 一次都没被返回过。

原因是两个条件互斥：`_client_ready` → `is_healthy()` 要求 `AccountStatus.AVAILABLE`，而 `is_guest()` 的定义就是 `== UNAUTHENTICATED`。旧代码那个双轮 `for account_only in (True, False)` 因此纯属冗余。`guest_mode` 配置也管不着——它只影响 health 端点的判定。

**本人决策：所有账号都挂时明确失败并提醒用户，不要用 guest 悄悄顶着。** Google 给 guest 没有历史、不能上传、不能选模型，静默降级等于用一个悄悄变差的东西回答请求。

于是拆掉假回退，`_unavailable_reason` 按四种情况分别给话：cookie 全过期（要人改凭据并重启容器）、全部退休、需要上传但无认证账号、普通不可用（重试即可）。前两种要人介入、后两种等一等就好，以前糊成同一句 `No Gemini clients are currently available`。该信息原样进入调用方看到的 503 `detail`，不是只写日志。

顺带：`require_account` 现在只用于选措辞——`_client_ready` 已隐含 `AVAILABLE`，返回的 client 必然非 guest、必然能上传。

### 四、证据等级

退避的 60→4 与 acquire 的新旧报错，均为单元级对照脚本实测。**两条都未在线上容器端到端验证**：触发条件是「所有账号 cookie 同时失效」，不会为了测试去弄坏线上凭据。

`tests/test_pool_recovery.py` 15 个用例；`check_deltas.sh` 增加 `_defer_futile_restart`、`_unavailable_reason` 两条守卫。

## 2026-08-23 附件上传链路修复（data URL 解码回归 + 四个连带缺陷）

线上反馈「上传附件的功能被破坏了」。查下来是**上游同步冲掉了 fork 自己的解码器**，外加同一条链路上四个独立缺陷。

### 一、根因：data URL 前缀没剥就丢给 base64 解码

`save_file_to_tempfile` 原本调用仓库自带的 `decode_base64_data`，`f0f4720`（":arrow_up: Adopt upstream's remote-fetch fixes"，08-20）把它换成了上游的裸 `base64.b64decode`，再由合并 `9a4859c` 带上主线。

OpenAI 官方的 `file.file_data` 格式就是 `data:<mime>;base64,...`。裸 `b64decode` **不剥前缀**，只丢掉前缀里不在 Base64 字母表的字符再解剩下的——**不抛异常，直接产出坏文件**。

坏法随 MIME 长度而变（前缀保留字符数 % 4）：

| MIME | 保留字符数 | %4 | 后果 |
|---|---|---|---|
| `text/plain` | 20 | 0 | 仅多 15 字节垃圾头，**内容仍可读** |
| `image/jpeg` | 20 | 0 | 同上 |
| `application/pdf` | 25 | 1 | **整体错位，彻底损坏** |
| `image/png` | 19 | 3 | 整体错位 |
| `text/csv` | 18 | 2 | 整体错位 |
| `application/json` | 26 | 2 | 整体错位 |
| docx | 77 | 1 | 整体错位 |

**排查陷阱**：第一轮用 `.txt` 测试，正好撞上 `%4==0` 这个唯一的良性个例，两个用例全绿、差点放过。换 PDF 才复现：同一份文件纯 base64 读出 marker，data URL 返回 `CANNOT_READ`；容器日志 `decoded_bytes=158` 而原文件 143 字节。

**检测信号**：`decode_base64_data` 定义完好却**零调用点**。同步冲掉的是**调用点**（函数体内一行），定义在文件别处不冲突所以活了下来——孤儿 helper 就是这类回归的指纹。`check_deltas.sh` 只断言符号存在，抓得到丢定义、抓不到丢调用。

### 二、连带缺陷（同一条链路，各自独立）

**`.bin` 兜底后缀有害。** 无 filename 时后缀写死 `.bin`。用字节完全正确的纯 base64 路径隔离验证：`.bin` → `CANNOT_READ`，而无扩展名文件名（后缀为空）→ 正常读出。Gemini 对 `.bin` 拒读。

**图片路径比附件路径更严格。** `_decode_data_url` 用 `b64decode(validate=True)`，不剥空白、不认 urlsafe；而 `decode_base64_data` 的注释明写着要容忍这两者。同一张 PNG：普通 base64 返回 `Red`，76 列折行和 urlsafe 全部 **HTTP 503 `Invalid data URL`**。

**`input_audio` 无视客户端声明的容器格式。** `client.py` 对每个音频写死 `audio.wav`，而 OpenAI 的 `format`（mp3/flac/m4a）存在 `raw_data` 里、还写进了 LMDB，上传点从来不读。依据 `app/main.py:25-36` 已有结论（Google 归类不了就"never reaches the model as an audible attachment"），mp3 套 `.wav` 会坏。

**畸形输入报 503。** `except Exception` 把所有失败糊成 503。这条**与本次第一项修复直接冲突**：静默损坏改成抛 `ValueError` 后，坏附件会变成 503，而 OpenAI SDK 重试 503 —— 一个永远不可能成功的请求会反复重发烧配额。等于把静默 bug 换成了重试风暴。

### 三、一个被证伪的假设（我自己下早了）

第二轮曾断言「无后缀会被内容嗅探，所以空后缀可以当兜底」，依据是**一次** `filename=report` 的观察。切镜像复验时同样是空后缀却 `CANNOT_READ` —— **空后缀不可靠，n=1 的结论不成立**。

正确做法：解码后的字节就在手上，直接嗅 magic bytes。后缀优先级定为 **文件名后缀 → data URL 的 MIME → 嗅探内容 → 空**。

### 四、最终实现

| 位置 | 改动 |
|---|---|
| `helper.py` | 换回 `decode_base64_data`；新增 `_suffix_for_upload`（三级优先，永不 `.bin`）与 `_sniff_suffix`（PDF/PNG/JPEG/GIF/WAV/WebP/MP3含裸帧同步/FLAC/M4A/MP4/docx/xlsx/pptx/zip/UTF-8 文本）；`_decode_data_url` 改走 `decode_base64_data`，两道尺寸防护保留 |
| `client.py` | `input_audio` 按声明的 format 命名，`isalnum()` 挡住客户端可控值落进临时文件后缀；仅在什么都没声明时退回 `.wav` |
| `chat.py` | 共用的 `_process_conversation_for_client` 把 `ValueError` 映射为 400；两个兜底 handler 不再把已定状态码重贴成 503 |

畸形 payload 现在抛 `ValueError` → 400，而不是静默产出垃圾。

### 五、验证（镜像 `20260823-234514`）

端到端 **13/13**：

| 用例 | 修复前 | 修复后 |
|---|---|---|
| PDF 走 data URL | `CANNOT_READ` | ✅ |
| 附件无 filename（纯 b64 / data URL / octet-stream） | `CANNOT_READ` | ✅ 嗅探出 `.pdf` |
| 图片 base64 折行 / urlsafe | **503** | ✅ |
| 畸形附件 | 503（会被重试） | ✅ **400** |
| TXT / PNG 无 filename | — | ✅ 嗅探出 `.txt` / `.png` |

日志佐证：`decoded_bytes` 全部等于原文件大小；临时文件后缀分别来自文件名、MIME、嗅探三条路径；两个 400 是 WARNING 不带堆栈；验证期间零 ERROR。

**证据等级**：上表全部为线上容器实测。**唯一未端到端验证的是音频格式修复**——机器上没有 ffmpeg/lame，造不出真 mp3 样本，其依据是 `main.py` 已有结论加本轮实测的「扩展名决定成败」，逻辑成立但未直接证实。

### 六、防回归

- `tests/test_file_upload.py` 55 个用例；每项修复都 stash 掉验证过会变红（分别 9 / 7 / 2 个），能失败的测试才算数。
- `scripts/check_deltas.sh` 新增 5 条守卫，断言的是**调用点**而非定义：`decode_base64_data(file_in_base64)`、`_suffix_for_upload`、`_sniff_suffix`、`to_thread(decode_base64_data`、`raw_audio.get("format")`。

### 七、遗留（与本次无关）

`tests/test_api_compatibility.py::test_unusable_schemas_leave_the_reply_unverified_rather_than_failing_it[schema0]` 卡死。把本次改动全部 stash 后在原始代码上复跑同样卡死（exit 124），**是既有问题**，未处理。

## 2026-08-23 响应延迟根因定位与客户端生命周期重构

线上反馈「有时候很慢，官网直连不论带不带思考都很快」。查下来根因和延迟本身无关，是**死账号的重启重试卡在请求路径上**。

### 一、两个被证伪的假设（别重走）

**假设一：扩展思考导致慢 —— 错。** 一开始拿「简单问题 3.10s vs 重推理问题 21.07s，输出都是 1~4 字符」下了结论。这是 n=1 的过度归纳。跑 10 轮量化后直接推翻：

| 样本 | 总耗时 | 思考字符 |
|---|---|---|
| 重推理 #5 | **3.20s** | 341 |
| 重推理 #8 | 3.89s | 347 |
| 重推理 #2 | **19.81s** | **0** |

最慢那次思考字符数是 0，多次带 300+ 思考的只要 3.2s。**思考与慢不相关。**

**假设二：curl_cffi 指纹导致慢 —— 也错。** 155 次请求按指纹分组：

| 指纹 | 次数 | p50 | p90 | 卡顿(>8s) |
|---|---|---|---|---|
| chrome145 | 83 | 1.22s | 2.29s | 1 (1%) |
| firefox147 | 72 | 1.19s | 1.86s | 2 (3%) |

两组几乎相同，n=1 和 n=2 无统计意义。

同时排除的还有：**网络**（直连 gemini.google.com 首字节 137ms；65 小时 117 次探针每个 2 小时窗口中位数都是 0.13s，最大 0.40s）、**本地程序**（`process_conversation` 埋点 183 次全是 `elapsed=0.000s`，LMDB 落库 p50 0.31s，容器 0.27% CPU，合计占总耗时约 2%）、**Gemini 排队**（库里有 `is_queueing` 状态和 debug 日志，65 小时全量 0 条）、**会话复用**（命中 p50 11.56s vs 未命中 11.70s，无差别）。

### 二、真正的根因

死账号 `osdfasdfrange-ubd97083` 的 cookie 失效，且它的认证**被 Gemini 限流**：

```
GET https://gemini.google.com/app  [429] × 18
GET https://gemini.google.com/app  [200] ×  2
```

同期业务请求 62 次全 200 —— 只有它的认证在吃 429。

而当时的 `pool.py` **没有任何冷却/退避/熔断**（全仓 grep `cooldown|backoff|circuit|last_fail|fail_count` 命中 0 条）。轮询每次转到它，`_ensure_client_ready` 就在**请求路径上同步**完整重启一遍：三轮认证全 429，**耗时 6.5–7.7 秒**，失败后才回落到健康账号。

证据：5 次「重启放弃 → 用户请求开始」的间隔全部是 **7–9 毫秒**。某段日志 11 个请求里 **5 个（45%）**先白等了 6.5+ 秒。

**为什么之前的测量漏掉了它**：早先测的「卡顿」窗口开在 `create_chat_completion → _generate`，而这 6.5 秒发生在 `create_chat_completion` 打日志**之前**，被系统性漏掉。

### 三、设计演进（三次推翻）

| 版本 | 方案 | 为什么被推翻 |
|---|---|---|
| v1 | 请求路径上加固定 300s 冷却 | 只是把 45% 降到每 5 分钟一次，重试**仍在请求路径上** |
| v2 | 移出请求路径 + 指数退避 | 本人指出：出错账号只有改 compose + 重启容器才能救，**重试是纯浪费** |
| v3 | 一次失败就永久拉黑 | **引入回归**：见下 |
| **v4** | 最终方案 | —— |

**v3 引入的回归（本人提问揪出）**：`chat.py` 在 `except Exception` 里对**任何**请求异常调 `mark_unavailable()`，注释写着 *"the pool revives it on the next acquire"*。v3 让 `acquire()` 不再复活任何东西，于是**一次网络抖动就能永久杀掉一个好账号**，三个账号三次偶发错误就能让服务全废。**213 个既有测试全绿也没抓到**——测试套没有覆盖「账号中途掉线后能否复活」这条路径。

### 四、最终实现（v4）

| 场景 | 行为 |
|---|---|
| 请求路径 | **永不重启**。`acquire()` 只做 `_client_ready()` 纯布尔判断 |
| 启动阶段 | 有间隔重试 `startup_init_attempts` 轮（5s/15s/45s），全失败才退役 |
| 使用期间掉线 | 后台任务复活；连续 `restart_max_failures` 次失败 → 永久退役 |
| 复活成功 | 失败计数清零 |
| 已退役 | 再不尝试，直到容器重启 |
| `ClientBusyError` | 有在途请求属瞬时状态，**不计入**失败计数 |

**启动阶段为什么要间隔重试**：库自身的三次认证尝试全挤在 ~7 秒内。容器启动时 socks5 代理（同 stack 的 `warp`）可能还没起来，7 秒全落空，账号就被误判永久死亡。拉开间隔才能把「启动抖动」和「凭证是真的坏」区分开。启动跑在后台，多等几十秒对请求零影响。

**为什么不对 `AuthError` 单独判型**：`AuthError(Exception)` 是光板异常，不带状态码；库里 429 只出现在 debug 日志字符串中（`get_access_token.py:183`），代码只判 `status_code == 200`，429 从不作为值传出；抛给我们的消息连 "429" 字样都没有。要判就得扒库内部实现——正是 `use_responses_lite` / `tool_mode` 栽过两次的坑。而且连续失败计数本身已经把两类区分开了：认证错误稳定失败 N 次退役，网络抖动大概率第二次就成功并清零。

### 五、三处精化

**① 事件驱动复活（补 v4 自身的洞）**：轮询间隔 60s 意味着一次偶发异常会让账号**最多 60 秒不在轮换里**——而改动前是下次请求立刻复活。加 `_recovery_wanted` 事件，`pool.mark_unavailable()` 置位，后台任务 `wait_for_recovery_signal(timeout=interval)` 立刻被唤醒。实测唤醒延迟 **0.04ms**。轮询保留作兜底，接住那些没人通知、由库 watchdog 改 `account_status` 导致的掉线。

**② 退役状态可见**：`/health` 的 `clients` 里退役和临时掉线**都是 `false`，分不出来**，但两者需要的动作完全不同——临时掉线后台自己会救，退役必须人工改配置重启。新增 `retired: Sequence[str] | None` 字段单列。**不参与 `ok` 判定**：`adaptive` 模式下仍然是 `not any(...)`，即只有全部账号都挂才报 `ok=false`/503，挂一两个仍是 200。

**③ 启动与后台抢锁**：启动重试全程持锁，后台恢复会排队等待；等到锁时若该账号刚被退役，会多打一次无用的 7 秒重启。在锁内补一次 `_retired` 复查。

### 六、验证

| 项目 | 结果 |
|---|---|
| `scripts/check_deltas.sh` | **28 项**加固断言全绿 |
| 客户端生命周期专项 | **24 项**（含 v3 回归的专门用例） |
| 三处精化专项 | **12 项** |
| `/health` 语义专项 | **8 项**（挂 1 个仍 200，全挂才 503） |
| lifespan 起停 | 任务注册 / 事件唤醒 / 无泄漏 |
| `ruff check` + `format` + `ty` | 全清 |

`pyright` 报的 import 未解析是环境噪音——未改动的 `lmdb.py` 同样报 29 个同类错，它没接上 venv。

### 七、新增配置项（都可用 `CONFIG_GEMINI__*` 环境变量覆盖，不必重建镜像）

| 配置 | 默认 | 含义 |
|---|---|---|
| `startup_init_attempts` | 3 | 启动时尝试拉起一个账号的轮数，全失败才退役 |
| `restart_max_failures` | 3 | 使用期间连续后台复活失败几次后永久退役 |
| `restart_check_interval` | 60 | 后台恢复任务的**兜底**轮询间隔（正常由事件驱动，毫秒级） |

同批修复：`config/config.yaml` 的 `url_fetch_timeout` 从 15 改回 **30** 并加注释。`app/utils/config.py` 的默认值一直写着 30 并注明「本 fork 实际在跑并验证过的值」，但 YAML 里仍是上游的 15 且**静默覆盖**了它——加固被自己的配置文件抵消。系统比对全部 YAML 项与代码默认值，只有这一项是真失效（`max_request_body_bytes` 的 `268435456` 与 `256*1024*1024` 是同一个值）。

### 八、运维须知

**Gemini 账号必须从 0 连续编号。** 注释掉 `CONFIG_GEMINI__CLIENTS__0__*` 而保留 1、2 会**直接启动报错**（本人实测）。要摘掉某个账号只能重新编号，或者依赖上面的退役机制让它零成本闲置。


## 2026-08-20（晚）线上问题排查

上线 `20260820-120423` 后报了两个现象，逐个查证。

### 1. flash 模型思维链不显示 —— 不是 bug

实测（线上容器，同一句提问）：

| 提问 | gemini-pro | gemini-flash |
|---|---|---|
| 「9.11 和 9.9 哪个大」 | reasoning **0 字** | reasoning **0 字** |
| 过河问题（狼/羊/白菜） | reasoning 746 字 | reasoning **940 字** |

简单题目下**两个模型都不输出思维链**，难题下**两个都输出**，flash 甚至更多。配置侧 `extended_thinking=true` 生效，库里该参数统一置 `inner_req_list[80]=2`，不按模型区分。结论：Google 按题目难度决定是否思考，不是 flash 被关掉。

附带发现：上游改用动态模型注册表后，线上模型列表从 9 个（含 `-plus` / `-advanced` 思维等级变体）变成 3 个 —— `gemini-pro` / `gemini-flash` / `gemini-flash-lite`。`model_strategy` 配置项也被上游删除。

### 2. 生图时出现 `_451` / `_454` —— 真 bug，**三轮**才修对（`9acf234` → `d601814` → `6f58e2f`）

前两轮都在修一个文本根本不会以那种形态经过的位置，线上照漏不误。

**真正的根因**：`gemini_webapi` 在 `_parse_candidate` 里**先于我们所有代码**剥离 artifact URL，用的是它自己的正则（以 `\d+` 结尾）。Google 现在的路径是 `.../image_generation_content/0_452`，库吃掉 `0`、停在下划线，**把 `_452` 连同已被删除的 URL 一起交给我们** —— 我们的正则再正确也匹配不到任何东西，因为可匹配的部分早没了。

直接验证：
```
client 模块持有独立引用: True
库剥完剩下: 'x\n\n_452\n\n'   <- 我们只能看到这个
```

**修法**：在源头替换库的正则，`gemini_webapi.client` 和 `gemini_webapi.constants` 两处都要打（`client.py` 在 import 时绑定了名字，只打 constants 不生效）。

前两轮的改动保留：`9acf234` 的流式扣留和 `d601814` 的 `process_llm_output` 剥离本身都是对的、零成本，且能覆盖"未来库版本干脆不剥了"的情况。

**教训**：修之前必须确认**文本以什么形态经过我要改的那个位置**。前两轮我都验证了"我的正则能正确处理完整 URL"，但完整 URL 从来没到过那里。

### 存量脏数据清理（已授权执行）

新镜像只保证新对话干净，管不了已经落库的。LMDB 里 269 条对话中 **23 条**存着修复前的孤儿尾巴，续聊会被回放，还会当上下文喂回给模型。

两种形态：
```
"content":"_451\n\n![[Generated Image 0]]...     <- 正文以孤儿开头
ASSISTANT: _451\n</chat_history>                  <- 整条回复只有孤儿（回复本身只是图）
```

清理用的两条规则（作用在 JSON 转义后的字节上）：
```python
P1 = rb'"content":"_\d{2,4}(?:\\n)*'   -> b'"content":"'   # 连同其后的转义换行
P2 = rb'(?<![\w/])_\d{2,4}(?![\w])'     -> b''              # 其余位置只去 token
```
`P2` 的两个断言是关键：`(?<![\w/])` 挡掉 `img_4a85af…` 这类文件名，`(?![\w])` 挡掉更长的标识符，否则会把图片文件名和 token 一起吃掉。

执行顺序，每步都留了退路：
1. `cp -a` 全量备份到 `/root/revgemini/lmdb-backup-20260820-145955`（117M）
2. **空跑**打印改前/改后对照，确认两种形态都处理正确
3. **逐条校验**改动后 JSON 仍可解析 —— 23 条全通过，0 条会破坏结构
4. 读写打开（**保留锁**，容器仍在运行；LMDB 本身支持多进程并发）写入 23 条
5. 复核：269 条记录，仍含孤儿 **0** 条
6. 服务复查：容器 `running healthy`，存储 272 条可读，`/v1/models` 正常

没有停容器，服务全程未中断。

---

<details><summary>前两轮的分析（已被推翻，留作记录）</summary>

#### 第二轮（`d601814`，仍未命中）

第一轮诊断错了。我当时认为是流式增量把 artifact URL 逐块漏出，做了 `StreamingOutputFilter` 扣留（`9acf234`）。部署后仍然复现，且**非流式端点一模一样会漏** —— 说明泄漏在库返回的最终文本里就有，与流式无关。

**真正的原因**（关掉库的剥离、抓原始候选文本得到）：Google 现在发的 artifact URL 是

```
http://googleusercontent.com/image_generation_content/0_452
```

末段是 `0_452`（数字_数字）。而 gemini-webapi 的正则以 `\d+` 结尾：

```python
ARTIFACTS_RE = r"https?://googleusercontent\.com/(?:\w+/)+\d+\n*"
```

只吃掉 `0`，遇到下划线就停，`_452` 留在正文里。**Google 改了路径形状，库的正则没跟上。**

对比验证：
```
库的正则  -> '自画像。\n\n_452\n\n'
我们的正则 -> '自画像。\n\n'
```

**修法**：自己的 `ARTIFACT_URL_RE` 末段用 `[\w-]+` 吃整段，并放进 `process_llm_output` —— 这样非流式和存储路径一起覆盖，不只是流式。`9acf234` 的扣留逻辑保留（流式下仍需要），只是不够。

**为什么第一轮测试全绿却没发现**：我用的测试样本是旧的纯数字形式 `/451`，那个形状库的正则本来就能处理。现在测试用生产实测的形状，并保留一条旧格式做兼容。

---

<details><summary>第一轮的分析（已被推翻，留作记录）</summary>

**成因**：生图时 Google 正文里带一个 artifact URL，形如
`http://googleusercontent.com/image_generation_content/451`。
库在 `_parse_candidate` 里用 `ARTIFACTS_RE` 剥离它，但那个正则**要求匹配完整 URL**（末尾必须有 `\d+`），流式下 URL 还没传完就剥不掉，碎片被当正文逐块发出。

用库自己的差分函数复现，输出依次是：
```
delta='http:'
delta='//googleusercontent.com/imag'
delta='e_generation_co'
delta='ntent'
```
等最后一块凑齐能剥离时，碎片早已发给客户端 —— 这就是图片前面那个 `_451` / `_499`。

**修法**：`StreamingOutputFilter` 增加 artifact 扣留 —— 从可能的 URL 起点开始一律扣住，直到它凑成完整 URL（丢弃）或流结束（`flush` 释放，所以正文里以半截 URL 结尾的响应不会被吃掉）。

**一个易踩的细节**：结尾处的匹配不能剥。`ARTIFACTS_RE` 接受单个数字，`.../4` 就已经匹配，而 `51` 还在路上；此时剥离会让这两个字符漏成正文 —— 这正是第一版修法失败的原因。所以边缘匹配一律留到下一块或 flush 再定。

**测试**：`tests/test_artifact_filter.py` 覆盖 1~100 各种分块粒度、一条响应里多个 artifact、普通文本保真、以及流在 URL 中途结束。

</details>

测试补充：新增 `_452` 形状的流式与非流式用例。

</details>

**最终测试**：`test_library_pattern_is_replaced_at_source` 断言补丁确实生效，并加了守卫断言 —— 库升级或上游同步一旦把它弄丢，症状是每张生成图前面挂个孤零零的数字，这种毛病几个月都不会有人报。

### 产出

镜像 `ghcr.io/awaragml00029-debug/clean:20260820-145235`
digest `sha256:dab89a47f52208ddee349b6cebdd2f4d02e4dfb1b78dc1aeda037e8cec62c713`，已推 ghcr，**用户已上线并验证通过**。

验证：**213/213 测试通过**、ruff/ty/pyright 全清、守卫 **28/28**、镜像内实测库正则已被替换、剥离干净。

回滚：`20260820-143627` / `20260820-122943`（两者都仍会漏）、`20260805-091753`。

---

## 2026-08-20（下午）第二步：整体迁移到上游基线

分支 `migrate/upstream-20260819`，**已 fast-forward 合入 `main`（`de6f23d`）**。合并上游 `7d1744e`，50 个文件、**+11530 / −1918**。

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
digest `sha256:e26942a6f9e8094c626ed18b090be2150484ec08a0a4a63bbd41e6718c50039c`，已推 ghcr，镜像内 22/22 文件与 `main` 一致。**未上线 —— 上线由本人手动执行。**

合入 main 后复验：守卫 26/26、上游测试 188/188 全过。

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
