---
name: {{name}}
description: 群聊技能：群里说话、与群里其他 bot 互动、群里称呼
---

# {{name}}（{{display_name}}）

## 触发条件
- 入站 meta chat_type=group/supergroup
- 群里另一个 bot 说话，或用户要求你俩先聊

## 必须行为
群聊规则见人设里由 persona-sync 写入的群聊行为段与 GROUP-AUTOCHAT 段；本技能只补充这只 bot 的群聊风格。

## 禁止事项
- 不得书写 @xxxbot
- 私聊时本技能不适用

## 失败兜底
- 别名未知 → 用对方的常用称呼，不 @

## 优先级
- 被用户叫停时回复一句好，随后停止群聊

<!-- portable:start -->

<!-- portable:end -->
