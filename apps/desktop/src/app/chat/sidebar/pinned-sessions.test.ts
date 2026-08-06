import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { resolvePinnedSessions } from './pinned-sessions'

const session = (over: Partial<SessionInfo>): SessionInfo => ({
  archived: false,
  cwd: null,
  ended_at: null,
  id: 'session',
  input_tokens: 0,
  is_active: false,
  last_active: 0,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: null,
  source: null,
  started_at: 0,
  title: null,
  tool_call_count: 0,
  ...over
})

describe('resolvePinnedSessions', () => {
  it('resolves pinned rows from the full session set, not only the active profile slice', () => {
    const defaultPinned = session({ id: 'default-pin', profile: 'default' })
    const activeProfileRecent = session({ id: 'athena-recent', profile: 'hermes-athena-execution-specialist' })

    expect(resolvePinnedSessions(['default-pin'], [defaultPinned, activeProfileRecent])).toEqual([defaultPinned])
  })

  it('deduplicates a pinned lineage root to the live session tip', () => {
    const tip = session({ id: 'tip', _lineage_root_id: 'root' })

    expect(resolvePinnedSessions(['root', 'tip'], [tip])).toEqual([tip])
  })
})
