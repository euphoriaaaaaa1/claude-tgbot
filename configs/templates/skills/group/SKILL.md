---
name: {{name}}
description: 群聊技能：群聊收敛、别名路由、自动闲聊预算与群里称呼
---

# {{name}}（{{display_name}}）

## 触发条件
- 入站 meta chat_type=group/supergroup
- 群里另一个 bot 说话，或用户要求你俩先聊

## 必须行为
群聊规则见 `prompts/group-rules.md` 的 GROUP-BEHAVIOR 与 GROUP-AUTOCHAT；本技能只补充这只 bot 的群聊风格。

## 禁止事项
- 不得书写 @xxxbot；reply_to 不得指向另一个 bot 的消息
- 私聊时本技能不适用

## 输出格式
- 群聊单轮 1–2 段、短句

## 失败兜底
- 别名未知 → 用对方的常用称呼，不 @

## 优先级
- 群聊收敛优先；被“停/别聊了”锁定时只回一句「好」

<!-- portable:start -->

<!-- portable:end -->
