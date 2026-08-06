import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Mock useAuiState to return true (message is running) so ClarifyToolLive
// renders ClarifyToolPending instead of falling back to ToolFallback.
vi.mock('@assistant-ui/react', async () => {
  const actual = await vi.importActual('@assistant-ui/react')

  return {
    ...actual,
    useAuiState: vi.fn((selector?: (s: unknown) => unknown) =>
      typeof selector === 'function' ? selector({ thread: { isRunning: true }, message: { status: { type: 'running' } } }) : true
    )
  }
})

import { I18nProvider } from '@/i18n'
import { $clarifyRequest, $clarifyRequests, clearClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool, readClarifyResult } from './clarify-tool'

vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

function renderClarify(ui: ReactNode) {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function settledClarifyProps(
  args: ToolCallMessagePartProps['args'],
  result: ToolCallMessagePartProps['result'],
  toolCallId: string
): ToolCallMessagePartProps {
  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result,
    resume: vi.fn(),
    status: { type: 'complete' },
    toolCallId,
    toolName: 'clarify',
    type: 'tool-call'
  }
}

describe('readClarifyResult', () => {
  it('reads question + user_response from the tool JSON payload', () => {
    expect(
      readClarifyResult({
        question: 'Which target?',
        choices_offered: ['staging', 'prod'],
        user_response: 'staging'
      })
    ).toEqual({
      question: 'Which target?',
      answer: 'staging',
      error: undefined
    })
  })

  it('parses a JSON string result the same way as an object', () => {
    expect(
      readClarifyResult(
        JSON.stringify({
          question: 'Ship it?',
          user_response: 'yes'
        })
      )
    ).toEqual({
      question: 'Ship it?',
      answer: 'yes',
      error: undefined
    })
  })

  it('keeps an empty user_response so Skip can render as skipped', () => {
    expect(readClarifyResult({ question: 'Ok?', user_response: '' })).toEqual({
      question: 'Ok?',
      answer: '',
      error: undefined
    })
  })
})

describe('ClarifyTool settled view', () => {
  afterEach(() => {
    cleanup()
  })

  it('keeps the question and answer visible after the tool completes', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'staging'
          },
          'clarify-1'
        )}
      />
    )

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-answer]')?.textContent).toBe('staging')
  })

  it('labels an empty response as Skipped', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps({ question: 'Anything else?' }, { question: 'Anything else?', user_response: '' }, 'clarify-2')}
      />
    )

    expect(screen.getByText('Anything else?')).toBeTruthy()
    expect(screen.getByText('Skipped')).toBeTruthy()
  })
})

describe('ClarifyTool auto-dismiss on expiry', () => {
  beforeEach(() => {
    $activeSessionId.set('s1')
    $clarifyRequests.set({})
    $gateway.set({ request: vi.fn().mockResolvedValue({ ok: true }) } as never)
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    $activeSessionId.set(null)
    $gateway.set(null)
    vi.clearAllMocks()
  })

  it('dismisses the panel when clarify.respond fails with "no pending answer request"', async () => {
    const rejectError = new Error('RPC failed: no pending answer request')
    const request = vi.fn().mockRejectedValue(rejectError)
    $gateway.set({ request } as never)

    $clarifyRequests.set({
      s1: {
        requestId: 'req-1',
        question: 'Pick one',
        choices: ['A', 'B'],
        sessionId: 's1'
      }
    })

    renderClarify(
      <ClarifyTool
        {...({
          addResult: vi.fn(),
          args: { question: 'Pick one', choices: ['A', 'B'] },
          argsText: JSON.stringify({ question: 'Pick one', choices: ['A', 'B'] }),
          isError: false,
          respondToApproval: vi.fn(),
          result: undefined,
          resume: vi.fn(),
          status: { type: 'running' },
          toolCallId: 'tc-1',
          toolName: 'clarify',
          type: 'tool-call'
        } as ToolCallMessagePartProps)}
      />
    )

    await waitFor(() => expect(screen.getByText('Pick one')).toBeTruthy())

    const choiceButton = document.querySelector('[data-choice]') as HTMLElement
    expect(choiceButton).toBeTruthy()
    fireEvent.click(choiceButton)

    const continueButton = screen.getByRole('button', { name: /Continue/ })
    fireEvent.click(continueButton)

    await waitFor(() => {
      expect($clarifyRequest.get()).toBeNull()
    })
    expect(notifyError).toHaveBeenCalledWith(rejectError, expect.stringContaining('expired'))
  })

  it('keeps the panel visible on non-expiry errors (e.g. gateway disconnected)', async () => {
    const rejectError = new Error('gateway not connected')
    const request = vi.fn().mockRejectedValue(rejectError)
    $gateway.set({ request } as never)

    $clarifyRequests.set({
      s1: {
        requestId: 'req-1',
        question: 'Pick one',
        choices: ['A', 'B'],
        sessionId: 's1'
      }
    })

    renderClarify(
      <ClarifyTool
        {...({
          addResult: vi.fn(),
          args: { question: 'Pick one', choices: ['A', 'B'] },
          argsText: JSON.stringify({ question: 'Pick one', choices: ['A', 'B'] }),
          isError: false,
          respondToApproval: vi.fn(),
          result: undefined,
          resume: vi.fn(),
          status: { type: 'running' },
          toolCallId: 'tc-1',
          toolName: 'clarify',
          type: 'tool-call'
        } as ToolCallMessagePartProps)}
      />
    )

    await waitFor(() => expect(screen.getByText('Pick one')).toBeTruthy())

    const choiceButton = document.querySelector('[data-choice]') as HTMLElement
    fireEvent.click(choiceButton)

    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => expect(request).toHaveBeenCalled())

    expect($clarifyRequest.get()).not.toBeNull()
    expect(notifyError).toHaveBeenCalledWith(rejectError, expect.not.stringContaining('expired'))
  })
})
