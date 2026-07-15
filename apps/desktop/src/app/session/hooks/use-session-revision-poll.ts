import { useEffect, useRef } from 'react'

import { getAllProfileSessionsRevision } from '@/hermes'
import { ALL_PROFILES } from '@/store/profile'

const SESSION_REVISION_POLL_INTERVAL_MS = 5_000

export interface UseSessionRevisionPollArgs {
  enabled: boolean
  profileScope: string
  refreshSessions: () => Promise<SessionRefreshResult>
}

export type SessionRefreshResult = 'applied' | 'superseded'

interface PollGeneration {
  cancelled: boolean
  id: number
  profileScope: string
  refreshSessions: () => Promise<SessionRefreshResult>
}

interface PollCoordinator {
  acknowledgedRevision: string | null
  activeGeneration: PollGeneration | null
  dirty: boolean
  disposed: boolean
  failureActive: boolean
  inFlightPromise: Promise<void> | null
  latestProbe: (() => void) | null
  nextGeneration: number
  pendingGeneration: number | null
}

function createCoordinator(): PollCoordinator {
  return {
    acknowledgedRevision: null,
    activeGeneration: null,
    dirty: true,
    disposed: false,
    failureActive: false,
    inFlightPromise: null,
    latestProbe: null,
    nextGeneration: 0,
    pendingGeneration: null
  }
}

export function useSessionRevisionPoll({ enabled, profileScope, refreshSessions }: UseSessionRevisionPollArgs): void {
  const coordinatorRef = useRef<PollCoordinator | null>(null)

  if (coordinatorRef.current === null) {
    coordinatorRef.current = createCoordinator()
  }

  const coordinator = coordinatorRef.current

  useEffect(
    () => () => {
      coordinator.disposed = true
      coordinator.activeGeneration = null
      coordinator.latestProbe = null
      coordinator.pendingGeneration = null

      const outstandingPromise = coordinator.inFlightPromise

      const dispose = (): void => {
        if (!coordinator.disposed) {
          return
        }

        coordinator.acknowledgedRevision = null
        coordinator.dirty = true
        coordinator.failureActive = false

        if (coordinator.inFlightPromise === outstandingPromise) {
          coordinator.inFlightPromise = null
        }
      }

      if (outstandingPromise) {
        void outstandingPromise.finally(dispose)
      } else {
        dispose()
      }
    },
    [coordinator]
  )

  useEffect(() => {
    const generationId = ++coordinator.nextGeneration

    if (!enabled) {
      coordinator.activeGeneration = null
      coordinator.acknowledgedRevision = null
      coordinator.dirty = true
      coordinator.latestProbe = null
      coordinator.pendingGeneration = null

      return
    }

    const generation: PollGeneration = {
      cancelled: false,
      id: generationId,
      profileScope,
      refreshSessions
    }

    const revisionProfileScope = generation.profileScope === ALL_PROFILES ? 'all' : generation.profileScope

    coordinator.disposed = false
    coordinator.activeGeneration = generation
    coordinator.acknowledgedRevision = null
    coordinator.dirty = true
    coordinator.failureActive = false

    const isCurrent = (): boolean =>
      !coordinator.disposed && !generation.cancelled && coordinator.activeGeneration?.id === generation.id

    const recordFailure = (error: unknown): void => {
      if (!isCurrent()) {
        return
      }

      coordinator.dirty = true

      if (coordinator.failureActive) {
        return
      }

      coordinator.failureActive = true
      console.warn('Session revision polling failed; retrying in the background.', error)
    }

    const recordSuccess = (): void => {
      if (isCurrent()) {
        coordinator.failureActive = false
      }
    }

    const runCycle = async (): Promise<void> => {
      try {
        const candidate = (await getAllProfileSessionsRevision(revisionProfileScope)).revision

        if (!isCurrent()) {
          return
        }

        if (!coordinator.dirty && coordinator.acknowledgedRevision === candidate) {
          recordSuccess()

          return
        }

        coordinator.dirty = true
        const refreshResult = await generation.refreshSessions()

        if (!isCurrent()) {
          return
        }

        if (refreshResult === 'superseded') {
          coordinator.dirty = true
          recordSuccess()

          return
        }

        const confirmed = (await getAllProfileSessionsRevision(revisionProfileScope)).revision

        if (!isCurrent()) {
          return
        }

        if (confirmed !== candidate) {
          coordinator.dirty = true
          coordinator.pendingGeneration = generation.id
          recordSuccess()

          return
        }

        coordinator.acknowledgedRevision = candidate
        coordinator.dirty = false
        recordSuccess()
      } catch (error) {
        recordFailure(error)
      }
    }

    const probe = (): void => {
      if (!isCurrent()) {
        return
      }

      if (coordinator.inFlightPromise) {
        coordinator.pendingGeneration = generation.id

        return
      }

      const task = runCycle()
      coordinator.inFlightPromise = task
      void task.finally(() => {
        if (coordinator.inFlightPromise !== task) {
          return
        }

        coordinator.inFlightPromise = null

        if (coordinator.disposed) {
          return
        }

        const pendingGeneration = coordinator.pendingGeneration
        coordinator.pendingGeneration = null
        const activeGeneration = coordinator.activeGeneration

        if (pendingGeneration !== null && activeGeneration?.id === pendingGeneration && !activeGeneration.cancelled) {
          coordinator.latestProbe?.()
        }
      })
    }

    coordinator.latestProbe = probe
    probe()

    const intervalId = window.setInterval(probe, SESSION_REVISION_POLL_INTERVAL_MS)

    const removePowerResumeListener = window.hermesDesktop.onPowerResume?.(probe)

    return () => {
      generation.cancelled = true
      window.clearInterval(intervalId)
      removePowerResumeListener?.()

      if (coordinator.activeGeneration?.id === generation.id) {
        coordinator.activeGeneration = null
        coordinator.latestProbe = null
      }
    }
  }, [coordinator, enabled, profileScope, refreshSessions])
}
