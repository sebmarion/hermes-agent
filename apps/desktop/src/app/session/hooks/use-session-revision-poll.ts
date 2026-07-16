import { useEffect, useRef } from 'react'

import { getAllProfileSessionsRevision } from '@/hermes'

const SESSION_REVISION_POLL_INTERVAL_MS = 5_000
const ALL_PROFILES_SCOPE = '__all__'

export interface UseSessionRevisionPollArgs {
  enabled: boolean
  profileScope: string
  refreshSessions: () => Promise<void>
}

interface LatestProbe {
  generation: number
  run: () => void
}

export function useSessionRevisionPoll({
  enabled,
  profileScope,
  refreshSessions
}: UseSessionRevisionPollArgs): void {
  const generationCounterRef = useRef(0)
  const activeGenerationRef = useRef(0)
  const acknowledgedRevisionRef = useRef<string | null>(null)
  const dirtyRef = useRef(true)
  const inFlightPromiseRef = useRef<Promise<void> | null>(null)
  const pendingGenerationRef = useRef<number | null>(null)
  const latestProbeRef = useRef<LatestProbe | null>(null)
  const failureLoggedRef = useRef(false)

  useEffect(() => {
    const generation = generationCounterRef.current + 1
    generationCounterRef.current = generation
    activeGenerationRef.current = generation
    acknowledgedRevisionRef.current = null
    dirtyRef.current = true
    failureLoggedRef.current = false

    if (!enabled) {
      return
    }

    let cancelled = false
    const requestedProfile =
      profileScope === ALL_PROFILES_SCOPE ? 'all' : profileScope
    const isCurrent = () =>
      !cancelled && activeGenerationRef.current === generation

    const recordFailure = (error: unknown) => {
      if (!isCurrent()) {
        return
      }

      dirtyRef.current = true
      if (!failureLoggedRef.current) {
        console.warn('Session revision refresh failed; retrying', error)
        failureLoggedRef.current = true
      }
    }

    const probe = () => {
      if (!isCurrent()) {
        return
      }

      if (inFlightPromiseRef.current !== null) {
        pendingGenerationRef.current = activeGenerationRef.current
        return
      }

      const work = (async () => {
        try {
          const candidate = (
            await getAllProfileSessionsRevision(requestedProfile)
          ).revision

          if (!isCurrent()) {
            return
          }

          if (
            !dirtyRef.current &&
            acknowledgedRevisionRef.current === candidate
          ) {
            failureLoggedRef.current = false
            return
          }

          await refreshSessions()

          if (!isCurrent()) {
            return
          }

          const confirmed = (
            await getAllProfileSessionsRevision(requestedProfile)
          ).revision

          if (!isCurrent()) {
            return
          }

          failureLoggedRef.current = false
          if (confirmed === candidate) {
            acknowledgedRevisionRef.current = confirmed
            dirtyRef.current = false
          } else {
            dirtyRef.current = true
            pendingGenerationRef.current = generation
          }
        } catch (error) {
          recordFailure(error)
        }
      })()

      inFlightPromiseRef.current = work
      void work.finally(() => {
        if (inFlightPromiseRef.current === work) {
          inFlightPromiseRef.current = null
        }

        const pendingGeneration = pendingGenerationRef.current
        if (pendingGeneration === null) {
          return
        }
        pendingGenerationRef.current = null

        const latestProbe = latestProbeRef.current
        if (
          pendingGeneration === activeGenerationRef.current &&
          latestProbe?.generation === pendingGeneration
        ) {
          queueMicrotask(latestProbe.run)
        }
      })
    }

    latestProbeRef.current = { generation, run: probe }
    probe()
    const interval = window.setInterval(
      probe,
      SESSION_REVISION_POLL_INTERVAL_MS
    )
    const removePowerResumeListener =
      window.hermesDesktop.onPowerResume?.(probe)

    return () => {
      cancelled = true
      window.clearInterval(interval)
      removePowerResumeListener?.()
      if (latestProbeRef.current?.generation === generation) {
        latestProbeRef.current = null
      }
      if (pendingGenerationRef.current === generation) {
        pendingGenerationRef.current = null
      }
    }
  }, [enabled, profileScope, refreshSessions])
}
