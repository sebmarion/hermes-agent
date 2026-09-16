import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import {
  hydrateLiveSessionInflight,
  liveSessionInflightMessages,
  replayPendingTerminalOutbox,
  replayTerminalOutbox,
  scheduleResumeScrollToBottom,
  signalFreshSessionBoundary,
  writeActiveSessionFile
} from '../app/useSessionLifecycle.js'
import { toTranscriptMessages, transcriptDeliveryIds } from '../domain/messages.js'

describe('transcript delivery identity', () => {
  it('hydrates a persisted async completion delivery id onto its assistant response', () => {
    const transcript = toTranscriptMessages([
      {
        role: 'user',
        text: 'background agent work finished',
        display_kind: 'async_delegation_complete',
        display_metadata: { delivery_id: 'async-delegation:d1' }
      },
      { role: 'assistant', text: 'Recovered answer' }
    ])

    expect(transcript).toEqual([
      { kind: 'event', role: 'system', text: 'background agent work finished' },
      { role: 'assistant', text: 'Recovered answer', deliveryId: 'async-delegation:d1' }
    ])
    expect(transcriptDeliveryIds(transcript)).toEqual(['async-delegation:d1'])
  })
})

describe('fresh session boundary', () => {
  it('signals only when a live session is replaced by a different session', () => {
    const onFreshSessionStarted = vi.fn()

    expect(signalFreshSessionBoundary('old-session', 'new-session', onFreshSessionStarted)).toBe(true)
    expect(signalFreshSessionBoundary(null, 'first-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('same-session', 'same-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', null, onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', 'new-session')).toBe(false)
    expect(onFreshSessionStarted).toHaveBeenCalledOnce()
    expect(onFreshSessionStarted).toHaveBeenCalledWith('new-session')
  })
})

describe('durable terminal outbox replay', () => {
  it('renders each durable delivery once and acknowledges it after append', async () => {
    const appended: unknown[] = []
    const acknowledged: string[] = []
    const seen = new Set<string>()

    await replayTerminalOutbox(
      [
        { payload: { delivery_id: 'delivery-1', text: 'Recovered answer' } },
        { payload: { delivery_id: 'delivery-1', text: 'Recovered answer' } }
      ],
      seen,
      msg => appended.push(msg),
      deliveryId => acknowledged.push(deliveryId)
    )

    expect(appended).toEqual([{ role: 'assistant', text: 'Recovered answer', deliveryId: 'delivery-1' }])
    expect(acknowledged).toEqual(['delivery-1'])
  })

  it('renders distinct deliveries even when their text is identical', async () => {
    const appended: unknown[] = []
    const seen = new Set<string>()

    await replayTerminalOutbox(
      [
        { payload: { delivery_id: 'same-text-1', text: 'Repeat' } },
        { payload: { delivery_id: 'same-text-2', text: 'Repeat' } }
      ],
      seen,
      msg => appended.push(msg),
      async () => undefined
    )

    expect(appended).toEqual([
      { role: 'assistant', text: 'Repeat', deliveryId: 'same-text-1' },
      { role: 'assistant', text: 'Repeat', deliveryId: 'same-text-2' }
    ])
  })

  it('deduplicates concurrent replays while acknowledgement is pending', async () => {
    const appended: unknown[] = []
    const seen = new Set<string>()
    let releaseAck!: () => void

    const ackPending = new Promise<void>(resolve => {
      releaseAck = resolve
    })

    const acknowledge = vi.fn(() => ackPending)
    const deliveries = [{ payload: { delivery_id: 'delivery-concurrent', text: 'Once' } }]

    const first = replayTerminalOutbox(deliveries, seen, msg => appended.push(msg), acknowledge)
    const second = replayTerminalOutbox(deliveries, seen, msg => appended.push(msg), acknowledge)
    await Promise.resolve()
    expect(appended).toEqual([{ role: 'assistant', text: 'Once', deliveryId: 'delivery-concurrent' }])
    expect(acknowledge).toHaveBeenCalledOnce()

    releaseAck()
    await Promise.all([first, second])
  })

  it('fetches, appends, and acknowledges pending deliveries during activation replay', async () => {
    const appended: unknown[] = []
    const seen = new Set<string>()

    const gw = {
      request: vi.fn(async (method: string) => {
        if (method === 'terminal.outbox.pending') {
          return { deliveries: [{ payload: { delivery_id: 'activation-1', text: 'Recovered on activation' } }] }
        }

        return { acknowledged: true }
      })
    }

    await replayPendingTerminalOutbox(gw, 'runtime-activation', seen, msg => appended.push(msg))

    expect(appended).toEqual([{ role: 'assistant', text: 'Recovered on activation', deliveryId: 'activation-1' }])
    expect(gw.request).toHaveBeenNthCalledWith(1, 'terminal.outbox.pending', { session_id: 'runtime-activation' })
    expect(gw.request).toHaveBeenNthCalledWith(2, 'terminal.outbox.ack', {
      delivery_id: 'activation-1',
      session_id: 'runtime-activation'
    })
  })
  it('keeps an appended delivery reserved when acknowledgement fails', async () => {
    const seen = new Set<string>()
    const acknowledge = vi.fn().mockRejectedValueOnce(new Error('disconnected'))

    await expect(
      replayTerminalOutbox(
        [{ payload: { delivery_id: 'delivery-retry', text: 'Retry me' } }],
        seen,
        () => undefined,
        acknowledge
      )
    ).rejects.toThrow('disconnected')

    expect(seen.has('delivery-retry')).toBe(true)
  })
})

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })
})

describe('live session activation in-flight state', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ streaming: true })
  })

  it('keeps the in-flight user prompt in history and hydrates partial assistant text', () => {
    const inflight = { assistant: 'partial answer', streaming: true, user: 'write a long answer' }

    expect(liveSessionInflightMessages(inflight)).toEqual([{ role: 'user', text: 'write a long answer' }])

    hydrateLiveSessionInflight(inflight)

    expect(turnController.bufRef).toBe('partial answer')
    expect(getTurnState().streaming).toBe('partial answer')
  })

  it('ignores empty in-flight payloads', () => {
    expect(liveSessionInflightMessages({ assistant: '', streaming: false, user: '   ' })).toEqual([])

    hydrateLiveSessionInflight({ assistant: '', streaming: false, user: '' })

    expect(turnController.bufRef).toBe('')
    expect(getTurnState().streaming).toBe('')
  })
})

describe('resume scroll settle', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('re-snaps while sticky and stops when the user scrolls away', () => {
    vi.useFakeTimers()
    let sticky = true
    let lastManualScrollAt = 0
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => lastManualScrollAt,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80, 240]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    sticky = false
    lastManualScrollAt = Date.now() + 1
    vi.advanceTimersByTime(160)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    cancel()
  })

  it('cancels pending resume snaps', () => {
    vi.useFakeTimers()
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => true,
          scrollToBottom
        }
      } as any,
      [20]
    )

    cancel()
    vi.advanceTimersByTime(20)

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the immediate resume snap even before sticky state settles', () => {
    vi.useFakeTimers()
    let sticky = false
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    sticky = true
    cancel()
  })
})
