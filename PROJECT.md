# PROJECT · claude-tgbot（公开版）状态账本

> 唯一写者 = 主会话；所有子代理只读。每过一个卡点追加一行。

## 进行中：provider 管理台（provider-hub，标准任务·完整七阶段）

需求：常驻多协议路由端口（集成 cliproxy）+ 统一管理网页 `/hub`（模型来源 / 朋友圈 / bot 状态三块）+ 全站鉴权，三平台。
产物：`.devflow/BRIEF-provider-hub.md`、`RESEARCH-{代码地图,cliproxy,门户鉴权}.md`、`PLAN/INTERFACE-provider-hub.md`、`REVIEW{,-arch}-provider-hub.md`

### 阶段进度

- 2026-09-03 调研×3 完成（代码地图 583 行 / cliproxy 417 行 / 鉴权 22KB）。
  鉴权调研顺带发现 **/image/ 公网任意文件读**（自 5 月存在），当日两版修复：公开版 `3abaafd`、私有版 `4ebd7f6`。
- 2026-09-03 方案盲审×2（四维度 + 架构）：致命 7 / 重要 20 / 建议 12，方案代理逐条处置
  （PLAN 704 行 / INTERFACE 740 行 / 处置表 150 行）。要点：install 只管骨架不再抹用户 key、
  单一真相源收敛 cliproxy（删 hub.json 台账）、activate 三段式 + 启动 reconcile、
  三类来源拆分（第三方 key / OAuth 账户 / Claude 原生）、两条开工硬门槛 S0-A/S0-B。
- **卡点1 已确认 @2026-09-03**：用户三项拍板——
  ① 鉴权 = 双口令可选式（`HUB_ADMIN_PASSWORD` 未设退化单口令，允许与 `ACCESS_PASSWORD` 同值）
  ② S6 不丢消息 = 按既有机制能力验收（磁盘 inbox 补投即过）
  ③ 第三方 Anthropic 兼容**必须**进统一页面：S0-B 失败不缩水，改走 `anthropic-direct` fallback
  （settings.json 三键直写真上游；Gemini 兼容允许不对称缩水）。定案小修回传 planner 落盘中。

### 已知欠账（本期外）

- `cleanup.plist.tmpl` / `daily-wildcard.plist.tmpl` 引用的脚本在仓库不存在（代码地图发现）
- install.sh 的 ⑤自启仅注册 1/6 个 plist；Linux 无自启；Windows 无安装脚本只有 register-tasks.ps1
- cliproxy 错误请求会把完整 prompt 明文落盘（上游行为，本期用日志三件套缓解 + README 告知）
- **卡点2 已确认 @a986046 2026-09-03**：验收测试锁定 340 用例（313红/24绿/3skip，
  乱序一致；TEST-PLAN 563 行留本地 .devflow）。锁定 hash 记 .devflow/LOCK-provider-hub（本地，.devflow 被公开仓 ignore 属既有约定）。
  下一步：开工硬门槛 S0-A（真 claude 带 tool_use 经 cliproxy v7）/ S0-B（anthropic/gemini 兼容配置块探明）。
- **测试重锁 @1b0596f**：S0 双门槛通过（.devflow/RESEARCH-spike-S0.md，382 行，8 处实测修正
  含 CLAUDE_SETTINGS_PATH 非 CLI 变量的红线级纠正）；haiku 双 alias 契约变更由出题人修题后重锁，
  356 用例（328红/25绿/3skip）。施工版 PLAN 835 行 / INTERFACE 1155 行。开发放行。
- 04 开发启动 @eddfc4c：3 条 worktree 并行（上限3，压掉方案里的4）——
  [✓] M0 鉴权门+骨架  ready-to-merge @009b3bd（轮2 探针 r1 16/0、r4 30/0 翻绿；unit 42 绿）
  [✓] M4 纯函数层     ready-to-merge @df14583（轮2五缺陷确认+两小修被复测方探针 88 条钉住全绿；unit 226/m4probe 227）
  [✓] M6 安装与常驻   ready-to-merge @20093e8（轮2三修复硬验全过+BUG-12 单测钉住；unit 34 绿/探针 56 绿）
  串行队列：M1 空壳骨架（M0 合并后）→ M2 客户端 → M3/M5/M7 → M8。桩已随测试锁定交付，M2 起只读复用。
- M0→M1 交接备忘：feed/styles 各补一行 href="/hub"（归 M1）；/hub/healthz 已在 hub_auth.install() 注册，M1 勿重复。
  M0 自主裁决待 planner 备案：404/405 JSON 化范围含 /api/*。.gitignore 待补 state.db*（合并时主会话处理）。
  基线数字注意：test_startup_gate 起真子进程有 ±5 抖动（两方独立观测到），验收阶段以干净环境连跑两次为准。
- M6 备忘：A12/C1（门户 plist 自动注册）因需动白名单外的 moments-web.plist.tmpl 与 run_moments_web.sh
  （后者写死 cd $HOME/claudebotlife 是既有 bug）未做——归 M8/合并后主线。白名单外增补 run-cliproxy.ps1
  （B4 硬断言依据）与 .gitignore 两行，接受。顺手修：bash 变量紧跟中文标点在 C locale 吃字节的真 bug。
- 契约裁决 §3.0d @INTERFACE 1250 行：互斥改 per-entry prefix（禁写 disabled 键）+ M2 真机必验 V1–V4；
  haiku 并集判占用（第二个 provider 创建即停用=预期）；active.alias 恒回 env 原值；空 settings 等同不存在。
  待分发：dev-m4 改①③两处口径；出题人改⑲⑳㉑三处后第三次落锁（攒到三路实测齐再发）。
### Bug 台账（provider-hub 开发期）
| 编号 | 现象 | 状态 | 发现/负责 | 修复 commit |
|---|---|---|---|---|
| BUG-01 | config 骨架缺 force-model-prefix | 已修复 | test-m6 / dev-m6 | c658370 |
| BUG-02 | install.sh ⑥体检误报就绪 | 已修复 | test-m6 / dev-m6 | 65b5430 |
| BUG-03 | Linux unit 口令注入 cliproxy 进程 | 已修复 | test-m6 / dev-m6 | 55f840d |
| BUG-04 | M4 from_block_entry 数字型字段 TypeError 崩（mask_key/derive_id） | 已修复 | test-m4 / dev-m4 | 69948e7 |
| BUG-05 | M4 不配对方括号 URL：urlsplit 在 try 外 → 500 而非 400 bad_base_url | 已修复 | test-m4 / dev-m4 | 11bce78 |
| BUG-06 | M4 非 UTF-8 settings 裸 UnicodeDecodeError（只 catch OSError） | 已修复 | test-m4 / dev-m4 | 716dc5a |
| BUG-07 | M4 孤代理字符落盘裸 UnicodeEncodeError，绕过 HubError 补偿契约 | 已修复 | test-m4 / dev-m4 | 904bda4 |
| BUG-08 | M4 id 派生分隔符可注入：不同四元组同 id f9b581f6 | 已修复 | test-m4 / dev-m4 | e4a8af1 |
| 口径 | §3.0d prefix 互斥+active.alias 回原值 | 已修复 | planner / dev-m4 | 9b46a28 |
| 文档 | CGNAT 段/零宽 RTL/BOM 自愈/软链语义/"归一化"名不副实 → 待 planner 补文 | 待处理 | test-m4 / planner | |
| BUG-09 | M0 admin 口令零爆破防护，猜错反清空全站限速计数 | 已修复 | test-m0 / dev-m0 | d7d1e9c |
| BUG-10 | M0 只设 admin 不设 access → /hub 裸奔且无告警 | 已修复 | test-m0 / dev-m0 | ce2caf4 |
| BUG-11 | M0 管理锁 startswith 未归一化路径（今日被 404 兜住，反代下成真绕过） | 已修复 | test-m0 / dev-m0 | be36ea8 |
| 备忘 | M0→M1 交接：cliproxy_client 异常消息勿携上游响应体（traceback 进日志）；#4 老路由抹敏更严格属向安全偏离→planner 备案 | 待处理 | test-m0 | |
- 出题人待改批次（攒单，下次一并发→第三次落锁）：⑲ 桩 prefix 断言点、⑳ 连建多 provider 期望、㉑ active.alias 期望、
  ㉒ stub_cliproxy.aliases_enabled() 改读 prefix 语义；㉓ 验收 A8 补断言 force-model-prefix（复测发现该键修复前后验收同绿=测不到）。
| BUG-12 | M6 systemd 分支 grep 子串匹配 | 已修复 | test-m6-r2 / dev-m6 | 20093e8 |
| BUG-13 | M4 resolve_active：MODEL 非字符串未归 null（契约㉑另一半），int/dict/bool 原样回 | 已修复 | test-m4-r2 / dev-m4 | 5334847 |
| BUG-14 | M4 方括号内非法 IPv6 字面量当域名放行（[gggg::1] 等应 400；仅脏数据非 SSRF） | 已修复 | test-m4-r2 / dev-m4 | df14583 |
| BUG-15 | M0 爆破防护半修：只签浏览 cookie 的成功分支仍 reset 滑窗→交替登录可无限猜 admin | 已修复 | test-m0-r2 / dev-m0 | 1fc82dc |
| BUG-16 | M0 project() url 打码带 isinstance(str) 前置，dict/list 型 base-url 原样出站 | 已修复 | test-m0-r2 / dev-m0 | 009b3bd |
- 第一批合并完成 @76b58cb：M0/M4/M6 零冲突并入，集成基线 验收 253红/100绿/3skip、unit 302 全绿。
  worktree 已清，放 M1（阻塞项）@../ctgb-m1。
