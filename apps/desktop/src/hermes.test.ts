import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getAllProfileSessionsRevision,
  AUDIO_SPEAK_MAX_REQUEST_TIMEOUT_MS,
  AUDIO_SPEAK_MIN_REQUEST_TIMEOUT_MS,
  AUDIO_TRANSCRIBE_MAX_REQUEST_TIMEOUT_MS,
  AUDIO_TRANSCRIBE_MIN_REQUEST_TIMEOUT_MS,
  audioSpeakRequestTimeoutMs,
  audioTranscribeRequestTimeoutMs,
  getAllSessionMessages,
  getCronJobs,
  getGlobalModelInfo,
  getGlobalModelOptions,
  getHermesConfig,
  getHermesConfigDefaults,
  getProfiles,
  getSessionMessages,
  getStatus,
  listAllProfileSessions,
  listSessions,
  listSidebarSessions,
  resetSidebarBatchCapability,
  setApiRequestProfile,
  speakText,
  transcribeAudio,
  triggerCronJob
} from './hermes'
import { refreshActiveProfile } from './store/profile'

const emptySessionsResponse = {
  limit: 0,
  offset: 0,
  sessions: [],
  total: 0
}

describe('Hermes REST session helpers', () => {
  let api: ReturnType<typeof vi.fn>

  beforeEach(() => {
    api = vi.fn().mockResolvedValue(emptySessionsResponse)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('uses a longer timeout for the single-profile session list', async () => {
    await listSessions(50, 1)

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/sessions?limit=50&offset=0&min_messages=1&archived=exclude&order=recent',
        timeoutMs: 60_000
      })
    )
  })

  it('uses a longer timeout for the all-profile session list', async () => {
    await listAllProfileSessions(50, 1)

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/profiles/sessions?limit=50&offset=0&min_messages=1&archived=exclude&order=recent&profile=all',
        timeoutMs: 60_000
      })
    )
  })

  it('uses the lightweight timeout for the all-profile revision probe', async () => {
    api.mockResolvedValue({ profiles: ['worker one'], revision: 'revision-1' })

    await getAllProfileSessionsRevision('worker one')

    expect(api).toHaveBeenCalledWith({
      path: '/api/profiles/sessions/revision?profile=worker%20one',
      timeoutMs: 5_000
    })
  })

  it('uses a longer timeout for profile listing during desktop startup', async () => {
    api.mockResolvedValue({ profiles: [] })

    await getProfiles()

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/profiles',
        timeoutMs: 60_000
      })
    )
  })

  it('uses a longer timeout for active profile refresh during desktop startup', async () => {
    api.mockResolvedValueOnce({ current: 'default' }).mockResolvedValueOnce({ profiles: [] })

    await refreshActiveProfile()

    expect(api).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        path: '/api/profiles/active',
        timeoutMs: 60_000
      })
    )
    expect(api).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        path: '/api/profiles',
        timeoutMs: 60_000
      })
    )
  })

  it('gives the whole startup data burst the long timeout, not just profiles', async () => {
    api.mockResolvedValue({})

    const bootCalls: [() => Promise<unknown>, string][] = [
      [getHermesConfig, '/api/config'],
      [getHermesConfigDefaults, '/api/config/defaults'],
      [getGlobalModelInfo, '/api/model/info'],
      [() => getGlobalModelOptions(), '/api/model/options?explicit_only=1'],
      [getCronJobs, '/api/cron/jobs']
    ]

    for (const [call, path] of bootCalls) {
      api.mockClear()
      await call()
      expect(api).toHaveBeenCalledWith(expect.objectContaining({ path, timeoutMs: 60_000 }))
    }
  })

  it('waits for synchronous cron triggers as a long-running operation', async () => {
    api.mockResolvedValue({ id: 'job-1' })

    await triggerCronJob('job-1')

    const request = api.mock.calls[0]?.[0]
    expect(request).toEqual(
      expect.objectContaining({
        path: '/api/cron/jobs/job-1/trigger',
        method: 'POST'
      })
    )
    expect(request.timeoutMs).toBeGreaterThanOrEqual(60 * 60 * 1000)
  })

  it('keeps the liveness poll on the short default so a dead backend fails fast', async () => {
    api.mockResolvedValue({})
    api.mockClear()

    await getStatus()

    // /api/status must NOT carry the long startup timeout — it is the runtime
    // liveness probe and has to fail quickly when the backend drops.
    const call = api.mock.calls[0]?.[0] as { path: string; timeoutMs?: number }
    expect(call.path).toBe('/api/status')
    expect(call.timeoutMs).toBeUndefined()
  })

  it('tags cross-profile message reads for Electron routing and backend lookup', async () => {
    api.mockResolvedValue({ messages: [], session_id: 'session-1' })

    await getSessionMessages('session-1', 'xiaoxuxu')

    expect(api).toHaveBeenCalledWith({
      path: '/api/sessions/session-1/messages?profile=xiaoxuxu',
      profile: 'xiaoxuxu'
    })
  })

  it('passes bounded transcript pagination through to the backend', async () => {
    api.mockResolvedValue({ messages: [], session_id: 'session-1' })

    await getSessionMessages('session-1', 'xiaoxuxu', {
      limit: 500,
      offset: 1000,
      order: 'latest'
    })

    expect(api).toHaveBeenCalledWith({
      path: '/api/sessions/session-1/messages?profile=xiaoxuxu&limit=500&offset=1000&order=latest',
      profile: 'xiaoxuxu'
    })
  })

  it('loads complete transcripts through bounded oldest-first pages', async () => {
    api
      .mockResolvedValueOnce({
        messages: [{ id: 1 }, { id: 2 }],
        session_id: 'session-1',
        pagination: { limit: 2, offset: 0, order: 'oldest', returned: 2 }
      })
      .mockResolvedValueOnce({
        messages: [{ id: 3 }],
        session_id: 'session-1',
        pagination: { limit: 2, offset: 2, order: 'oldest', returned: 1 }
      })

    const result = await getAllSessionMessages('session-1', 'xiaoxuxu')

    expect(result.messages).toEqual([{ id: 1 }, { id: 2 }, { id: 3 }])
    expect(api).toHaveBeenNthCalledWith(1, {
      path: '/api/sessions/session-1/messages?profile=xiaoxuxu&limit=500&offset=0&order=oldest&include_compacted=true',
      profile: 'xiaoxuxu'
    })
    expect(api).toHaveBeenNthCalledWith(2, {
      path: '/api/sessions/session-1/messages?profile=xiaoxuxu&limit=500&offset=2&order=oldest&include_compacted=true',
      profile: 'xiaoxuxu'
    })
  })

  it('stops complete transcript loads before Desktop memory becomes unbounded', async () => {
    api.mockResolvedValueOnce({
      messages: [{ id: 1, content: 'large transcript page' }],
      session_id: 'session-1',
      pagination: { limit: 1, offset: 0, order: 'oldest', returned: 1 }
    })

    await expect(getAllSessionMessages('session-1', null, { maxJsonChars: 1 })).rejects.toThrow(
      'Desktop safe-load limit'
    )
  })

  it('bounds blocking TTS synthesis timeouts by text length', () => {
    expect(audioSpeakRequestTimeoutMs('short message')).toBe(AUDIO_SPEAK_MIN_REQUEST_TIMEOUT_MS)
    expect(audioSpeakRequestTimeoutMs('x'.repeat(8_000))).toBe(280_000)
    expect(audioSpeakRequestTimeoutMs('x'.repeat(100_000))).toBe(AUDIO_SPEAK_MAX_REQUEST_TIMEOUT_MS)
  })

  it('routes blocking TTS synthesis through the active profile backend', async () => {
    setApiRequestProfile('rhaegal')
    api.mockResolvedValueOnce({
      data_url: 'data:audio/mpeg;base64,AA==',
      mime_type: 'audio/mpeg',
      ok: true,
      provider: 'openai'
    })

    await expect(speakText('Read this aloud')).resolves.toEqual({
      data_url: 'data:audio/mpeg;base64,AA==',
      mime_type: 'audio/mpeg',
      ok: true,
      provider: 'openai'
    })

    expect(api).toHaveBeenCalledWith({
      body: { text: 'Read this aloud' },
      method: 'POST',
      path: '/api/audio/speak',
      profile: 'rhaegal',
      timeoutMs: AUDIO_SPEAK_MIN_REQUEST_TIMEOUT_MS
    })
  })

  it('bounds blocking transcription timeouts by payload length', () => {
    expect(audioTranscribeRequestTimeoutMs('data:audio/webm;base64,AA==')).toBe(AUDIO_TRANSCRIBE_MIN_REQUEST_TIMEOUT_MS)
    expect(audioTranscribeRequestTimeoutMs('x'.repeat(3_000_000))).toBe(300_000)
    expect(audioTranscribeRequestTimeoutMs('x'.repeat(9_000_000))).toBe(AUDIO_TRANSCRIBE_MAX_REQUEST_TIMEOUT_MS)
  })

  it('uses an extended timeout for blocking transcription', async () => {
    api.mockResolvedValueOnce({
      ok: true,
      provider: 'openai',
      text: 'transcribed text'
    })

    await expect(transcribeAudio('data:audio/webm;base64,AA==', 'audio/webm')).resolves.toEqual({
      ok: true,
      provider: 'openai',
      text: 'transcribed text'
    })

    expect(api).toHaveBeenCalledWith({
      body: { data_url: 'data:audio/webm;base64,AA==', mime_type: 'audio/webm' },
      method: 'POST',
      path: '/api/audio/transcribe',
      timeoutMs: AUDIO_TRANSCRIBE_MIN_REQUEST_TIMEOUT_MS
    })
  })

  it('defaults model options to configured providers only', async () => {
    await getGlobalModelOptions()

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/model/options?explicit_only=1'
      })
    )
  })

  it('can opt into unconfigured providers for onboarding flows', async () => {
    await getGlobalModelOptions({ includeUnconfigured: true, refresh: true, explicitOnly: false })

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/model/options?refresh=1&include_unconfigured=1'
      })
    )
  })
})
