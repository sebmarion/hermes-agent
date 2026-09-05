import { describe, expect, it } from 'vitest'

import { toChatMessages } from './chat-messages'
import { chatMessagesEquivalent } from '../app/session/hooks/use-session-actions/utils'
import { collectTerminalOutboxDeliveries } from './terminal-outbox'

describe('terminal outbox', () => {
  it('hydrates the persisted async completion delivery id onto its assistant response', () => {
    const messages = toChatMessages([
      {
        role: 'user',
        content: 'background agent work finished',
        display_kind: 'async_delegation_complete',
        display_metadata: {
          delegation_id: 'd1',
          delivery_id: 'async-delegation:d1',
          task_count: 1
        }
      },
      { role: 'assistant', content: 'Recovered answer' }
    ])

    expect(messages[1]?.deliveryId).toBe('async-delegation:d1')
    expect(
      collectTerminalOutboxDeliveries(
        [{ payload: { delivery_id: 'async-delegation:d1', text: 'Recovered answer' } }],
        new Set(messages.flatMap(message => (message.deliveryId ? [message.deliveryId] : []))),
        new Set()
      )
    ).toEqual([{ deliveryId: 'async-delegation:d1', text: 'Recovered answer', shouldRender: false }])
  })

  it('keeps valid unseen deliveries and marks whether rendering is needed', () => {
    const seen = new Set<string>()
    expect(
      collectTerminalOutboxDeliveries(
        [
          { payload: { delivery_id: 'd1', text: 'Recovered answer' } },
          { payload: { delivery_id: 'd1', text: 'Recovered answer' } },
          { payload: { delivery_id: 'd2', text: 'Already stored' } },
          { payload: { delivery_id: 'd3', text: '   ' } }
        ],
        new Set(['d2']),
        seen
      )
    ).toEqual([
      { deliveryId: 'd1', text: 'Recovered answer', shouldRender: true },
      { deliveryId: 'd2', text: 'Already stored', shouldRender: false }
    ])
    expect(seen).toEqual(new Set(['d1', 'd2']))
  })

  it('renders distinct deliveries even when their text is identical', () => {
    const seen = new Set<string>()
    expect(
      collectTerminalOutboxDeliveries(
        [
          { payload: { delivery_id: 'same-text-1', text: 'Repeat' } },
          { payload: { delivery_id: 'same-text-2', text: 'Repeat' } }
        ],
        new Set(),
        seen
      )
    ).toEqual([
      { deliveryId: 'same-text-1', text: 'Repeat', shouldRender: true },
      { deliveryId: 'same-text-2', text: 'Repeat', shouldRender: true }
    ])
  })

  it('allows an unacknowledged delivery to be collected again', () => {
    const seen = new Set<string>()
    const deliveries = [{ payload: { delivery_id: 'retry', text: 'Try again' } }]

    expect(collectTerminalOutboxDeliveries(deliveries, new Set(), seen)).toHaveLength(1)
    expect(collectTerminalOutboxDeliveries(deliveries, new Set(), seen)).toHaveLength(0)
    seen.delete('retry')
    expect(collectTerminalOutboxDeliveries(deliveries, new Set(), seen)).toHaveLength(1)
  })
})


it('hydrates an explicit terminal identity from JSON-encoded metadata', () => {
  const messages = toChatMessages([{
    role: 'assistant', content: 'Recovered answer',
    display_metadata: JSON.stringify({ delivery_id: 'durable-1', delegation_id: 'child-1' })
  }])
  expect(messages[0]?.deliveryId).toBe('durable-1')
})

it('does not discard a newly hydrated terminal identity as an unchanged transcript', () => {
  const [message] = toChatMessages([{ role: 'assistant', content: 'Recovered answer' }])
  expect(chatMessagesEquivalent(message!, { ...message!, deliveryId: 'durable-1' })).toBe(false)
})
