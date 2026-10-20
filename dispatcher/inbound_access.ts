/**
 * 入站发送者白名单判定（H-14 的纯函数版本，便于单测）。
 *
 * 群聊：群策略 allowFrom 非空 → 只认它；为空 → 回退到私聊 owner 名单；
 * owner 名单也为空 → 拒绝。绝不把“群 allowFrom 为空”解释成“所有人放行”。
 */
export type AccessLike = {
  allowFrom?: unknown
  groups?: Record<string, { allowFrom?: unknown } | undefined>
}

function ids(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : []
}

export function senderAllowed(
  access: AccessLike,
  chatType: string | undefined,
  chatId: string,
  fromId: string,
): boolean {
  if (chatType === 'group' || chatType === 'supergroup') {
    const pol = access.groups?.[chatId]
    if (!pol) return false
    const groupAllow = ids(pol.allowFrom)
    if (groupAllow.length > 0) return groupAllow.includes(fromId)
    const owners = ids(access.allowFrom)
    return owners.length > 0 && owners.includes(fromId)
  }
  return ids(access.allowFrom).includes(fromId)
}
