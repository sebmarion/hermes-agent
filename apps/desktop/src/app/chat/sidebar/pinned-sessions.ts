import type { SessionInfo } from '@/types/hermes'

export function resolvePinnedSessions(
  pinnedSessionIds: string[],
  sessions: SessionInfo[],
  cronSessions: SessionInfo[] = []
): SessionInfo[] {
  const sessionByAnyId = new Map<string, SessionInfo>()

  for (const session of [...cronSessions, ...sessions]) {
    sessionByAnyId.set(session.id, session)

    if (session._lineage_root_id && !sessionByAnyId.has(session._lineage_root_id)) {
      sessionByAnyId.set(session._lineage_root_id, session)
    }
  }

  const seen = new Set<string>()
  const out: SessionInfo[] = []

  for (const pinId of pinnedSessionIds) {
    const session = sessionByAnyId.get(pinId)

    if (session && !seen.has(session.id)) {
      seen.add(session.id)
      out.push(session)
    }
  }

  return out
}
