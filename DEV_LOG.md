# 开发日志

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
