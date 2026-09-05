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
  ㉒ stub_cliproxy.aliases_enabled() 改读 prefix 语义；㉓ 验收 A8 补断言 force-model-prefix；㉔㉙ 按 §12.4 备案同步（routing 判定按文本、secret-key 禁字面比对）。
| BUG-12 | M6 systemd 分支 grep 子串匹配 | 已修复 | test-m6-r2 / dev-m6 | 20093e8 |
| BUG-13 | M4 resolve_active：MODEL 非字符串未归 null（契约㉑另一半），int/dict/bool 原样回 | 已修复 | test-m4-r2 / dev-m4 | 5334847 |
| BUG-14 | M4 方括号内非法 IPv6 字面量当域名放行（[gggg::1] 等应 400；仅脏数据非 SSRF） | 已修复 | test-m4-r2 / dev-m4 | df14583 |
| BUG-15 | M0 爆破防护半修：只签浏览 cookie 的成功分支仍 reset 滑窗→交替登录可无限猜 admin | 已修复 | test-m0-r2 / dev-m0 | 1fc82dc |
| BUG-16 | M0 project() url 打码带 isinstance(str) 前置，dict/list 型 base-url 原样出站 | 已修复 | test-m0-r2 / dev-m0 | 009b3bd |
- 第一批合并完成 @76b58cb：M0/M4/M6 零冲突并入，集成基线 验收 253红/100绿/3skip、unit 302 全绿。
  worktree 已清，放 M1（阻塞项）@../ctgb-m1。
- M1 骨架并入 @merge（验收净+23 绿、页面组 15/15；其行为全被锁定验收覆盖故免独立实测轮）。放 M2 @../ctgb-m2。
- M2 并入：V1–V4 真机 9/9 过（prefix 互斥实证），unit 390 全绿零回归。真机五发现已注释落字
  （桩/真机形状差 unwrap_list 兼容、未知模型 400 非 502、healthz 无版本、GET /v0/management/config 回明文 key 禁调、
  secret-key 启动即 bcrypt 化）。两偏离备案：set_alias_exclusive 整条替换语义、COMPAT 缺失取 1。
  放最后三线：M3/M5/M7 @../ctgb-{m3,m5,m7}。
- M7 交付 @ph-m7：bots 组 30/31 绿（1 skip=180s 慢用例）。两偏离采纳待备案：restart_available 不判 X_OK
  （install 是 cp 644+bash 执行，判 X_OK 会误报缺失）；互斥/job 表挂 app.extensions（等价进程内且测试不串扰）。
  集成交接：合并后 web.py:454 _BOT_PORTS 改 from bots_client import BOT_PORTS（int/str 拼 URL 均可）。
  注意：test_unconfigured 有一条会对本机 17801 发只读 POST /status（锁定用例无法规避，无副作用）。
- M5 交付 @ph-m5：oauth 42/42 绿，全量 156绿/197红（余红全是 M3 未填段）。三留白待 planner 定死：
  ①无 model_aliases 的账户 activate→暂 400 bad_alias ②state 不匹配/超窗→暂 400 bad_body ③DELETE auth-files
  入参形状未实测（真机核验项，归验收前单点核验）。state 表进程内存（单进程够用，注释有升级路径）。
- §12.5 定死：oauth_no_alias/oauth_state_expired 专码（M5 需由 400 临时码改 409 专码，攒至其实测后一并）；
  DELETE auth-files=真机核验项 R1（核验前不放行）；M7 两偏离据实入文。出题人批次至 10 项（+㉚㉛㉝）。
- M3 交付 @ph-m3：验收 80红/273绿/3skip（crud 104/104、native 10/10、selftest 12/12），unit 447 全绿。
  activate 5 红=桩 disabled 语义（批次㉒触发条件已齐）。
  **环境事故**：Clash TUN fake-IP(198.18.x)使 SSRF 把一切域名判内网→开 TUN 机器加不了任何 provider，
  测试用进程内 nofakedns 插件抵消（未改仓库）。待 planner 裁决 §3.3 fake-IP 段策略。
- §12.6 fake-IP 裁决=方案A（字面量恒拦/198.18.0.0/15 解析判作废）。**合并后集成批**待办：
  ①provider_model 实现两层判定（T4–T6 用例随批次）②web.py:454 _BOT_PORTS 收拢到 bots_client
  ③M5 专码 400→409 两处（oauth_no_alias/oauth_state_expired）。
- M7 实测零缺陷 ready-to-merge（探针 74 全绿：注入 32/并发/撕裂/10MB tail/探活上限）。两上限告知不改：
  tail 全量缓冲 100MB→+310MB RSS（重启脚本自吐才触发）；探活 ≥16 卡死 bot 时队尾误报 offline（本仓 1 bot）。
  跑法注意：tests/unit 与 acceptance/provider_hub 两份顶层 conftest 重名，同一次 pytest 收集会 ImportError，须分开跑（验收阶段照此）。
| BUG-17 | M5 专码未同步：state 失配/无 alias 应 409 oauth_state_expired/oauth_no_alias（§12.5） | 已修复 | test-m5 / dev-m5 | 10d63f5 |
| BUG-18 | M5 非对象 JSON 体→500（body.get 惯用法，§0.1 应 400；M3/M7 同法需排查） | 已修复 | test-m5 / dev-m5 | 6eaf1c3 |
- §12.7：state 成功消费即作废（二次 409）；activatable=合法且有 alias（新增 not_activatable_reason）；
  非 dict 体→400 提为 §0.1 通则。集成批追加④：M3/M7 段收 body 端点按通则排查收口。
| BUG-19 | M3 并发 activate 互清（40轮31轮双输双200，bot 全502）：B 段快照读改写无互斥 | 已修复 | test-m3 / dev-m3 | cd32013 |
| BUG-20 | M3 haiku 双启用盲区 | 已修复 | test-m3 / dev-m3 | 740b898 |
- P4 实锤批次㉒（桩 disabled 语义致 5 假红，prefix 镜像全绿）；新矛盾：§3.6 reconcile 断言 vs §3.0d 禁清 disabled，待裁。
- M5 ready-to-merge @fb5de94（四件套+㊱㊲+锁接入=7 commit；实测互斥对翻转即复验；unit 497/oauth 42）。
- **第四次落锁（终局）@df607f2**：验收 408 用例 405绿/0红/3skip，unit 635 全绿；
  终修 f911189（version 读文件）。进入 05 裁判盲判。
- **卡点3 前最后一针 @214033d**：实拍抓出 supported_kinds 缺失（原用例只断言"包含"，
  字段整缺也过）→ 实现补齐+锁 1 条恒等断言，crud 123 全绿。第五次落锁。裁判判定：合格（附人工清单）。
- **卡点3 已确认 @bf1ac05 2026-09-04**：裁判合格（附 5 条人工验证清单）；
  S5/S8 离线达成，S1-S4/S6/S7 终点动作归人工清单。进入 05.5 安全审计 + 质量抽查。
- 质量抽查（05 Step5）：致命 2 / 重要 6 / 建议 7（报告以正文归档于本表下列编号；正文另存
  .devflow/CODE-REVIEW-provider-hub.md 由主会话转存）：
| BUG-21 | 致命：出货三启动路径均不 source hub.env → HUB_ACCESS_PASSWORD 恒空 → gate 恒关 → 全裸奔 | 修复中 | 抽查 / dev-int | |
| BUG-22 | 致命：PATCH 换 kind 先删后加，中途失败丢用户 key 无补偿 | 修复中 | 抽查 / dev-int | |
| BUG-23 | 重要：CRUD 三端点不持锁+过期 index，可把 A 整条覆盖到 B（含 key） | 修复中 | 抽查 / dev-int | |
| BUG-24 | 重要：reconcile 挂蓝图注册，bot 进程 import 即触发+可被拖秒+双进程互写 | 修复中 | 抽查 / dev-int | |
| BUG-25 | 重要：reconcile 中途失败无补偿，留 0 enabled 更坏态 | 修复中 | 抽查 / dev-int | |
| BUG-26 | 重要：unwrap 空默认喂给写路径整块 PUT，升级换形状会静默抹全部 provider | 修复中 | 抽查 / dev-int | |
| BUG-27 | 重要：重启线程 start 失败 _running 永卡 409 | 修复中 | 抽查 / dev-int | |
| BUG-28 | 重要：systemd WorkingDirectory 无引号+体检照样绿；sed 未防 #/& | 修复中 | 抽查 / dev-int | |
- **第六次落锁·停点 @035ec83**：双审合并批+追针批全清（BUG-21~28+安审S2-S4+抽查9-15），
  验收 406绿/0红/3skip、unit 702 全绿。两遗留采纳：①手写disabled暂复用 delete_active 409（专码留补丁版）
  ②启动闸在 install() 内 503 化、exit 仍归 __main__。**按用户指示停在卡点 4 之前，不发布**；
  发布时人工清单 5 条见 ACCEPT-REPORT。
| BUG-21~28 | 双审八缺陷 | 已修复 | 双审 / dev-int | bc7e30c..1fc4d34 |

## provider-hub 二期（参数页+加bot）
- 卡点1已确认 @2026-09-04：PLAN-hub2 428行/INTERFACE-hub2 559行；盲审5致命全闭环
  （版本乐观锁/yaml.compose 行级替换保真/端口legacy兜底+最小空闲/C段探活判成/token唯一落点.env 0600）。
- 卡点2 锁定 @e4655ff：hub2 验收 419 条（保存参数174/加bot 63/高级26/备份16/保真14/CLI15/其余），
  红基线 411/8 双序一致；§13 八裁决落文。
- N2 交付 @7ad16ca：289/289 绿，unit 754，一期 406 冻结基线无损。集成待办：①web.py:685 老接口 safe_dump
  改调 hub_config.set_scalar（F2 同病一期路径）②test_pages_hub2 未登录 302 用例补 Accept 头（用例侧，归出题人）。
- **二期发布 @a331064（2026-09-04，master 已推 GitHub）**：N3 加bot+两集成针合并后，判官初审 FAIL
  （锁前移吞掉终局用例改动=构造性恒真）→ 整改为修订账本式锁（BASE e4655ff + R1~R5 带裁决号入库，
  .devflow/LOCK-hub2 纳入版本管理）→ 复核 PASS。终局：hub2 验收 420 绿 / 一期 406绿3skip / unit 891 绿。
- 发布前双专项：①安全审计 2高3中4低全修+复核抓漏 splice 根因去重（别名 YAML 写坏密钥）；
  ②部署链路 11 项（install.sh 口令引导独立化防零鉴权、README Windows 段补 install-cliproxy.ps1、
  emoji×cp936 编码链、.env 串味防双 bot 抢 token、HUB_TRUST_PROXY 走 env_file、moments-web plist 注册等）
  ＋ UTF-8 两批（subprocess/open 显式 encoding、ps1 PYTHONUTF8 根治层）。
- 遗留（非阻断）：moments-web plist Label 沿用 com.example.*（改名需旧标签检测防双起）；
  Windows ps1 无 pwsh 本机语法验证，仅人工比对；一期人工清单 5 条仍待用户。

## 2026-09-04 install.sh 第②步在 Homebrew Python 上报 externally-managed-environment（另一台 Mac 实测）
- 根因：PEP 668 标记（Homebrew/Debian 的 python3 自带 `EXTERNALLY-MANAGED`）禁止 `pip install` 往系统 site-packages 装。本项目十几处入口直接调 `python3` 不走 venv，故改为按标记文件判断后装到用户 site（`--user --break-system-packages`），装完立刻 `import yaml, feedparser, requests, flask` 自检。本机用 /opt/homebrew/bin/python3 + 临时 PYTHONUSERBASE 复现并验证通过；相关 55 条测试绿。README 两处手动 pip 命令同步加注。
- 2026-09-04 install.sh 第④步改为向导：三把密钥逐项说明"缺什么 → 怎么拿(@BotFather / @userinfobot / platform.deepseek.com) → 填哪(文件+键)"，终端里当场粘贴即写进文件（格式校验，错了重问，回车跳过）；写文件走 scripts/setup_keys.py（YAML 行级替换保留注释 / .env 替换或追加 + chmod 600 / JSON 保留其余键）；可单独重跑 `bash scripts/setup_keys.sh`（`--check` 只查不问）。测试 tests/unit/test_setup_keys.py 7 条 + expect 真 TTY 驱动验证（错一次再填对、三项全写入）。restart-bots.sh/stop-bots.sh 进 .gitignore（install 复制出的本机文件不再让工作区显示 ✗）。（文件名不能含 secret：.gitignore 的 `**/*secret*` 会把它忽略，故叫 setup_keys）
- 单bot停启 @2026-09-05：公开版 enabled:false 生效化（bots()过滤、端口按全表防挤号、hub bots页停止/启用按钮、
  重启脚本统一杀旧）+ 私有版 stop-bot.sh/start-bot.sh + TG命令 /stopbot /startbot（仅私聊白名单）。
  残留债：停用bot的 self-initiate launchd/计划任务未注销（拉不回进程，仅重启用时补发一条陈旧主动消息）。
