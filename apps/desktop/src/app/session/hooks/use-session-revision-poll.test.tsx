import { act, cleanup, renderHook } from '@testing-library/react'
import { type PropsWithChildren, StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getAllProfileSessionsRevision } from '@/hermes'
import { ALL_PROFILES } from '@/store/profile'

import { useSessionRevisionPoll } from './use-session-revision-poll'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getAllProfileSessionsRevision: vi.fn()
}))

const revisionProbe = vi.mocked(getAllProfileSessionsRevision)

function revision(value: string) {
  return { profiles: ['default'], revision: value }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

async function settleAsyncWork(rounds = 16): Promise<void> {
  await act(async () => {
    for (let index = 0; index < rounds; index += 1) {
      await Promise.resolve()
    }
  })
}

function StrictModeWrapper({ children }: PropsWithChildren) {
  return <StrictMode>{children}</StrictMode>
}

describe('useSessionRevisionPoll', () => {
  let resumeCallback: (() => void) | undefined
  let removePowerResumeListener: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.useFakeTimers()
    revisionProbe.mockReset()
    resumeCallback = undefined
    removePowerResumeListener = vi.fn()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        onPowerResume: vi.fn((callback: () => void) => {
          resumeCallback = callback

          return removePowerResumeListener
        })
      }
    })
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('refreshes once on the first probe and not again for an unchanged acknowledged revision', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(revisionProbe).toHaveBeenCalledTimes(2)

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(revisionProbe).toHaveBeenCalledTimes(3)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('normalizes the real all-profiles UI sentinel for the backend revision API', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: ALL_PROFILES, refreshSessions }))
    await settleAsyncWork()

    expect(revisionProbe).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenNthCalledWith(1, 'all')
    expect(revisionProbe).toHaveBeenNthCalledWith(2, 'all')
  })

  it('refreshes a changed revision by the next five-second tick', async () => {
    revisionProbe
      .mockResolvedValueOnce(revision('r1'))
      .mockResolvedValueOnce(revision('r1'))
      .mockResolvedValueOnce(revision('r2'))
      .mockResolvedValueOnce(revision('r2'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()
    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(4)
  })

  it('retries the same dirty revision after refresh failure', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))

    const refreshSessions = vi.fn().mockRejectedValueOnce(new Error('refresh failed')).mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(3)
  })

  it('retries when the requested refresh was superseded before it applied', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))

    const refreshSessions = vi.fn().mockResolvedValueOnce('superseded').mockResolvedValueOnce('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(revisionProbe).toHaveBeenCalledTimes(1)

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(3)
  })

  it('keeps the revision dirty when the confirmation probe fails', async () => {
    revisionProbe
      .mockResolvedValueOnce(revision('r1'))
      .mockRejectedValueOnce(new Error('confirmation failed'))
      .mockResolvedValueOnce(revision('r1'))
      .mockResolvedValueOnce(revision('r1'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()
    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(4)
  })

  it('coalesces ticks and follows a commit that lands during refresh', async () => {
    revisionProbe
      .mockResolvedValueOnce(revision('r1'))
      .mockResolvedValueOnce(revision('r2'))
      .mockResolvedValueOnce(revision('r2'))
      .mockResolvedValueOnce(revision('r2'))
    const firstRefresh = deferred<void>()
    let activeRefreshes = 0
    let maxActiveRefreshes = 0
    let refreshCall = 0

    const refreshSessions = vi.fn(async () => {
      refreshCall += 1
      activeRefreshes += 1
      maxActiveRefreshes = Math.max(maxActiveRefreshes, activeRefreshes)

      try {
        if (refreshCall === 1) {
          await firstRefresh.promise
        }
      } finally {
        activeRefreshes -= 1
      }

      return 'applied' as const
    })

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    expect(revisionProbe).toHaveBeenCalledTimes(1)

    firstRefresh.resolve()
    await settleAsyncWork(32)

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(4)
    expect(maxActiveRefreshes).toBe(1)
  })

  it('does not overlap old and new profile generations', async () => {
    revisionProbe
      .mockResolvedValueOnce(revision('default-r1'))
      .mockResolvedValueOnce(revision('worker-r1'))
      .mockResolvedValueOnce(revision('worker-r1'))
    const oldRefresh = deferred<void>()
    let activeRefreshes = 0
    let maxActiveRefreshes = 0
    let refreshCall = 0

    const refreshSessions = vi.fn(async () => {
      refreshCall += 1
      activeRefreshes += 1
      maxActiveRefreshes = Math.max(maxActiveRefreshes, activeRefreshes)

      try {
        if (refreshCall === 1) {
          await oldRefresh.promise
        }
      } finally {
        activeRefreshes -= 1
      }

      return 'applied' as const
    })

    const { rerender } = renderHook(
      ({ profileScope }) => useSessionRevisionPoll({ enabled: true, profileScope, refreshSessions }),
      { initialProps: { profileScope: 'default' } }
    )

    await settleAsyncWork()
    rerender({ profileScope: 'worker' })
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(revisionProbe).toHaveBeenCalledTimes(1)

    oldRefresh.resolve()
    await settleAsyncWork(32)

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(3)
    expect(revisionProbe).toHaveBeenNthCalledWith(2, 'worker')
    expect(maxActiveRefreshes).toBe(1)
  })

  it('preserves the replacement generation single-flight marker in StrictMode', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))
    const replacementRefresh = deferred<void>()
    let activeRefreshes = 0
    let maxActiveRefreshes = 0

    const refreshSessions = vi.fn(async () => {
      activeRefreshes += 1
      maxActiveRefreshes = Math.max(maxActiveRefreshes, activeRefreshes)

      try {
        if (refreshSessions.mock.calls.length === 1) {
          await replacementRefresh.promise
        }
      } finally {
        activeRefreshes -= 1
      }

      return 'applied' as const
    })

    renderHook(() => useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions }), {
      wrapper: StrictModeWrapper
    })
    await settleAsyncWork()
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    await settleAsyncWork()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(maxActiveRefreshes).toBe(1)

    replacementRefresh.resolve()
    await settleAsyncWork(32)
  })

  it('does not acknowledge an old profile generation', async () => {
    const oldConfirmation = deferred<ReturnType<typeof revision>>()
    revisionProbe
      .mockResolvedValueOnce(revision('shared'))
      .mockImplementationOnce(() => oldConfirmation.promise)
      .mockResolvedValueOnce(revision('shared'))
      .mockResolvedValueOnce(revision('shared'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    const { rerender } = renderHook(
      ({ profileScope }) => useSessionRevisionPoll({ enabled: true, profileScope, refreshSessions }),
      { initialProps: { profileScope: 'default' } }
    )

    await settleAsyncWork()
    expect(revisionProbe).toHaveBeenCalledTimes(2)

    rerender({ profileScope: 'worker' })
    oldConfirmation.resolve(revision('shared'))
    await settleAsyncWork(32)

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(revisionProbe).toHaveBeenCalledTimes(4)
    expect(revisionProbe).toHaveBeenNthCalledWith(3, 'worker')
  })

  it('does not start when disabled', async () => {
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    renderHook(() => useSessionRevisionPoll({ enabled: false, profileScope: 'all', refreshSessions }))
    await settleAsyncWork()
    await act(async () => vi.advanceTimersByTimeAsync(10_000))

    expect(revisionProbe).not.toHaveBeenCalled()
    expect(refreshSessions).not.toHaveBeenCalled()
    expect(window.hermesDesktop.onPowerResume).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('probes immediately on power resume without adding an interval', async () => {
    revisionProbe.mockResolvedValue(revision('r1'))
    const refreshSessions = vi.fn().mockResolvedValue('applied')

    const { unmount } = renderHook(() =>
      useSessionRevisionPoll({ enabled: true, profileScope: 'all', refreshSessions })
    )

    await settleAsyncWork()

    expect(vi.getTimerCount()).toBe(1)
    expect(resumeCallback).toBeTypeOf('function')
    await act(async () => resumeCallback?.())
    await settleAsyncWork()

    expect(revisionProbe).toHaveBeenCalledTimes(3)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(1)

    unmount()
    expect(removePowerResumeListener).toHaveBeenCalledTimes(1)
  })
})
