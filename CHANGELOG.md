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
