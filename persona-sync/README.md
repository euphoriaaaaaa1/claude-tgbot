# persona-sync · 多 bot 共用规则段的同步工具（可选）

跑多个 bot 的人，总有些规则段**每个 bot 的人设都要有、内容又完全一样**——比如「语音回复怎么用」「群里和另一个 bot 怎么互动」。手改 N 份必然漂移：改了一份忘了另一份，过阵子几个 bot 行为就不一致了。

本工具把这类段落集中成**源文件**（`snippets/`），用 HTML 注释标记写进每个 bot 的 persona（`CLAUDE.md`），然后一条命令同步到所有目标：

- **幂等**：目标里已有整段 → 原样替换；没有 → 追加到文件末尾。重复跑不产生重复内容。
- **标记外不动**：persona 里标记之外的文字（每个 bot 自己的 override）逐字节不动。
- **可预览、可巡检**：`--dry-run` 先看会改什么再真跑；`--check` 挂 CI 或定时任务查漂移。

纯标准库，Python 3.10+，无第三方依赖。

```
persona-sync/
├── sync_snippet.py       同步脚本
├── test_sync_snippet.py  自检：python3 test_sync_snippet.py（pytest 也能收集）
├── snippets.json         配置：目标 persona 清单 + snippet 清单
└── snippets/
    ├── voice-reply.md        语音回复规则段（voice-bridge 配套，含双语模式约定）
    └── group-autochat.md     群里与另一个 bot 互动 / 自动闲聊规则段
```

## 两步上手

**① 编辑 `snippets.json`**

- `targets`：改成你自己 bot 的 persona 文件路径（每个 bot 一条）。脚本会逐条同步。
- `snippets`：列要同步的规则段。不需要的整条删掉——比如单 bot 用户通常用不到 `group-autochat`。

路径三种写法都支持：相对配置文件所在目录（例：`../channels/chenlulu/CLAUDE.md`）、`~` 开头、绝对路径。JSON 不支持注释，字段说明都写在 `_说明` 里（加载时忽略 `_` 开头的键）。

**② 先预览，再真跑**

```
python3 persona-sync/sync_snippet.py --dry-run    # 只打印会怎么改，不写盘
python3 persona-sync/sync_snippet.py              # 真跑
```

默认读脚本同目录的 `snippets.json`；要用别的配置就 `--config <路径>`。

## snippet 源文件的格式约定

每个源文件是一整段 markdown，外层用一对 HTML 注释标记包住：

```markdown
<!-- VOICE-REPLY-START · 说明文字随便写 -->

……规则正文……

<!-- VOICE-REPLY-END -->
```

- 标记名（`VOICE-REPLY-START` / `VOICE-REPLY-END`）由 `snippets.json` 的 `start` / `end` 指定，写进 persona 后也靠它定位。
- 替换范围 = START 注释开头到 END 注释结尾，**范围内的所有内容**都被源文件内容整体替换，所以正文随便改。
- 标记之外不动：bot 专属的 override 规则写在 END 标记之后，同步永远不碰。
- 同一对标记在一个文件里必须只出现一次。出现重复或不成对（只删掉了一半）时脚本报错并跳过该文件，不会猜、不会写。

## --check：挂 CI / 定时任务

`--check` 不写任何文件，只报告差异，退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 所有目标与源一致 |
| 1 | 存在差异（有目标没同步） |
| 2 | 配置或文件错误（配置读不了、目标路径不存在、标记不成对等） |

cron 例子（每天 9 点查一次漂移，有差异按你的方式告警）：

```cron
0 9 * * * cd /path/to/repo && python3 persona-sync/sync_snippet.py --check || echo "persona 漂移，去跑 sync_snippet.py 同步"
```

GitHub Actions 里直接当一步跑：`python3 persona-sync/sync_snippet.py --check`，非 0 自动挂。

## 已知限制与注意

- **只做整段替换/追加，不合并单行改动**：你在 persona 里直接改标记段内的文字，下次同步会被源文件整体覆盖。要改共用规则就改 `snippets/` 源文件；只有该 bot 专属的差异才写标记之外。
- 目标文件不存在时脚本报错退出（不会替你新建空人设——那多半是路径写错了）。
- 写盘是"临时文件 + rename"的原子写，中断不会写坏人设；原文件权限保留。
- `snippets/voice-reply.md` 与 `voice-bridge/persona-snippet.md` 内容同源：前者用于本工具自动同步，后者是 voice-bridge 文档里的人手并入说明，二选一即可。
