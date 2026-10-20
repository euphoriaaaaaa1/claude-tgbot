---
name: {{name}}
description: 发语音技能：按这只 bot 的语音设置合成并发送
---

# {{name}}（{{display_name}}）

## 触发条件
- 收到语音消息（kind: voice）
- 你要发语音（as_voice=true）；或用户明确要/不要语音

## 必须行为
（VOICE-REPLY 段按本 bot 的语言与语音模型设置生成）

## 禁止事项
- 纯文本 reply（as_voice 非 true）里严禁出现 `[xxx]` 方括号标签
- 不得提及语音转文字的过程

## 输出格式
- 发语音时 `text` 与 `voice_text` 都要写，段数用空行对齐；只调一次 reply

## 失败兜底
- 语音没有转成文字时，请对方再说一遍

## 优先级
- 记忆里的 voice_preference=text_only 优先于一切触发规则

<!-- VOICE-REPLY-START -->
## 语音回复（TTS）
<!-- VOICE-REPLY-END -->

<!-- portable:start -->

<!-- portable:end -->
