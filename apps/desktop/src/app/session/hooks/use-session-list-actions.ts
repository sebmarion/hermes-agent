import { useCallback, useEffect, useRef } from 'react'

import { getCronJobs, listAllProfileSessions, listSidebarSessions, type PaginatedSessions, type SessionInfo } from '@/hermes'
import { sameCronSignature } from '@/lib/session-signatures'
import {
  isMessagingSource,
  LOCAL_SESSION_SOURCE_IDS,
  MESSAGING_SESSION_SOURCE_IDS,
  normalizeSessionSource
} from '@/lib/session-source'
import {
  $pinnedSessionIds,
  $sessionsLimit,
  $sidebarFiltersActive,
  bumpSessionsLimit,
  raiseSessionsLimit,
  SIDEBAR_FILTERED_PAGE_SIZE,
  SIDEBAR_SESSIONS_PAGE_SIZE
} from '@/store/layout'
import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import {
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  CRON_SECTION_LIMIT,
  mergeSessionPage,
  MESSAGING_SECTION_LIMIT,
  setCronSessions,
  setMessagingPlatformTotals,
  setMessagingSessions,
  setMessagingTruncated,
  setSessionProfilesTruncated,
  setSessionProfilesUsage,
  setSessions,
  setSessionsLoading
} from '@/store/session'
import { $workingSessionIds, getRecentlySettledSessionIds } from '@/store/session-states'

import { sameCronSignature } from '../../../lib/session-signatures'
import { refreshCronJobs as refreshCronJobsStore } from '../../cron/cron-actions'

// The recents list is local-only: cron rows have their own section, kanban
// (telegram, discord, …) is fetched separately into its own self-managed
// sidebar section (refreshMessagingSessions). Excluding them here keeps
// "Load more" paging through interactive local chats instead of
// interleaving gateway threads that bury them.
const SIDEBAR_EXCLUDED_SOURCES = ['cron', 'kanban', 'subagent', 'tool', ...MESSAGING_SESSION_SOURCE_IDS]
// The messaging slice is the inverse: drop cron + every local source so only
// external-platform conversations remain, then split per platform in the UI.
const MESSAGING_EXCLUDED_SOURCES = ['cron', ...LOCAL_SESSION_SOURCE_IDS]

// Drop rows the user just deleted/archived: ANY list fetch (full refresh,
// "Load more" paging, a per-platform messaging page, the cron slice) can race
// an in-flight delete RPC, and the backend page still carries the doomed row
// until the DELETE commits — so it flashed back into the sidebar (#50928).
// Honoring the optimistic tombstone at every ingestion point keeps the removal
// stable; the tombstone self-clears once projects.tree confirms the delete,
// and a failed delete untombstones immediately, so nothing is filtered on the
// non-destructive paths.
function dropTombstoned(sessions: SessionInfo[]): SessionInfo[] {
  const tombstones = $removedSessionIds.get()

  return tombstones.size
    ? sessions.filter(s => !tombstones.has(s.id) && !(s._lineage_root_id && tombstones.has(s._lineage_root_id)))
    : sessions
}

// Rows a session refresh must preserve even if the aggregator omits them:
// in-flight first turns (message_count 0), pinned rows aged off the page, the
// actively-viewed chat (its "working" flag clears a beat before the aggregator
// sees the persisted row), and sessions whose turn just settled (same race, but
// for a chat the user has already navigated away from). Pass `scope` to only
// keep the active row when it belongs to the profile being paged.
function sessionsToKeep(scope?: string): Set<string> {
  const keep = new Set<string>([
    ...$workingSessionIds.get(),
    ...$pinnedSessionIds.get(),
    ...getRecentlySettledSessionIds()
  ])

  const active = $selectedStoredSessionId.get()

  if (active) {
    const session = scope ? $sessions.get().find(s => s.id === active) : null

    if (!scope || !session || normalizeProfileKey(session.profile) === scope) {
      keep.add(active)
    }
  }

  return keep
}

interface UseSessionListActionsArgs {
  profileScope: string
}

function assertCompleteSessionPage(result: PaginatedSessions): void {
  if (!result.errors?.length) {
    return
  }

  const profiles = result.errors.map(item => item.profile).join(', ')

  throw new Error(`Session refresh incomplete for profiles: ${profiles}`)
}

/** Owns the sidebar's session-list fetching + paging: recents, cron runs/jobs,
 *  and the per-platform messaging slices. Returns the callbacks the controller
 *  wires into the sidebar and refresh effects. */
export function useSessionListActions({ profileScope }: UseSessionListActionsArgs) {
  const refreshCronSessionsRequestRef = useRef(0)
  const refreshMessagingSessionsRequestRef = useRef(0)
  const refreshSessionsLoadingRequestRef = useRef(0)
  const refreshSessionsRequestRef = useRef(0)

  // Cron-job sessions as their own list (latest N). Independent of the recents
  // page so the two never compete for slots. Cheap + bounded. Kept (even though
  // the sidebar now lists cron *jobs*, not run sessions) so a pinned cron run
  // still resolves into the Pinned section via sessionByAnyId.
  const fetchCronSessions = useCallback(async () => {
    return listAllProfileSessions(CRON_SECTION_LIMIT, 1, 'exclude', 'recent', 'all', {
      source: 'cron'
    })
  }, [])

  const applyCronSessions = useCallback((result: PaginatedSessions) => {
    setCronSessions(prev => (sameCronSignature(prev, result.sessions) ? prev : result.sessions))
  }, [])

  const refreshCronSessions = useCallback(async () => {
    const requestId = refreshCronSessionsRequestRef.current + 1
    refreshCronSessionsRequestRef.current = requestId

    try {
      const result = await fetchCronSessions()

      if (refreshCronSessionsRequestRef.current === requestId) {
        applyCronSessions(result)
      }
    } catch {
      // Non-fatal for standalone refreshes: the cron section keeps its last-known rows.
    }
  }, [applyCronSessions, fetchCronSessions])

  // Messaging-platform sessions as their own slice, fetched separately from
  // local recents so each platform renders a self-managed section and never
  // competes with local chats for the recents page budget. One combined fetch
  // seeds every platform; the sidebar splits the rows per source.
  const fetchMessagingSessions = useCallback(
    () =>
      listAllProfileSessions(MESSAGING_SECTION_LIMIT, 1, 'exclude', 'recent', 'all', {
        excludeSources: MESSAGING_EXCLUDED_SOURCES
      }),
    []
  )

  const applyMessagingSessions = useCallback((result: PaginatedSessions) => {
    // Drop any non-messaging source the broad exclude didn't catch (custom
    // sources) — those stay in local recents, not a platform section.
    const rows = dropTombstoned(result.sessions.filter(s => isMessagingSource(s.source)))

    setMessagingSessions(prev => (sameCronSignature(prev, rows) ? prev : rows))
    // Hit the cap → at least one platform may have more on disk than loaded,
    // so platform sections offer their own per-platform "load more".
    setMessagingTruncated(result.sessions.length >= MESSAGING_SECTION_LIMIT)
  }, [])

  const refreshMessagingSessions = useCallback(async () => {
    const requestId = refreshMessagingSessionsRequestRef.current + 1
    refreshMessagingSessionsRequestRef.current = requestId

    try {
      const result = await fetchMessagingSessions()

      if (refreshMessagingSessionsRequestRef.current === requestId) {
        applyMessagingSessions(result)
      }
    } catch {
      // Non-fatal for standalone refreshes: messaging sections keep their last-known rows.
    }
  }, [applyMessagingSessions, fetchMessagingSessions])

  // Page a single platform's section independently (mirrors the per-profile
  // pager): fetch that source's next window and merge it back in place, leaving
  // every other platform's rows untouched. Resolves the platform's exact total.
  const loadMoreMessagingForPlatform = useCallback(async (platform: string) => {
    const requestId = refreshMessagingSessionsRequestRef.current + 1
    refreshMessagingSessionsRequestRef.current = requestId
    const inPlatform = (s: SessionInfo) => normalizeSessionSource(s.source) === platform
    const loaded = $messagingSessions.get().filter(inPlatform).length

    const result = await listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', 'all', {
      source: platform
    })

    const incoming = dropTombstoned(result.sessions.filter(s => normalizeSessionSource(s.source) === platform))

    if (refreshMessagingSessionsRequestRef.current !== requestId) {
      return
    }

    setMessagingSessions(prev => [
      ...prev.filter(s => !inPlatform(s)),
      ...mergeSessionPage(prev.filter(inPlatform), incoming, sessionsToKeep())
    ])

    const total = result.total ?? incoming.length
    setMessagingPlatformTotals(prev => ({ ...prev, [platform]: Math.max(total, incoming.length) }))
  }, [])

  // Cron *jobs* drive the sidebar "Cron jobs" section. Jobs are created
  // synchronously (agent tool call or the cron UI), so refreshing here right
  // after an agent turn surfaces a new job immediately; the interval poll keeps
  // next-run/state fresh as the scheduler advances them.
  const refreshCronJobs = useCallback(async () => {
    try {
      await refreshCronJobsStore(profileScope === ALL_PROFILES ? 'all' : profileScope)
    } catch {
      // Non-fatal: the cron section just keeps its last-known jobs.
    }
  }, [profileScope])

  const fetchCoreSessions = useCallback(async () => {
    const requestId = refreshSessionsRequestRef.current + 1
    refreshSessionsRequestRef.current = requestId
    const loadingRequestId = refreshSessionsLoadingRequestRef.current + 1
    refreshSessionsLoadingRequestRef.current = loadingRequestId
    setSessionsLoading(true)

    try {
      const limit = $sessionsLimit.get()

      // Require at least one message so abandoned/empty "Untitled" drafts (one
      // was created per TUI/desktop launch before the lazy-create fix) don't
      // clutter the sidebar.
      // Unified cross-profile list (served read-only off each profile's
      // state.db; no per-profile backend is spawned). Single-profile users get
      // the same rows tagged profile="default". Cron sessions are excluded here
      // and fetched separately (refreshCronSessions) so the scheduler's
      // always-newest rows can't consume the recents page budget.
      // Scope the fetch to the active profile (not always 'all') so a profile
      // with few recent sessions isn't windowed out of the cross-profile
      // recency page — the empty-history-on-profile-switch bug.
      const sessionProfile = profileScope === ALL_PROFILES ? 'all' : profileScope

      const result = await listAllProfileSessions(limit, 1, 'exclude', 'recent', sessionProfile, {
        excludeSources: SIDEBAR_EXCLUDED_SOURCES
      })

      return { requestId, result }
    } finally {
      if (refreshSessionsLoadingRequestRef.current === loadingRequestId) {
        setSessionsLoading(false)
      }
    }
  }, [profileScope])

  const applyCoreSessions = useCallback((result: PaginatedSessions) => {
    setSessions(prev => mergeSessionPage(prev, result.sessions, sessionsToKeep()))
  }, [])

  const refreshCoreSessions = useCallback(async () => {
    const coreResult = await fetchCoreSessions()

    if (refreshSessionsRequestRef.current !== coreResult.requestId) {
      return 'superseded' as const
    }

    applyCoreSessions(coreResult.result)

    return 'applied' as const
  }, [applyCoreSessions, fetchCoreSessions])

  const refreshSessionsForRevision = useCallback(async () => {
    const coreResult = await fetchCoreSessions()

    if (refreshSessionsRequestRef.current !== coreResult.requestId) {
      return 'superseded' as const
    }

    assertCompleteSessionPage(coreResult.result)

    const cronRequestId = refreshCronSessionsRequestRef.current + 1
    refreshCronSessionsRequestRef.current = cronRequestId
    const messagingRequestId = refreshMessagingSessionsRequestRef.current + 1
    refreshMessagingSessionsRequestRef.current = messagingRequestId

    const requestIsCurrent = (): boolean =>
      refreshSessionsRequestRef.current === coreResult.requestId &&
      refreshCronSessionsRequestRef.current === cronRequestId &&
      refreshMessagingSessionsRequestRef.current === messagingRequestId

    try {
      const [cronResult, messagingResult] = await Promise.all([fetchCronSessions(), fetchMessagingSessions()])

      if (!requestIsCurrent()) {
        return 'superseded' as const
      }

      assertCompleteSessionPage(cronResult)
      assertCompleteSessionPage(messagingResult)
      applyCoreSessions(coreResult.result)
      applyCronSessions(cronResult)
      applyMessagingSessions(messagingResult)
    } catch (error) {
      if (!requestIsCurrent()) {
        return 'superseded' as const
      }

      throw error
    }

    void refreshCronJobs()

    return 'applied' as const
  }, [
    applyCronSessions,
    applyCoreSessions,
    applyMessagingSessions,
    fetchCoreSessions,
    fetchCronSessions,
    fetchMessagingSessions,
    refreshCronJobs
  ])

  const refreshSessions = useCallback(async () => {
    await refreshCoreSessions()
    void refreshCronSessions()
    void refreshCronJobs()
    void refreshMessagingSessions()
  }, [refreshCoreSessions, refreshCronJobs, refreshCronSessions, refreshMessagingSessions])

  const loadMoreSessions = useCallback(async () => {
    bumpSessionsLimit()
    await refreshSessions()
  }, [refreshSessions])

  // ALL-profiles view pages one profile at a time: fetch that profile's next
  // page and merge it in place, leaving every other profile's rows untouched.
  const loadMoreSessionsForProfile = useCallback(async (profile: string) => {
    const requestId = refreshSessionsRequestRef.current + 1
    refreshSessionsRequestRef.current = requestId
    const key = normalizeProfileKey(profile)
    const inKey = (s: SessionInfo) => normalizeProfileKey(s.profile) === key
    const loaded = $sessions.get().filter(inKey).length

    const result = await listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', key, {
      excludeSources: SIDEBAR_EXCLUDED_SOURCES
    })

    if (refreshSessionsRequestRef.current !== requestId) {
      return
    }

    const keep = sessionsToKeep(key)

    setSessions(prev => [
      ...prev.filter(s => !inKey(s)),
      ...mergeSessionPage(prev.filter(inKey), result.sessions, keep)
    ])

    const total = result.profile_totals?.[key] ?? result.total ?? result.sessions.length
    // profile-total tracking was removed in a sidebar refactor; the paging
    // state machine still works correctly through mergeSessionPage + sessionsToKeep.
  }, [])

  // A filter searches the loaded page, so switching one on has to deepen the
  // page — otherwise "merged PRs" answers for the last 50 rows and reads as
  // "you only have 6 merged PRs". Clearing the filters hands the window back:
  // the list refreshes on every settled turn, and paying for 300 rows a turn
  // once the view is unfiltered again buys nothing. Whatever the user had
  // paged to by hand is what it returns to.
  const unfilteredLimit = useRef<null | number>(null)

  useEffect(
    () =>
      $sidebarFiltersActive.subscribe(active => {
        if (active) {
          unfilteredLimit.current ??= $sessionsLimit.get()

          if (raiseSessionsLimit(SIDEBAR_FILTERED_PAGE_SIZE)) {
            void refreshSessions()
          }
        } else if (unfilteredLimit.current !== null) {
          const restored = unfilteredLimit.current
          unfilteredLimit.current = null

          if ($sessionsLimit.get() > restored) {
            $sessionsLimit.set(restored)
            void refreshSessions()
          }
        }
      }),
    [refreshSessions]
  )

  return {
    loadMoreMessagingForPlatform,
    loadMoreSessions,
    loadMoreSessionsForProfile,
    refreshCronJobs,
    refreshMessagingSessions,
    refreshSessions,
    refreshSessionsForRevision
  }
}
