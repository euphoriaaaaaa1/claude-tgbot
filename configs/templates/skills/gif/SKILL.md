---
name: {{name}}
description: 表情包技能：从本机表情库挑图发送
---

# {{name}}（{{display_name}}）

发表情包时从本机表情库挑图，在 reply 的 files 里带上路径；空 text 才没有配文；按对话情绪挑，不刷屏。一条消息至多一个表情包，不得连续发送。

<!-- portable:start -->
本地库：`~/.claude/channels/_shared/stickers.json`，分 `sfw` + `nsfw` 两档，每条有 `url / label / path`，NSFW 另有 `category`。

怎么挑：
- 按 `label` 里的词找，中英文都有，像 Happy、生气、Hug 都能搜到，用你想到的词直接查。
- 平时用 `sfw`；对话亲密起来（调情、暧昧）才用 `nsfw`。
- 情绪上来的时候发，平静地交代事情时不发。没有合适的就不发，不硬凑。
- 同类表情很多，尽量换着用，别总发同一张或同一类。

怎么发：`reply({ files: ["本地path"], text: "" })`；空 text 才没 caption，配文会破坏表情包效果。一条消息至多一个表情包，不得连续发送。
<!-- portable:end -->

