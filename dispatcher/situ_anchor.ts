/**
 * 入站消息的情境锚点：把"她此刻的学期状态 + 正在做的事"当作客观事实拼成一行，
 * 插在时间前缀之后、场景标注之前（顺序别改——摘要按第一个场景标注切正文）。
 *
 * 背景：学期/作息状态原先只进主动消息的提示词，被动聊天零锚点，用户一句"骗鬼呢"
 * 就能把 bot 的世界观带崩（实测 30 秒从"返校备课"改口成"暑假在家闲"）。
 *
 * 纯函数：不取时钟、不读文件、不碰环境。非字符串入参一律 TypeError——调用方本就
 * 该在桥那头兜底成空串，静默吞掉只会让锚点无声消失，出问题时更难查。
 */

/** 两者都空 → ''；否则 `（你此刻：<学期> · <活动>）\n`，缺哪项就少哪项。 */
export function formatSituLine(termLabel: string, activityName: string): string {
  if (typeof termLabel !== 'string') throw new TypeError(`formatSituLine: termLabel 必须是 string，收到 ${typeof termLabel}`)
  if (typeof activityName !== 'string') throw new TypeError(`formatSituLine: activityName 必须是 string，收到 ${typeof activityName}`)
  // 配置里的名字常带首尾空格；纯空白当空，免得拼出"（你此刻： · 备课）"这种废话
  const parts = [termLabel.trim(), activityName.trim()].filter(Boolean)
  return parts.length ? `（你此刻：${parts.join(' · ')}）\n` : ''
}
