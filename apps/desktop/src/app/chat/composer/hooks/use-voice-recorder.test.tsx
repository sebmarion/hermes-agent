import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useVoiceRecorder } from './use-voice-recorder'

const notify = vi.hoisted(() => vi.fn())
const notifyError = vi.hoisted(() => vi.fn())

const mic = vi.hoisted(() => {
  type RecorderError = (error: Error) => void
  const state: {
    cancel: ReturnType<typeof vi.fn>
    onError: RecorderError | null
    recording: boolean
    start: ReturnType<typeof vi.fn>
    stop: ReturnType<typeof vi.fn>
  } = {
    cancel: vi.fn(),
    onError: null,
    recording: false,
    start: vi.fn(),
    stop: vi.fn()
  }

  state.start = vi.fn(async (options?: { onError?: RecorderError }) => {
    state.onError = options?.onError ?? null
    state.recording = true
  })

  state.stop = vi.fn(async () => {
    state.recording = false

    return null
  })

  return state
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          microphoneAccessDenied: 'microphone access denied',
          microphoneConstraintsUnsupported: 'microphone constraints unsupported',
          microphoneInUse: 'microphone in use',
          microphonePermissionDenied: 'microphone permission denied',
          microphoneStartFailed: 'microphone start failed',
          microphoneUnsupported: 'microphone unsupported',
          noMicrophone: 'no microphone',
          noSpeechDetected: 'no speech detected',
          recordingFailed: 'recording failed',
          transcriptionFailed: 'transcription failed',
          transcriptionUnavailable: 'transcription unavailable',
          tryRecordingAgain: 'try recording again',
          unavailable: 'unavailable'
        }
      }
    }
  })
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
    level: 0,
    recording: mic.recording
  })
}))

type RecorderApi = ReturnType<typeof useVoiceRecorder>

function Harness({
  focusInput,
  onReady,
  onTranscribeAudio
}: {
  focusInput: () => void
  onReady: (api: RecorderApi) => void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
}) {
  const api = useVoiceRecorder({
    focusInput,
    maxRecordingSeconds: 60,
    onTranscript: vi.fn(),
    onTranscribeAudio
  })

  useEffect(() => onReady(api), [api, onReady])

  return <div data-testid="voice-status">{api.voiceStatus}</div>
}

describe('useVoiceRecorder', () => {
  beforeEach(() => {
    mic.cancel.mockClear()
    mic.start.mockClear()
    mic.stop.mockClear()
    mic.onError = null
    mic.recording = false
    notify.mockClear()
    notifyError.mockClear()
  })

  afterEach(() => cleanup())

  it('recovers the dictation state when MediaRecorder errors after start', async () => {
    const focusInput = vi.fn()
    let api: RecorderApi | null = null

    render(
      <Harness
        focusInput={focusInput}
        onReady={next => {
          api = next
        }}
        onTranscribeAudio={vi.fn(async () => 'transcript')}
      />
    )

    await waitFor(() => expect(api).not.toBeNull())

    act(() => api!.dictate())

    await waitFor(() => expect(screen.getByTestId('voice-status').textContent).toBe('recording'))
    expect(mic.onError).toBeTypeOf('function')

    const error = new Error('recorder device lost')
    act(() => mic.onError?.(error))

    await waitFor(() => expect(screen.getByTestId('voice-status').textContent).toBe('idle'))
    expect(focusInput).toHaveBeenCalledTimes(1)
    expect(notifyError).toHaveBeenCalledWith(error, 'recording failed')
  })

  it('uses voiceStatus as a fallback stop signal when recorder state desynchronizes', async () => {
    const focusInput = vi.fn()
    let api: RecorderApi | null = null
    const { rerender } = render(
      <Harness
        focusInput={focusInput}
        onReady={next => {
          api = next
        }}
        onTranscribeAudio={vi.fn(async () => 'transcript')}
      />
    )

    await waitFor(() => expect(api).not.toBeNull())
    act(() => api!.dictate())
    await waitFor(() => expect(screen.getByTestId('voice-status').textContent).toBe('recording'))

    mic.recording = false
    rerender(
      <Harness
        focusInput={focusInput}
        onReady={next => {
          api = next
        }}
        onTranscribeAudio={vi.fn(async () => 'transcript')}
      />
    )

    act(() => api!.dictate())

    await waitFor(() => expect(mic.stop).toHaveBeenCalledTimes(1))
  })
})
