export interface TerminalOutboxDelivery {
  deliveryId: string
  text: string
  shouldRender: boolean
}

export function collectTerminalOutboxDeliveries(
  deliveries: unknown,
  existingDeliveryIds: ReadonlySet<string>,
  seen: Set<string>
): TerminalOutboxDelivery[] {
  if (!Array.isArray(deliveries)) {
    return []
  }

  const result: TerminalOutboxDelivery[] = []
  const batchSeen = new Set(seen)

  for (const delivery of deliveries) {
    if (!delivery || typeof delivery !== 'object') {
      continue
    }

    const payload = (delivery as { payload?: unknown }).payload

    if (!payload || typeof payload !== 'object') {
      continue
    }

    const deliveryId = (payload as { delivery_id?: unknown }).delivery_id
    const text = (payload as { text?: unknown }).text

    if (
      typeof deliveryId !== 'string' ||
      !deliveryId.trim() ||
      typeof text !== 'string' ||
      !text.trim() ||
      batchSeen.has(deliveryId)
    ) {
      continue
    }

    batchSeen.add(deliveryId)
    seen.add(deliveryId)
    result.push({ deliveryId, text, shouldRender: !existingDeliveryIds.has(deliveryId) })
  }

  return result
}
