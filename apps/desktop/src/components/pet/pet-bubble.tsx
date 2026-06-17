import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { AlertCircle, Clock, type IconComponent } from '@/lib/icons'
import { $petActivity, $petState, type PetState } from '@/store/pet'

/**
 * Speech bubble + status glyph for the popped-out pet overlay — the
 * "notification" half of the mascot. It externalizes what the agent is doing
 * (Codex-style) so a glance at the desktop pet replaces switching back to the
 * window. The in-window pet doesn't show it (the app itself is the surface);
 * only the overlay renders it.
 *
 * Text is derived purely from the same `$petState` / `$petActivity` the sprite
 * already reacts to, so it never drifts from the animation. The bubble is shown
 * only when there's something worth saying (working / reviewing / a transient
 * done/error beat / waiting on the user) and is hidden at plain idle.
 */

interface Bubble {
  /** Optional — a glyph-only bubble collapses to a badge. */
  text?: string
  glyph?: IconComponent
  /** Tone → glyph color. Text stays neutral for legibility. */
  tone?: 'error' | 'wait'
}

// A couple of phrasings per working state, rotated for a touch of life.
const WORKING_LINES = ['working…', 'on it…', 'crunching…']
const REVIEW_LINES = ['thinking…', 'reading…', 'reviewing…']

function bubbleFor(state: PetState, awaitingInput: boolean, tick: number): Bubble | null {
  switch (state) {
    // Finish beats are carried by the sprite/mail icon now; no extra done badge.
    case 'jump':
    case 'wave':
      return null

    case 'failed':
      return { text: 'hit a snag', glyph: AlertCircle, tone: 'error' }

    case 'run':
      return { text: WORKING_LINES[tick % WORKING_LINES.length] }

    case 'review':
      return { text: REVIEW_LINES[tick % REVIEW_LINES.length] }

    case 'waiting':
      return { text: 'your turn', glyph: Clock, tone: 'wait' }

    default:
      // Idle: only speak up if the agent is blocked waiting on the user.
      return awaitingInput ? { text: 'your turn', glyph: Clock, tone: 'wait' } : null
  }
}

const TONE_COLOR: Record<NonNullable<Bubble['tone']>, string> = {
  error: 'var(--ui-red)',
  wait: 'var(--ui-yellow)'
}

export function PetBubble() {
  const state = useStore($petState)
  const activity = useStore($petActivity)
  const [tick, setTick] = useState(0)

  const rotating = state === 'run' || state === 'review'

  // Advance the phrasing while the agent keeps working; reset when it stops so
  // the next working spell starts on the first line.
  useEffect(() => {
    if (!rotating) {
      setTick(0)

      return
    }

    const id = window.setInterval(() => setTick(t => t + 1), 2600)

    return () => window.clearInterval(id)
  }, [rotating])

  const stateBubble = useMemo(
    () => bubbleFor(state, Boolean(activity.awaitingInput), tick),
    [state, activity.awaitingInput, tick]
  )
  const bubble: Bubble | null = stateBubble

  if (!bubble) {
    return null
  }

  const Glyph = bubble.glyph
  const hasText = Boolean(bubble.text)

  return (
    <div
      style={{
        alignItems: 'center',
        // Solid, theme-driven surface (the prior --ui-bg-card mixes in
        // `transparent`, so the bubble was see-through).
        background: 'var(--ui-bg-elevated)',
        border: '1px solid var(--ui-stroke-secondary)',
        borderRadius: hasText ? 10 : 999,
        boxShadow: '0 4px 14px rgba(0,0,0,0.22)',
        color: 'var(--foreground)',
        display: 'inline-flex',
        fontSize: 11,
        fontWeight: 500,
        gap: hasText ? 5 : 0,
        lineHeight: 1,
        // Glyph-only bubbles collapse to a tight, symmetric badge.
        padding: hasText ? '5px 8px' : 5,
        pointerEvents: 'none',
        whiteSpace: 'nowrap'
      }}
    >
      {Glyph && (
        <span style={{ display: 'inline-flex' }}>
          <Glyph style={{ color: bubble.tone ? TONE_COLOR[bubble.tone] : 'currentColor', height: 13, width: 13 }} />
        </span>
      )}
      {bubble.text}
    </div>
  )
}
