/**
 * worker 插件按这只 bot 的功能开关给模型看的文字（bot-features 第一批 F1-5；INTERFACE-bot-features.md 3.1"运行规则一句"、3.4.4）。
 * 纯函数：不读文件、不读环境变量。开关由 worker-plugin.ts 经 features.ts 的 readFeatures 现读后传进来。
 */
import { FEATURE_LABELS, offList, type FeatureFlags } from './features'

/** 有功能关着时 MCP 说明里加的一句（文案固定，3.1）；列表按 3.0 第 2 条的顺序用中文名、以"、"连接。全开时为空串。 */
export function featuresOffRule(features: FeatureFlags): string {
  const off = offList(features)
  if (off.length === 0) return ''
  return `这只 bot 现在关掉了这些功能：${off.map(k => FEATURE_LABELS[k]).join('、')}。即使人设或技能里提到，也不要用；对方要的话，照实说现在用不了。`
}

const REPLY_DESC_HEAD = '回复消息。默认回到你正在处理的那条消息所在的聊天（群回群、私聊回私聊）；只有要主动跨聊天发时才传 chat_id。'
const REPLY_DESC_FILES = '带 files 时可在 text 中想发图的位置单独一行写 [[图1]]/[[图2]]（对应 files 第几张），图片就插在那两段文字之间发出，像真人聊天；不写标记则图片在所有文字之后发。'

/**
 * reply 工具的描述。语音开着时与改动前逐字相同（全开的 bot 读到的文字零变化）；
 * 关着时只做删减：去掉四个语音参数名与"标记不要写进 voice_text"，不加新说法（PLAN D2"描述里的语音说明一并去掉"）。
 */
export function replyDescription(voiceOn: boolean): string {
  return voiceOn
    ? `${REPLY_DESC_HEAD}text 必填；可选 chat_id、reply_to、files、as_voice、voice_text、voice_emotion、voice_instruct。${REPLY_DESC_FILES}标记不要写进 voice_text。`
    : `${REPLY_DESC_HEAD}text 必填；可选 chat_id、reply_to、files。${REPLY_DESC_FILES}`
}
