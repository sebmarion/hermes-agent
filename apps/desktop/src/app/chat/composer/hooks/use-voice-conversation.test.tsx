import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useVoiceConversation } from './use-voice-conversation'

const notify = vi.hoisted(() => vi.fn())
const notifyError = vi.hoisted(() => vi.fn())
const playSpeechText = vi.hoisted(() => vi.fn(async (_text: string, _options: unknown) => true))
const stopVoicePlayback = vi.hoisted(() => vi.fn())

const mic = vi.hoisted(() => {
  type StartOptions = { onSilence?: () => void }
  const state: {
    cancel: ReturnType<typeof vi.fn>
    onSilence: (() => void) | null
    start: ReturnType<typeof vi.fn>
    stop: ReturnType<typeof vi.fn>
  } = {
    cancel: vi.fn(),
    onSilence: null,
    start: vi.fn(),
    stop: vi.fn()
  }

  state.start = vi.fn(async (options?: StartOptions) => {
    state.onSilence = options?.onSilence ?? null
  })
  state.stop = vi.fn(async () => ({
    audio: new Blob(['voice'], { type: 'audio/webm' }),
    heardSpeech: true
  }))

  return state
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: 'configure speech to text',
          couldNotStartSession: 'could not start session',
          microphoneFailed: 'microphone failed',
          playbackFailed: 'playback failed',
          transcriptionFailed: 'transcription failed',
          unavailable: 'unavailable'
        }
      }
    }
  })
}))

vi.mock('@/lib/voice-playback', () => ({
  playSpeechText: (text: string, options: unknown) => playSpeechText(text, options),
  stopVoicePlayback: () => stopVoicePlayback()
}))

vi.mock('@/store/notifications', () => ({
  notify: (...args: unknown[]) => notify(...args),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: {
      cancel: mic.cancel,
      start: mic.start,
      stop: mic.stop
    },
    level: 0
  })
}))

type ConversationApi = ReturnType<typeof useVoiceConversation>

function Harness({
  enabled,
  onReady,
  onSubmit
}: {
  enabled: boolean
  onReady: (api: ConversationApi) => void
  onSubmit: (text: string) => Promise<boolean | void> | boolean | void
}) {
  const api = useVoiceConversation({
    busy: false,
    consumePendingResponse: vi.fn(),
    enabled,
    onSubmit,
    onTranscribeAudio: vi.fn(async () => 'Can you hear me'),
    pendingResponse: () => ({
      id: 'stale-assistant-message',
      pending: false,
      text: 'This is the stale unrelated assistant response.'
    })
  })

  useEffect(() => onReady(api), [api, onReady])

  return <div data-testid="voice-conversation-status">{api.status}</div>
}

describe('useVoiceConversation', () => {
  beforeEach(() => {
    mic.cancel.mockClear()
    mic.start.mockClear()
    mic.stop.mockClear()
    mic.onSilence = null
    notify.mockClear()
    notifyError.mockClear()
    playSpeechText.mockClear()
    stopVoicePlayback.mockClear()
  })

  afterEach(() => cleanup())

  it('does not speak a stale assistant response when voice submit is rejected', async () => {
    const onSubmit = vi.fn(async () => false)
    let api: ConversationApi | null = null
    const { rerender } = render(
      <Harness
        enabled={false}
        onReady={next => {
          api = next
        }}
        onSubmit={onSubmit}
      />
    )

    rerender(
      <Harness
        enabled
        onReady={next => {
          api = next
        }}
        onSubmit={onSubmit}
      />
    )

    await waitFor(() => expect(mic.start).toHaveBeenCalledTimes(1))
    expect(api).not.toBeNull()
    expect(mic.onSilence).toBeTypeOf('function')

    await act(async () => {
      mic.onSilence?.()
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('Can you hear me'))
    await waitFor(() => expect(mic.start).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('voice-conversation-status').textContent).toBe('listening')
    expect(playSpeechText).not.toHaveBeenCalled()
  })
})
