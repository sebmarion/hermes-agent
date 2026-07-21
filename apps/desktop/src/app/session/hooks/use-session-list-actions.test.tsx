import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getCronJobs, listAllProfileSessions, type SessionInfo } from '@/hermes'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setMessagingSessions,
  setSessions,
  setSessionsLoading
} from '@/store/session'

import { useSessionListActions } from './use-session-list-actions'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobs: vi.fn(),
  listAllProfileSessions: vi.fn()
}))

const getCronJobsMock = vi.mocked(getCronJobs)
const listSessionsMock = vi.mocked(listAllProfileSessions)

function session(id: string, source: string): SessionInfo {
  return {
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source,
    started_at: 1,
    title: id,
    tool_call_count: 0
  }
}

function sessionsPage(sessions: SessionInfo[] = [], errors?: Array<{ profile: string; error: string }>) {
  return { errors, limit: 50, offset: 0, profile_totals: {}, sessions, total: sessions.length }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(resolvePromise => {
    resolve = resolvePromise
  })

  return { promise, resolve }
}

describe('useSessionListActions refresh acknowledgement', () => {
  beforeEach(() => {
    getCronJobsMock.mockReset().mockResolvedValue([])
    listSessionsMock.mockReset()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    setSessionsLoading(false)
  })

  afterEach(() => {
    cleanup()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    setSessionsLoading(false)
    vi.restoreAllMocks()
  })

  it('reports a refresh as superseded when a newer core request fails', async () => {
    const olderCore = deferred<ReturnType<typeof sessionsPage>>()
    listSessionsMock
      .mockImplementationOnce(() => olderCore.promise)
      .mockRejectedValueOnce(new Error('newer refresh failed'))
      .mockResolvedValue(sessionsPage())

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))

    let olderRefresh!: ReturnType<typeof result.current.refreshSessionsForRevision>
    let newerRefresh!: ReturnType<typeof result.current.refreshSessionsForRevision>

    act(() => {
      olderRefresh = result.current.refreshSessionsForRevision()
      newerRefresh = result.current.refreshSessionsForRevision()
    })

    await expect(newerRefresh).rejects.toThrow('newer refresh failed')
    olderCore.resolve(sessionsPage())

    await expect(olderRefresh).resolves.toBe('superseded')
  })

  it('rejects acknowledgement when a revision-covered section refresh fails', async () => {
    listSessionsMock
      .mockResolvedValueOnce(sessionsPage())
      .mockRejectedValueOnce(new Error('cron sessions failed'))
      .mockResolvedValueOnce(sessionsPage())

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))

    await expect(result.current.refreshSessionsForRevision()).rejects.toThrow('cron sessions failed')
  })

  it('rejects a partial-success core payload without publishing it', async () => {
    const oldLocal = session('local-old', 'desktop')
    setSessions([oldLocal])
    listSessionsMock
      .mockResolvedValueOnce(
        sessionsPage([session('local-partial', 'desktop')], [{ profile: 'worker', error: 'database is locked' }])
      )
      .mockResolvedValue(sessionsPage())

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))

    await expect(result.current.refreshSessionsForRevision()).rejects.toThrow('incomplete')
    expect($sessions.get().map(row => row.id)).toEqual(['local-old'])
  })

  it('rejects a partial-success section payload before publishing any strict projection', async () => {
    setSessions([session('local-old', 'desktop')])
    setCronSessions([session('cron-old', 'cron')])
    setMessagingSessions([session('telegram-old', 'telegram')])
    listSessionsMock
      .mockResolvedValueOnce(sessionsPage([session('local-new', 'desktop')]))
      .mockResolvedValueOnce(
        sessionsPage([session('cron-partial', 'cron')], [{ profile: 'worker', error: 'database is locked' }])
      )
      .mockResolvedValueOnce(sessionsPage([session('telegram-new', 'telegram')]))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))

    await expect(result.current.refreshSessionsForRevision()).rejects.toThrow('incomplete')
    expect($sessions.get().map(row => row.id)).toEqual(['local-old'])
    expect($cronSessions.get().map(row => row.id)).toEqual(['cron-old'])
    expect($messagingSessions.get().map(row => row.id)).toEqual(['telegram-old'])
  })

  it('keeps optional section failures non-fatal for the existing refresh callback', async () => {
    listSessionsMock
      .mockResolvedValueOnce(sessionsPage())
      .mockRejectedValueOnce(new Error('cron sessions failed'))
      .mockResolvedValueOnce(sessionsPage())

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))

    await expect(result.current.refreshSessions()).resolves.toBeUndefined()
  })

  it('does not let superseded section payloads overwrite a newer applied generation', async () => {
    const oldCron = deferred<ReturnType<typeof sessionsPage>>()
    const oldMessaging = deferred<ReturnType<typeof sessionsPage>>()
    const newCron = session('cron-new', 'cron')
    const newMessaging = session('telegram-new', 'telegram')
    let request = 0

    listSessionsMock.mockImplementation(() => {
      request += 1

      switch (request) {
        case 1:

        case 4:
          return Promise.resolve(sessionsPage())

        case 2:
          return oldCron.promise

        case 3:
          return oldMessaging.promise

        case 5:
          return Promise.resolve(sessionsPage([newCron]))

        case 6:
          return Promise.resolve(sessionsPage([newMessaging]))

        default:
          return Promise.resolve(sessionsPage())
      }
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
    const olderRefresh = result.current.refreshSessionsForRevision()

    await vi.waitFor(() => expect(listSessionsMock).toHaveBeenCalledTimes(3))
    await expect(result.current.refreshSessionsForRevision()).resolves.toBe('applied')

    expect($cronSessions.get().map(row => row.id)).toEqual(['cron-new'])
    expect($messagingSessions.get().map(row => row.id)).toEqual(['telegram-new'])

    oldCron.resolve(sessionsPage([session('cron-old', 'cron')]))
    oldMessaging.resolve(sessionsPage([session('telegram-old', 'telegram')]))
    await expect(olderRefresh).resolves.toBe('superseded')

    expect($cronSessions.get().map(row => row.id)).toEqual(['cron-new'])
    expect($messagingSessions.get().map(row => row.id)).toEqual(['telegram-new'])
  })

  it('does not let older standalone section requests overwrite a revision refresh', async () => {
    const oldCron = deferred<ReturnType<typeof sessionsPage>>()
    const oldMessaging = deferred<ReturnType<typeof sessionsPage>>()
    const newCron = session('cron-new', 'cron')
    const newMessaging = session('telegram-new', 'telegram')
    let request = 0

    listSessionsMock.mockImplementation(() => {
      request += 1

      switch (request) {
        case 1:
          return Promise.resolve(sessionsPage())

        case 2:
          return oldCron.promise

        case 3:
          return oldMessaging.promise

        case 4:
          return Promise.resolve(sessionsPage())

        case 5:
          return Promise.resolve(sessionsPage([newCron]))

        case 6:
          return Promise.resolve(sessionsPage([newMessaging]))

        default:
          return Promise.resolve(sessionsPage())
      }
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
    await result.current.refreshSessions()

    await vi.waitFor(() => expect(listSessionsMock).toHaveBeenCalledTimes(3))
    await expect(result.current.refreshSessionsForRevision()).resolves.toBe('applied')

    oldCron.resolve(sessionsPage([session('cron-old', 'cron')]))
    oldMessaging.resolve(sessionsPage([session('telegram-old', 'telegram')]))
    await Promise.all([oldCron.promise, oldMessaging.promise])
    await Promise.resolve()

    expect($cronSessions.get().map(row => row.id)).toEqual(['cron-new'])
    expect($messagingSessions.get().map(row => row.id)).toEqual(['telegram-new'])
  })

  it('does not let older per-platform paging overwrite a revision refresh', async () => {
    const oldPlatformPage = deferred<ReturnType<typeof sessionsPage>>()
    const newMessaging = session('telegram-new', 'telegram')
    let request = 0

    listSessionsMock.mockImplementation(() => {
      request += 1

      switch (request) {
        case 1:
          return oldPlatformPage.promise

        case 2:

        case 3:
          return Promise.resolve(sessionsPage())

        case 4:
          return Promise.resolve(sessionsPage([newMessaging]))

        default:
          return Promise.resolve(sessionsPage())
      }
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
    const olderPage = result.current.loadMoreMessagingForPlatform('telegram')

    await vi.waitFor(() => expect(listSessionsMock).toHaveBeenCalledTimes(1))
    await expect(result.current.refreshSessionsForRevision()).resolves.toBe('applied')

    oldPlatformPage.resolve(sessionsPage([session('telegram-old', 'telegram')]))
    await olderPage

    expect($messagingSessions.get().map(row => row.id)).toEqual(['telegram-new'])
  })

  it('does not let older per-profile paging overwrite a revision refresh', async () => {
    const oldProfilePage = deferred<ReturnType<typeof sessionsPage>>()
    const newLocal = session('local-new', 'desktop')
    let request = 0

    listSessionsMock.mockImplementation(() => {
      request += 1

      switch (request) {
        case 1:
          return oldProfilePage.promise

        case 2:
          return Promise.resolve(sessionsPage([newLocal]))

        case 3:

        case 4:
          return Promise.resolve(sessionsPage())

        default:
          return Promise.resolve(sessionsPage())
      }
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
    const olderPage = result.current.loadMoreSessionsForProfile('default')

    await vi.waitFor(() => expect(listSessionsMock).toHaveBeenCalledTimes(1))
    await expect(result.current.refreshSessionsForRevision()).resolves.toBe('applied')

    oldProfilePage.resolve(sessionsPage([session('local-old', 'desktop')]))
    await olderPage

    expect($sessions.get().map(row => row.id)).toEqual(['local-new'])
  })

  it('clears core loading when per-profile paging supersedes the core apply generation', async () => {
    const corePage = deferred<ReturnType<typeof sessionsPage>>()
    listSessionsMock.mockImplementationOnce(() => corePage.promise).mockResolvedValue(sessionsPage())

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
    const coreRefresh = result.current.refreshSessionsForRevision()

    await vi.waitFor(() => expect(listSessionsMock).toHaveBeenCalledTimes(1))
    expect($sessionsLoading.get()).toBe(true)

    await result.current.loadMoreSessionsForProfile('default')
    corePage.resolve(sessionsPage())

    await expect(coreRefresh).resolves.toBe('superseded')
    expect($sessionsLoading.get()).toBe(false)
  })
})
