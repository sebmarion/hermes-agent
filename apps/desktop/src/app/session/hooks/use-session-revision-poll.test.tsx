import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getAllProfileSessionsRevision } from '@/hermes'

import { useSessionRevisionPoll } from './use-session-revision-poll'

vi.mock('@/hermes', () => ({
  getAllProfileSessionsRevision: vi.fn()
}))

interface Deferred<T> {
  promise: Promise<T>
  reject: (error: Error) => void
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })

  return { promise, reject, resolve }
}

function revision(value: string) {
  return { profiles: ['default'], revision: value }
}

function Harness({
  enabled = true,
  profileScope = 'default',
  refreshSessions
}: {
  enabled?: boolean
  profileScope?: string
  refreshSessions: () => Promise<void>
}) {
  useSessionRevisionPoll({ enabled, profileScope, refreshSessions })
  return null
}

const getRevision = vi.mocked(getAllProfileSessionsRevision)
let resume: (() => void) | undefined
let removeResumeListener: ReturnType<typeof vi.fn>

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function tick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000)
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  getRevision.mockReset()
  resume = undefined
  removeResumeListener = vi.fn()
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      onPowerResume: vi.fn((callback: () => void) => {
        resume = callback
        return removeResumeListener
      })
    }
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('useSessionRevisionPoll', () => {
  it('refreshes once on the first probe and not again for an unchanged acknowledged revision', async () => {
    getRevision.mockResolvedValue(revision('a'))
    const refreshSessions = vi.fn(async () => undefined)

    render(<Harness refreshSessions={refreshSessions} />)
    await flush()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(getRevision).toHaveBeenCalledTimes(2)

    await tick()

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(getRevision).toHaveBeenCalledTimes(3)
  })

  it('refreshes a changed revision by the next five-second tick', async () => {
    getRevision
      .mockResolvedValueOnce(revision('a'))
      .mockResolvedValueOnce(revision('a'))
      .mockResolvedValueOnce(revision('b'))
      .mockResolvedValueOnce(revision('b'))
    const refreshSessions = vi.fn(async () => undefined)

    render(<Harness refreshSessions={refreshSessions} />)
    await flush()
    await tick()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(getRevision).toHaveBeenCalledTimes(4)
  })

  it('retries the same dirty revision after refresh failure', async () => {
    getRevision.mockResolvedValue(revision('a'))
    const refreshSessions = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('refresh failed'))
      .mockResolvedValue(undefined)

    render(<Harness refreshSessions={refreshSessions} />)
    await flush()
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await tick()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(getRevision).toHaveBeenCalledTimes(3)
  })

  it('keeps the revision dirty when the confirmation probe fails', async () => {
    getRevision
      .mockResolvedValueOnce(revision('a'))
      .mockRejectedValueOnce(new Error('confirm failed'))
      .mockResolvedValue(revision('a'))
    const refreshSessions = vi.fn(async () => undefined)

    render(<Harness refreshSessions={refreshSessions} />)
    await flush()
    await tick()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(getRevision).toHaveBeenCalledTimes(4)
  })

  it('coalesces ticks and follows a commit that lands during refresh', async () => {
    const firstRefresh = deferred<void>()
    const secondRefresh = deferred<void>()
    getRevision
      .mockResolvedValueOnce(revision('a'))
      .mockResolvedValueOnce(revision('b'))
      .mockResolvedValueOnce(revision('b'))
      .mockResolvedValueOnce(revision('b'))
    let active = 0
    let maximumActive = 0
    const refreshSessions = vi.fn(async () => {
      active += 1
      maximumActive = Math.max(maximumActive, active)
      try {
        if (refreshSessions.mock.calls.length === 1) {
          await firstRefresh.promise
        } else {
          await secondRefresh.promise
        }
      } finally {
        active -= 1
      }
    })

    render(<Harness refreshSessions={refreshSessions} />)
    await flush()
    await tick()
    await tick()
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    firstRefresh.resolve(undefined)
    await flush()
    expect(refreshSessions).toHaveBeenCalledTimes(2)

    secondRefresh.resolve(undefined)
    await flush()

    expect(maximumActive).toBe(1)
    expect(getRevision).toHaveBeenCalledTimes(4)
  })

  it('does not overlap old and new profile generations', async () => {
    const oldRefresh = deferred<void>()
    getRevision.mockImplementation(async profile => revision(String(profile)))
    let active = 0
    let maximumActive = 0
    const refreshSessions = vi.fn(async () => {
      active += 1
      maximumActive = Math.max(maximumActive, active)
      try {
        if (refreshSessions.mock.calls.length === 1) {
          await oldRefresh.promise
        }
      } finally {
        active -= 1
      }
    })
    const view = render(
      <Harness profileScope="old" refreshSessions={refreshSessions} />
    )
    await flush()

    view.rerender(
      <Harness profileScope="new" refreshSessions={refreshSessions} />
    )
    await flush()
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    oldRefresh.resolve(undefined)
    await flush()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    expect(maximumActive).toBe(1)
    expect(getRevision).toHaveBeenCalledWith('new')
  })

  it('does not acknowledge an old profile generation', async () => {
    const oldConfirmation = deferred<ReturnType<typeof revision>>()
    getRevision
      .mockResolvedValueOnce(revision('shared'))
      .mockReturnValueOnce(oldConfirmation.promise)
      .mockResolvedValue(revision('shared'))
    const refreshSessions = vi.fn(async () => undefined)
    const view = render(
      <Harness profileScope="old" refreshSessions={refreshSessions} />
    )
    await flush()

    view.rerender(
      <Harness profileScope="new" refreshSessions={refreshSessions} />
    )
    await flush()
    oldConfirmation.resolve(revision('shared'))
    await flush()

    expect(refreshSessions).toHaveBeenCalledTimes(2)
  })

  it('does not start when disabled', async () => {
    const refreshSessions = vi.fn(async () => undefined)

    render(<Harness enabled={false} refreshSessions={refreshSessions} />)
    await tick()

    expect(getRevision).not.toHaveBeenCalled()
    expect(refreshSessions).not.toHaveBeenCalled()
    expect(resume).toBeUndefined()
  })

  it('maps the unified profile scope to the backend all-profile selector', async () => {
    getRevision.mockResolvedValue(revision('a'))

    render(
      <Harness
        profileScope="__all__"
        refreshSessions={async () => undefined}
      />
    )
    await flush()

    expect(getRevision).toHaveBeenCalledWith('all')
  })

  it('probes immediately on power resume without adding an interval', async () => {
    getRevision.mockResolvedValue(revision('a'))
    const setIntervalSpy = vi.spyOn(window, 'setInterval')

    render(<Harness refreshSessions={async () => undefined} />)
    await flush()
    const callsBeforeResume = getRevision.mock.calls.length
    expect(setIntervalSpy).toHaveBeenCalledTimes(1)

    act(() => resume?.())
    await flush()

    expect(getRevision.mock.calls.length).toBe(callsBeforeResume + 1)
    expect(setIntervalSpy).toHaveBeenCalledTimes(1)
  })
})
