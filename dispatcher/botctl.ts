/** /stopbot <bot> | /startbot <bot> 的纯解析层（独立模块：dispatcher.ts 顶层即运行，
 *  测试没法 import 它——解析逻辑放这里，两边共用）。 */
/**
 * 可管理的 bot 名单。公开版不硬编码任何 bot：由部署方经 DISPATCHER_CTL_BOTS
 * （逗号分隔）注入。名单为空时所有目标都得到用法提示，命令不会误放行。
 */
export const CTL_BOTS: readonly string[] = Object.freeze(
  (process.env.DISPATCHER_CTL_BOTS || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean),
)

/** 纯解析层：目标名单由调用方注入，便于单测与按配置接线。 */
export function parseBotCtlWithBots(
  trimmed: string, bots: readonly string[],
): { cmd: 'stopbot' | 'startbot'; target: string } | { usage: string } | null {
  const m = trimmed.match(/^\/(stopbot|startbot)(?:@\w+)?(?:\s+(.+?))?\s*$/)
  if (!m) return null
  const cmd = m[1] as 'stopbot' | 'startbot'
  const parts = (m[2] ?? '').trim().split(/\s+/).filter(Boolean)
  const target = (parts[0] ?? '').toLowerCase()
  // 段数不是 1 或目标不认识 → 都给用法提示（多余参数不放行进 AI，命令就该在这层终结）
  if (parts.length !== 1 || !bots.includes(target)) {
    const known = bots.length > 0 ? `，可选：${bots.join(' / ')}` : '（尚未配置可管理的 bot）'
    return { usage: `用法：/${cmd} <bot>${known}` }
  }
  return { cmd, target }
}

/** 生产入口：名单取 DISPATCHER_CTL_BOTS。 */
export function parseBotCtl(trimmed: string): { cmd: 'stopbot' | 'startbot'; target: string } | { usage: string } | null {
  return parseBotCtlWithBots(trimmed, CTL_BOTS)
}

/** 公开版 /stopbot /startbot 的回复：不 spawn 任何脚本，只指向 8767 管理台。 */
export function botCtlNotice(ctl: { cmd: 'stopbot' | 'startbot'; target: string }): string {
  const action = ctl.cmd === 'stopbot' ? '停用' : '启用'
  return `公开版请在 8767 管理台${action}这只 bot（${ctl.target}）。`
}

/** /model [编号|模型名] | /provider [编号|供应商名|全局]：查看 / 切换本 bot 的模型与供应商（实际执行在
 *  公开版的 provider 管理脚本）。只取首行、截 200 字；arg 为空串 = 列出。不是这两个命令 → null。 */
export function parseProviderCmd(trimmed: string): { kind: 'model' | 'provider'; arg: string } | null {
  if (PROVIDER_ADD_HEAD.test(trimmed)) return null   // 带密钥的添加命令另走 parseProviderAdd，绝不当成供应商名
  const m = trimmed.match(/^\/(model|provider)(?:@\w+)?(?:\s+([\s\S]*))?$/)
  if (!m) return null
  return { kind: m[1] as 'model' | 'provider', arg: (m[2] ?? '').split('\n')[0].trim().slice(0, 200) }
}

/** /provider add [名字 地址 密钥 [anthropic|openai]]：可能带 API 密钥。识别不看 SLASH_RE、允许前置 @某bot 与 /provider@某bot，
 *  调用方据此在一切路由之前拦下（不进 AI、不进群路由）。只取首行。不带参数 = 逐步问（像 BotFather 建 bot）；带了几段就按
 *  名字→地址→密钥→协议的顺序当作已答（prefill），正好 3 段时协议取默认 anthropic，一次发全的老用法照旧一步到位；
 *  超过 4 段 → 用法提示；不是这个命令 → null。 */
const PROVIDER_ADD_HEAD = /^(\s*(?:@\w+\s+)?\/provider(?:@\w+)?\s+add)(?=\s|$)/i
export const PROVIDER_ADD_USAGE = '用法：发 /provider add 按提示逐步填写；或一次发全：/provider add 名字 地址 密钥 [openai]'
export function parseProviderAdd(text: string): { prefill: string[] } | { usage: string } | null {
  const m = PROVIDER_ADD_HEAD.exec(text)
  if (!m) return null
  const parts = text.slice(m[0].length).split('\n')[0].trim().split(/\s+/).filter(Boolean)
  if (parts.length > 4) return { usage: PROVIDER_ADD_USAGE }
  return { prefill: parts.length === 3 ? [...parts, 'anthropic'] : parts }
}

/** 逐步添加供应商：按 名字 → 地址 → 密钥 → 协议 的顺序问，草稿里缺哪项就问哪项 */
export type AddStep = 'name' | 'baseUrl' | 'apiKey' | 'protocol'
export type AddDraft = Partial<Record<AddStep, string>>
const ADD_STEPS: AddStep[] = ['name', 'baseUrl', 'apiKey', 'protocol']
export const ADD_GREETING = '好，来添加一个新的供应商，一共 4 步。随时发 /cancel 取消；30 分钟不回复也会自动作废。'
export const ADD_PROMPTS: Record<AddStep, string> = {
  name: '第 1/4 步：给它起个名字，以后发 /provider 名字 就能切过去。',
  baseUrl: '第 2/4 步：发它的接口地址，以 http:// 或 https:// 开头。',
  apiKey: '第 3/4 步：发它的 API 密钥。收到后会马上删掉那条消息。',
  protocol: '第 4/4 步：选接口协议，回数字：\n1. anthropic（默认）\n2. openai',
}
const ADD_CHECKS: Record<AddStep, (a: string) => string | { error: string }> = {
  name: a => a && !a.includes('\n') && a.length <= 50 ? a : { error: '名字要写在一行里，50 个字以内。' },
  baseUrl: a => /^https?:\/\/\S+$/i.test(a) ? a : { error: '地址要以 http:// 或 https:// 开头，中间不能有空格。' },
  // 只收可见 ASCII：中文聊天句子、带空格换行的内容都不会被当成密钥收下
  apiKey: a => /^[\x21-\x7e]+$/.test(a) ? a : { error: '密钥只能由英文字母、数字和符号组成，不能有空格或换行。' },
  protocol: a => /^(1|anthropic)$/i.test(a) ? 'anthropic' : /^(2|openai)$/i.test(a) ? 'openai' : { error: '请回 1 或 2。' },
}

export function nextAddStep(d: AddDraft): AddStep | null {
  return ADD_STEPS.find(s => !d[s]) ?? null
}

/** 依次收下回答（逐条消息时只有一条；/provider add 带参数时是预填的几段），遇到第一条不合格的就停，
 *  返回已收下的草稿和错误说明。名字那步用 isTaken 查重：重名当场让换，不等填完四步才说。 */
export async function feedAddAnswers(draft: AddDraft, answers: string[],
  isTaken: (name: string) => Promise<boolean>): Promise<{ draft: AddDraft; error?: string }> {
  for (const answer of answers) {
    const step = nextAddStep(draft)
    if (!step) break
    const v = ADD_CHECKS[step](answer.trim())
    if (typeof v !== 'string') return { draft, error: v.error }
    if (step === 'name' && await isTaken(v)) return { draft, error: `已经有叫「${v}」的供应商了，换一个名字。` }
    draft = { ...draft, [step]: v }
  }
  return { draft }
}

/** 逐步添加的会话管理：每个私聊最多一个进行中的流程，只存内存（dispatcher 重启即作废）。
 *  收发消息、删消息、查重、真正新建都由调用方注入（dispatcher.ts 顶层即运行、测试没法 import，状态机放这里才能单测）。 */
export type AddFlowIO = {
  send: (chatId: string, text: string) => Promise<void>
  del: (chatId: string, messageId: number) => Promise<boolean>
  isTaken: (name: string) => Promise<boolean>
  run: (d: Required<AddDraft>) => Promise<string>   // 新建并拉模型列表，返回回执
  now?: () => number
}
export const ADD_FLOW_TTL_MS = 30 * 60_000   // 上一次提问后 30 分钟没回就作废，之后的消息照常进 AI
const FLOW_SLASH_RE = /^\/[a-zA-Z][\w-]*(\s|$)/   // 与 dispatcher 的 SLASH_RE 同一条规则

export function createAddFlows(io: AddFlowIO) {
  const flows = new Map<string, { draft: AddDraft; at: number }>()
  const now = io.now ?? Date.now
  const active = (chatId: string) => {
    const f = flows.get(chatId)
    if (f && now() - f.at > ADD_FLOW_TTL_MS) { flows.delete(chatId); return undefined }
    return f
  }
  // 收下回答再问下一步；四项齐了就收尾。lead = 删消息的说明，greet = 流程刚开始、要发开场白
  const advance = async (chatId: string, draft: AddDraft, answers: string[], lead: string, greet: boolean) => {
    const r = await feedAddAnswers(draft, answers, io.isTaken)
    const next = nextAddStep(r.draft)
    if (next) {
      flows.set(chatId, { draft: r.draft, at: now() })
      await io.send(chatId, [lead, greet ? ADD_GREETING : '', r.error ?? '', ADD_PROMPTS[next]].filter(Boolean).join('\n\n'))
      return
    }
    flows.delete(chatId)
    const d = r.draft as Required<AddDraft>
    await io.send(chatId, [lead, `正在添加「${d.name}」并拉取模型列表…`].filter(Boolean).join('\n\n'))
    void io.run(d).then(out => io.send(chatId, out)).catch(() => {})   // 拉模型要连外网，不占着更新循环
  }
  return {
    /** 这个私聊正等哪一步；没有进行中的流程 → null（日志据此决定要不要隐去正文） */
    pendingStep: (chatId: string): AddStep | null => { const f = active(chatId); return f ? nextAddStep(f.draft) : null },
    /** 该私聊是否有进行中的添加流程（过期自动清理）。/provider、/model 菜单向导据此做互斥（PLAN §4①）。 */
    isActive: (chatId: string): boolean => active(chatId) !== undefined,
    /** 结束该私聊的添加流程（菜单向导互斥/让路时清理；没有流程时 no-op，不抛）。 */
    abort: (chatId: string): void => { flows.delete(chatId) },
    /** /provider add：prefill = 命令里带的几段（一次发全就直接新建），lead = 删原消息的说明 */
    start: (chatId: string, prefill: string[], lead = '') => advance(chatId, {}, prefill, lead, true),
    /** 私聊来了一条消息。返回 true = 当作这一步的回答处理掉了（不进 AI、不进任何记录）；false = 不归流程管，照常往下走。
     *  /cancel 或"取消" → 作废；发了别的斜杠命令 → 放弃这次添加，返回 false 让那条命令照常执行。 */
    async onMessage(chatId: string, msg: { text: string; isText: boolean; messageId?: number }): Promise<boolean> {
      const f = active(chatId)
      if (!f) return false
      const step = nextAddStep(f.draft)
      const t = msg.text.trim()
      if (/^\/cancel(@\w+)?$/i.test(t) || t === '取消') {
        flows.delete(chatId)
        await io.send(chatId, '已取消，没有添加供应商。')
        return true
      }
      if (!step || FLOW_SLASH_RE.test(t)) { flows.delete(chatId); return false }
      let lead = ''
      if (step === 'apiKey') {   // 等密钥这一步：发来的不管是什么都先删（可能就是密钥，哪怕格式不对）
        const deleted = msg.messageId != null && await io.del(chatId, msg.messageId)
        lead = deleted ? '那条消息已删除。' : '那条消息没能删除，请手动删掉它（可能含密钥）。'
      }
      if (!msg.isText) {   // 图片、语音、贴纸等：这一步只收文字
        f.at = now()
        await io.send(chatId, [lead, '这一步要发文字。', ADD_PROMPTS[step]].filter(Boolean).join('\n\n'))
        return true
      }
      await advance(chatId, f.draft, [t], lead, false)
      return true
    },
  }
}

/** 日志与群聊记录用：添加命令只留到 "add"，后面（名字、地址、密钥）一律换成占位；别的消息原样返回 */
export function redactProviderAdd(text: string): string {
  const m = PROVIDER_ADD_HEAD.exec(text)
  return m ? `${m[1]} <参数已隐去>` : text
}
