# Changelog

## [1.3.0] — 2026-10-19

本轮把看板从「能用」推到「并发安全 + 可观测」：网页服务补上本机鉴权，采集层加锁并改成单飞，
TUI 采集搬进后台线程，守护进程不再可能误杀无关进程。条目按「问题 — 方案 — 验证」记录。

### 安全

**本机网页服务缺少鉴权与来源校验（P0-1，高危）**

- 问题：`board/server.py` 只绑 `127.0.0.1` 却不校验任何东西——`POST /api/focus`、`/api/send`、`/api/cleanup`
  都是「简单请求」，本机任意网页无需预检就能把提示词注入 Agent、关掉 Pane / Tab / 工作区。
- 方案：启动时生成（已有则复用）43 字符随机 token，以 `0600` 写入 `$HERDR_PORTAL_RUNTIME/token`
  （默认 `~/.local/state/herdr-portal/token`）；所有 `/api/*` 校验 `X-Herdr-Portal-Token`；
  `Host` 必须是 `127.0.0.1:<port>` / `localhost:<port>`（挡 DNS rebinding）；`POST` 若带 `Origin` / `Referer`
  必须同源；`open-web.sh` 把 token 带在 URL 上交给浏览器，前端存进 `sessionStorage` 后立刻从地址栏抹掉；
  服务端日志对 URL 里的 token 脱敏；新增仅限回环的 `/healthz` 供守护进程探活。
- 验证：无 token `POST /api/cleanup` → `403`；带 `Origin: https://evil.example` → `403`；
  `Host: evil.example:8787` → `403`；token 文件权限 `0600` 且不出现在 `server.log`；
  带 token 打开看板 → 地址栏已无 token、状态 `LIVE`、点卡片 `POST /api/focus 200`、回复框发送送达 pane。

### 新增

- **实时上下文面板**：底部面板跟着选中卡片走，表头（标题 / 框架 / 供应商 / 模型 / 子代理数 / 状态）
  → 阶段行 → 最近一次回复按面板宽度折行；最少 6 行、最多 14 行，`C` 切换上下文聚焦，`PgUp` / `PgDn`
  （或 `[` `]` `Space`、点击面板）翻页，新输出自动跟到最新一行。
- **会话标签色稳定**：8 个槽位由后端按 pane 固定分配（独立于状态色），pane 结束后超宽限期才释放；
  列内顺序改为按 pane_id 自然排序，刷新时卡片不再跳动。
- **今日用量面板**：`M` 打开模型维度的 token / 成本表 + 各供应商套餐余量条，成本分档高亮、负荷配色；
  网页顶栏同步显示今日 token 与成本读数，卡片角标补供应商 / 模型 / `⧉Sub Agent` 数。
- **后台子代理识别**：主 agent 让出控制权但子会话仍在写盘时，卡片显示「后台执行中 · N」，不再误报已完成。

### 稳定性

**采集层全局缓存并发不安全（P0-2，高危）**

- 问题：`board/server.py` 是 `ThreadingHTTPServer`（每个请求一个线程），而 `board/collect.py` 里
  `_session_cache`、`_SUBAGENT_OPEN` / `_SUBAGENT_SEEN_CALLS`、`_activity`、`_usage_memo` /
  `_file_usage_cache`、`_plan_memo`、颜色槽位状态全是无锁模块级全局：并发请求互相踩
  （丢子代理计数、读到半成品缓存、颜色槽位打架）。
- 方案：一把可重入锁 `_MEMO_LOCK` 保护这些 memo 的读—改—写（嵌套调用靠 RLock 免死锁）；
  采集加「单飞 + 1s TTL」——同一时刻只有一个线程真的跑 `collect()`，其余线程复用同一份快照。
  没有它，1.5s 一次的网页轮询 + 1Hz 的 TUI + 并发请求会各跑一遍完整采集（今日用量扫描实测 ~5s，
  全程占着 GIL），请求互相排队到超时；另外把监听队列 backlog 从 5 提到 128
  （并发时 accept 抢不到 GIL，队列一满内核直接 RST，请求白丢）。磁盘状态仍是原子写 `os.replace`。
- 验证：20 线程 × 200 次 `/api/snapshot` 并发压测，同时另开一个 TUI 进程：
  4000/4000 全部 `200` 且结构自洽（`total == len(items)`、`windows + agents == total`、
  状态计数之和 == `agents`、`pane_id` 不重复、`color_slot ∈ 0..7`、`quiet_seconds ≥ 0`），
  `server.log` 无 traceback、进程未重启；压测结束后 TUI 仍存活，方向键 0.79s 内重绘、`q` 0.20s 退出。
  调整 backlog 之前，同样的压测有 1164 次 `Connection reset by peer`。

**TUI 采集阻塞渲染（P1-3，中）**

- 问题：`board/tui.py` 主循环每次刷新都在渲染线程里同步跑 `collect_or_error()`——里面有 4s 超时的
  herdr 调用、会话文件读取与今日用量扫描，herdr 一慢整屏卡住，连按 `q` / 方向键都要等采集跑完。
- 方案：新增 `SnapshotFeed`，守护线程按 1Hz 采集（复用 `COLLECT_TTL` 窗口），主循环只渲染最新一份快照；
  首帧没有数据时渲染空列 + 表头「采集中…」；`R` 键与动作后的 `refresh(force=True)` 用 `kick()`
  叫醒采集线程立刻补采一次，按键处理不再依赖采集完成。
- 验证：把 herdr 换成 `sleep 3` 的桩（`HERDR_BIN_PATH`），首帧数据到位后连按 12 次方向键：
  旧版最差 4.90s 才有反应、按 `q` 要 4.54s 才退出；新版最差 0.13s、`q` 0.22s 退出（两者中位数均 0.03s）。

**daemon 停服务可能误杀无关进程（P1-4，中）**

- 问题：`board/daemon.py` 的 `stop` 直接对 pid 文件里的号发 `SIGTERM` → `SIGKILL`，不校验这个 pid
  现在还是不是本插件的 `server.py`：pid 复用（或 pid 文件过期）时会误杀无关进程；另外启动子进程时
  没有显式传 `HERDR_PORTAL_RUNTIME`，父子进程的 token / 日志目录只靠环境变量恰好一致。
- 方案：新增 `process_command()` / `is_portal_server()`——动手前用 `ps -ww -o command= -p <pid>`
  取命令行，必须同时命中 `python` 与 `board/server.py` 才继续；否则打印该 pid 的真实命令行、
  明确拒绝停止（返回 1）且不发任何信号。子进程 env 显式带上 `HERDR_PORTAL_RUNTIME`（连同 HOST / PORT）。
- 验证：把 pid 文件指向 `sleep 1000` 的进程 → `daemon.py stop` 退出码 1，stderr 打印
  「拒绝停止：pid … 不是本插件的 server.py」+「该 pid 的实际命令行: sleep 1000」，sleep 进程存活；
  pid 文件指回真正的 `server.py` 时同一套校验判定通过。另跑
  `HERDR_PORTAL_RUNTIME=/tmp/portal-rt-test2 HERDR_PORTAL_PORT=8791 daemon.py start`：
  `server.log` / `server.pid` / `token`(0600) 全部落在该目录、`/healthz` 正常应答、`stop` 干净退出，
  默认运行时目录不受影响。

**open-web.sh 的 AppleScript 注入面（P2-5，低-中）**

- 问题：脚本把 `HERDR_PORTAL_HOST` / `HERDR_PORTAL_PORT` 派生的 URL 直接拼进
  `osascript -e "…"` 的源码里；形如 `http://127.0.0.1:8787/" & (do shell script "touch /tmp/x") & "`
  的取值会被 AppleScript 当成表达式执行（实测这段 payload 确实触发了 `do shell script`）。
- 方案：改成 `osascript -e 'on run argv' … -e 'end run' -- "$url" "$open_url"`，
  两个 URL 通过 argv 传入，AppleScript 源码里不再出现任何外部字符串。
- 验证：同一段 payload 分别喂给两种写法——旧写法执行了 `touch /tmp/pwned_a`，新写法没有任何执行；
  正常环境下 `bash scripts/open-web.sh` 依旧复用 / 打开 Chrome 门户标签页，地址栏只剩
  `http://127.0.0.1:8787/`（token 已由前端抹掉）。注：脚本里 `daemon.py start` 挡在 osascript 之前，
  带引号的 HOST / PORT 会先让服务启动失败，因此这条改动是拆掉潜在 sink，而不是修补一个已被利用的路径。

**兜底异常静默吞掉（P2-6，中）**

- 问题：采集/服务/TUI 里到处是「失败也不影响看板」的兜底捕获，但既不记日志也不打印，
  出问题时没有任何线索（哪些失败是正常的降级、哪些是真 bug 分不出来）。
- 方案：新增 `board/logutil.py`：`HERDR_PORTAL_DEBUG=1` 时把带上下文的失败与堆栈写到 stderr
  （TUI 因为 curses 占屏，改写到 `$HERDR_PORTAL_RUNTIME/tui-debug.log`），默认完全安静；
  `collect()` 里两处重复捕获收敛成 `_optional()` 一个入口；`logutil.note()` 只接受 `Exception`，
  `KeyboardInterrupt` / `SystemExit` 依旧由调用点原样抛出（全仓无 `except BaseException`）。
  顺手把运行时目录的解析收敛到 `logutil.runtime_dir()`（daemon / server / TUI 一处定义）。
- 验证：把 herdr 换成桩、让某个会话文件 `chmod 000` —— `HERDR_PORTAL_DEBUG=1` 时 stderr 出现
  `[herdr-portal] 读取会话文件失败 /tmp/dbg-session/broken.jsonl` 与完整 `PermissionError` 堆栈；
  不设该变量时 stderr 为 0 字节、退出码 0，看板数据与之前完全一致（blocked 卡片照常渲染）；
  TUI 在 debug 模式下把同样的堆栈写进 `tui-debug.log`，屏幕上没有任何多余输出。

### 测试与 CI

**纯逻辑没有回归网（P2-7）**

- 问题：`normalize_item` / `build_activity` / `classify_window` / 静默判定 / `send_text` 分支这些
  纯逻辑没有任何测试，改一行就可能悄悄改掉卡片语义；也没有 CI 兜底。
- 方案：新增 `tests/test_collect.py`（27 个用例，标准库 unittest，全部 fixture：临时目录 + 假会话文件 +
  `mock.patch` 掉 `run_herdr` / `collect`），覆盖窗口与 Agent 卡片的归一化、会话里的模型/供应商/
  用量/子代理账本、后台子代理升级「后台执行中」、静默计时（revision 变才重新计时、消失即清理、落盘）、
  `send_text` 的两条分支与三类拒绝路径、pane_id 自然排序；另加 `.github/workflows/ci.yml`，
  在 Linux + macOS 上用 Python 3.9 / 3.13 跑 `python3 -m py_compile board/*.py`、
  `bash -n scripts/*.sh` 与 `python3 -m unittest discover -s tests`。
- 验证：本机 Python 3.14.7 与系统 Python 3.9.6 上 `python3 -m unittest discover -s tests`
  均 27 passed；`py_compile` 两个版本都通过；workflow 的 YAML 用 `YAML.load_file` 解析校验通过。
