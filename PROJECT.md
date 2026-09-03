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
