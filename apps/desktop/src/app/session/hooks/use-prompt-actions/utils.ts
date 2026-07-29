import type { AppendMessage } from '@assistant-ui/react'
import { atom } from 'nanostores'

import { translateNow, type Translations } from '@/i18n'
import type { ChatMessage } from '@/lib/chat-messages'
import { type CommandsCatalogLike, filterDesktopCommandsCatalog } from '@/lib/desktop-slash-commands'
import { isProviderSetupErrorMessage } from '@/lib/provider-setup-errors'
import type { ComposerAttachment } from '@/store/composer'

export type GatewayRequest = <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

export function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export function isSessionIdCandidate(value: string): boolean {
  const trimmed = value.trim()

  return /^\d{8}_\d{6}_[A-Fa-f0-9]{6}$/.test(trimmed) || /^[A-Fa-f0-9]{32}$/.test(trimmed)
}

export function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()

    reader.addEventListener('load', () => {
      if (typeof reader.result === 'string') {
        resolve(reader.result)
      } else {
        reject(new Error(translateNow('desktop.audioReadFailed')))
      }
    })
    reader.addEventListener('error', () => reject(reader.error || new Error(translateNow('desktop.audioReadFailed'))))
    reader.readAsDataURL(blob)
  })
}

export function isProviderSetupError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error)

  return isProviderSetupErrorMessage(message)
}

export function inlineErrorMessage(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : fallback

  return (raw.match(/Error invoking remote method '[^']+': Error: (.+)$/)?.[1] ?? raw).replace(/^Error:\s*/, '').trim()
}

export function isSessionNotFoundError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /session not found/i.test(message)
}

/**
 * Is the session a prompt is about to run against currently mid-turn?
 *
 * The foreground `busyRef` is NOT the answer. It mirrors whatever chat is on
 * screen, while submit and slash both resolve their target through
 * `resolveTargetSessionId` — routinely a different session (a tile, a route
 * rebind, a session created by this very call). Reading the foreground flag
 * therefore gates one session's send on another session's turn: a stale
 * foreground `true` (a warm resume of a still-running chat leaves one behind)
 * blocks an IDLE target and reports "session busy" about a session doing
 * nothing, and the converse lets a background send fire mid-turn.
 *
 * The published per-session state is authoritative. Fall back to the
 * foreground flag only when the target has no state yet — a just-minted
 * session whose first publish hasn't landed.
 */
export function isTargetSessionBusy(
  sessionStates: Record<string, { busy: boolean }>,
  sessionId: null | string,
  foregroundBusy: boolean
): boolean {
  const state = sessionId ? sessionStates[sessionId] : undefined

  return state ? state.busy : foregroundBusy
}

// Gateway JSON-RPC calls reject with "request timed out: <method>" when the
// backend event loop is starved (e.g. a poller spin or a heavy async-injected
// turn). For prompt.submit this is indistinguishable from a dead runtime
// session on the client side — recovery must treat it like one (#55578):
// resume the SELECTED stored session and retry, instead of surfacing an error
// that leads to a null activeSessionId and a silently minted new session.
export function isGatewayTimeoutError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /request timed out/i.test(message)
}

// The gateway refuses prompt.submit while a turn is running (4009 "session
// busy"). It's a transient concurrency guard, never a user-facing error: a
// submit racing the settle edge (or a rewind interrupting mid-turn) just waits
// a beat for the turn to wind down, then lands. Bounded so a genuinely stuck
// turn still surfaces eventually.
export const SESSION_BUSY_RETRY_TIMEOUT_MS = 6_000
export const SESSION_BUSY_RETRY_INTERVAL_MS = 150

export function isSessionBusyError(error: unknown): boolean {
  return /session busy/i.test(error instanceof Error ? error.message : String(error))
}

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

// Retry a gateway call across transient "session busy" so it never reaches the
// user — the turn settles within the deadline and the call lands.
export async function withSessionBusyRetry<T>(call: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + SESSION_BUSY_RETRY_TIMEOUT_MS

  for (;;) {
    try {
      return await call()
    } catch (err) {
      if (isSessionBusyError(err) && Date.now() < deadline) {
        await sleep(SESSION_BUSY_RETRY_INTERVAL_MS)

        continue
      }

      throw err
    }
  }
}

// Hard guard: at most one prompt.submit in flight per session. Every submit
// path — user Enter, queue drain, busy-retry, slash fallthrough — funnels
// through submitPromptText. Without this, a stalled turn (e.g. a context-bloated
// session whose first call hangs) let the SAME prompt launch several real turns
// at once (the "message stacked 5×" bug). Keyed by stored/active session id.
//
// TTL: prompt.submit is fire-and-forget from the gateway's perspective — the
// RPC ack timeout is 30 min (PROMPT_SUBMIT_REQUEST_TIMEOUT_MS). If the ack is
// delayed that long, a stale lock would silently block every resubmit for the
// entire wait (the "text stays in input, can't resend" bug). The TTL bounds
// this: a lock older than the ceiling is treated as stale and evicted, so the
// user's retry lands instead of silently false-returning. The ceiling is well
// above any legitimate ack latency (seconds) and well below the 30-min RPC
// timeout, so a genuinely-stuck RPC still surfaces eventually.
const SUBMIT_LOCK_TTL_MS = 30_000
const _submitInFlightMap = new Map<string, number>()

// Reactive mirror so the UI (send button, composer) can show a "still sending"
// state while a submit lock is held — without this, a slow gateway ack leaves
// the user staring at an idle composer with no feedback that their message is
// still in flight (the "text stays in input, can't resend" confusion).
// Keyed by session lock key (storedSessionId || runtimeId || '__pending_new__').
export const $submitInFlight = atom<ReadonlySet<string>>(new Set())

function notifySubmitInFlight() {
  $submitInFlight.set(new Set(_submitInFlightMap.keys()))
}

/** Check whether a submit lock is held for the given key, evicting stale entries. */
export function _submitInFlightHas(key: string): boolean {
  const acquiredAt = _submitInFlightMap.get(key)

  if (acquiredAt === undefined) {
    return false
  }

  if (Date.now() - acquiredAt > SUBMIT_LOCK_TTL_MS) {
    // Stale lock — the gateway ack never landed within the TTL window.
    // Evict so the next submit attempt isn't silently blocked.
    _submitInFlightMap.delete(key)
    notifySubmitInFlight()

    return false
  }

  return true
}

/** Acquire a submit lock for the given key. Caller MUST release via
 *  `_submitInFlightDelete` on success, failure, or abort. */
export function _submitInFlightAdd(key: string): void {
  _submitInFlightMap.set(key, Date.now())
  notifySubmitInFlight()
}

/** Release a submit lock. Safe to call multiple times (idempotent). */
export function _submitInFlightDelete(key: string): void {
  if (_submitInFlightMap.delete(key)) {
    notifySubmitInFlight()
  }
}

export function base64FromDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(',')

  return comma >= 0 ? dataUrl.slice(comma + 1) : ''
}

export function imageFilenameFromPath(filePath: string): string {
  return filePath.split(/[\\/]/).filter(Boolean).pop() || 'image.png'
}

// Remote gateway: the local composer-image file lives on THIS machine's disk,
// not the gateway's, so read the bytes here and upload them via
// image.attach_bytes. Returns null when the file can't be read.
export async function readImageForRemoteAttach(
  filePath: string
): Promise<{ contentBase64: string; filename: string } | null> {
  const dataUrl = await window.hermesDesktop?.readFileDataUrl(filePath)
  const contentBase64 = dataUrl ? base64FromDataUrl(dataUrl) : ''

  return contentBase64 ? { contentBase64, filename: imageFilenameFromPath(filePath) } : null
}

// Read a non-image file as a data URL for upload via file.attach. Returns null
// when the desktop bridge can't read the file (e.g. it was moved/deleted).
// Prefer the attach-specific IPC (256 MiB) so remote uploads are not stuck on
// the preview/Settings default; fall back for older Electron shells.
export async function readFileDataUrlForAttach(filePath: string): Promise<string | null> {
  const reader = window.hermesDesktop?.readFileDataUrlForAttach ?? window.hermesDesktop?.readFileDataUrl

  if (!reader) {
    return null
  }

  const dataUrl = await reader(filePath)

  return dataUrl || null
}

// The attach/preview IPC base64-loads the whole file into memory and rejects
// with a raw "file is too large (N bytes; limit M bytes)" string when over
// cap. In remote mode every attachment's bytes go through that read, so a big
// file surfaces that internal message verbatim in the failure toast. Translate
// it into a friendly "too large to upload to the remote gateway" line, parsing
// the limit out of the message so it tracks the real cap. Non-cap errors pass
// through unchanged.
export function friendlyRemoteAttachError(err: unknown, label: string): Error {
  const message = err instanceof Error ? err.message : String(err)

  if (!/too large/i.test(message)) {
    return err instanceof Error ? err : new Error(message)
  }

  const limitBytes = Number(message.match(/limit (\d+) bytes/)?.[1])
  const cap = Number.isFinite(limitBytes) && limitBytes > 0 ? ` (max ${Math.floor(limitBytes / (1024 * 1024))} MB)` : ''

  return new Error(`${label} is too large to upload to the remote gateway${cap}.`)
}

export function renderCommandsCatalog(catalog: CommandsCatalogLike, copy: Translations['desktop']): string {
  const desktopCatalog = filterDesktopCommandsCatalog(catalog)

  const sections = desktopCatalog.categories?.length
    ? desktopCatalog.categories
    : [{ name: copy.desktopCommands, pairs: desktopCatalog.pairs ?? [] }]

  const body = sections
    .filter(section => section.pairs.length > 0)
    .map(section => {
      const rows = section.pairs.map(([cmd, desc]) => `${cmd.padEnd(18)} ${desc}`)

      return [`${section.name}:`, ...rows].join('\n')
    })
    .join('\n\n')

  const tail = [
    desktopCatalog.skill_count ? copy.skillCommandsAvailable(desktopCatalog.skill_count) : '',
    desktopCatalog.warning ? copy.warningLine(desktopCatalog.warning) : ''
  ]
    .filter(Boolean)
    .join('\n')

  return [body || 'No desktop commands available.', tail].filter(Boolean).join('\n\n')
}

export function slashStatusText(command: string, output: string): string {
  return [`slash:${command}`, output.trim()].filter(Boolean).join('\n')
}

/**
 * Format the JSON reply from a gateway RPC surfaced as `kind: 'rpc'` in
 * desktop-slash-commands.ts. Kept here (instead of slash.ts) so it has no
 * React / store dependencies and can be unit-tested in isolation.
 *
 * The renderer follows the field conventions shared by the gateway handlers
 * we route to today:
 * - `session.compress`: { summary: { headline, token_line, note, noop } }
 *   (only used if /compress ever moves to `rpc`; it's an `action` today
 *   because it needs transcript replacement)
 * - `session.status`:   { output: "<multi-line plain text>" }
 * - `session.save`:     { file: "<absolute path>" }
 * - `session.usage`:    { calls, input, output, total, credits_lines? }
 * - `session.steer`:    { status: 'queued' | 'rejected', text }
 * - `process.stop`:     { killed: boolean }
 * - `agents.list`:      { processes: [{ session_id, command, status, uptime }] }
 *
 * Any RPC whose response doesn't match these shapes falls through to a
 * JSON.stringify dump so we never silently swallow data.
 */
export function renderRpcResult(response: unknown, name: string): string {
  if (!response || typeof response !== 'object') {
    return ''
  }

  const r = response as Record<string, unknown>

  const summary = r.summary as { headline?: string; token_line?: string; note?: string; noop?: boolean } | undefined

  if (summary && typeof summary === 'object' && typeof summary.headline === 'string' && summary.headline) {
    const lines: string[] = [`${summary.noop ? '' : '✓ '}${summary.headline}`]

    if (summary.token_line) {
      lines.push(`  ${summary.token_line}`)
    }

    if (summary.note) {
      lines.push(`  ${summary.note}`)
    }

    return lines.join('\n')
  }

  // session.steer — { status: 'queued' | 'rejected', text }
  if (r.status === 'queued' || r.status === 'rejected') {
    const text = typeof r.text === 'string' ? r.text : ''

    if (r.status === 'queued') {
      return text ? `Steered · "${text}" queued for next tool call` : 'Steered next tool call'
    }

    return 'Steer rejected — agent declined input'
  }

  // process.stop — { killed: number }
  if ('killed' in r && typeof r.killed === 'number') {
    return r.killed > 0
      ? `Stopped ${r.killed} background process${r.killed === 1 ? '' : 'es'}.`
      : 'No background processes to stop.'
  }

  // session.save — { file }
  if (typeof r.file === 'string' && r.file) {
    return `Saved transcript to ${r.file}`
  }

  // session.status — { output }
  if (typeof r.output === 'string' && r.output) {
    return r.output
  }

  // session.usage — { calls, input, output, total, credits_lines? }
  if ('total' in r || 'input' in r || 'output' in r || 'calls' in r) {
    const calls = Number(r.calls ?? 0)
    const input = Number(r.input ?? 0)
    const output = Number(r.output ?? 0)
    const total = Number(r.total ?? 0)

    const lines: string[] = [
      `Usage: ${calls.toLocaleString()} calls · ${input.toLocaleString()} in / ${output.toLocaleString()} out · ${total.toLocaleString()} total`
    ]

    if (Array.isArray(r.credits_lines)) {
      for (const credit of r.credits_lines) {
        if (typeof credit === 'string' && credit.trim()) {
          lines.push(credit.trim())
        }
      }
    }

    return lines.join('\n')
  }

  // agents.list — { processes: [{ session_id, command, status, uptime }] }
  if (Array.isArray(r.processes)) {
    if (r.processes.length === 0) {
      return 'No background tasks running.'
    }

    return r.processes
      .map(p => {
        if (!p || typeof p !== 'object') {
          return ''
        }

        const proc = p as Record<string, unknown>
        const status = typeof proc.status === 'string' ? proc.status : 'unknown'
        const command = typeof proc.command === 'string' ? proc.command : ''
        const sessionId = typeof proc.session_id === 'string' ? proc.session_id : ''
        const uptime = proc.uptime

        const meta: string[] = []

        if (typeof uptime === 'number' && uptime >= 0) {
          meta.push(`${uptime}s`)
        }

        if (sessionId) {
          meta.push(sessionId)
        }

        const tail = meta.length ? ` (${meta.join(' · ')})` : ''

        return `• [${status}] ${command}${tail}`
      })
      .filter(Boolean)
      .join('\n')
  }

  // Generic fallback — keeps us honest if the gateway adds new fields we
  // haven't shaped yet.
  return `/${name}: ${JSON.stringify(r)}`
}

export function appendText(message: AppendMessage): string {
  return message.content
    .map(part => ('text' in part ? part.text : ''))
    .join('')
    .trim()
}

export function visibleUserOrdinal(messages: readonly ChatMessage[], end: number): number {
  return messages.slice(0, end).filter(m => m.role === 'user' && !m.hidden).length
}

export function visibleUserIndexAtOrdinal(messages: readonly ChatMessage[], targetOrdinal: number): number {
  let ordinal = 0

  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]

    if (message.role !== 'user' || message.hidden) {
      continue
    }

    if (ordinal === targetOrdinal) {
      return index
    }

    ordinal += 1
  }

  return -1
}

export interface SubmitTextOptions {
  attachments?: ComposerAttachment[]
  /** The composer scope key that was actually loaded when this text was
   *  submitted (see use-composer-draft's activeQueueSessionKeyRef). Compared
   *  against the resolved submit target in sessionContextDrift — a mismatch
   *  means the composer and the session-side refs disagreed about which
   *  session this send belongs to (#59305). Omit for non-composer submits
   *  (queue drain, steer, external submit requests): the check is a no-op
   *  without it. */
  composerScope?: string | null
  /** What the transcript shows for this send, when it differs from the text
   *  the agent receives. A `/skill` invocation expands into the whole skill
   *  body — model-facing scaffolding the UI must never render — so the slash
   *  dispatcher passes the invocation (`/work fix the leak`) here. */
  displayText?: string
  fromQueue?: boolean
  /** Runtime session id to submit into. Queue drains pass this so a
   *  backgrounded/source session cannot be replaced by the current foreground
   *  session between enqueue and drain. */
  sessionId?: string | null
  /** Stable stored session id for optimistic/cache updates and stale-runtime
   *  recovery. Distinct from the runtime session id minted by the gateway. */
  storedSessionId?: string | null
}
