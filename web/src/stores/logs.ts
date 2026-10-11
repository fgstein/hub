/**
 * Log subscriptions: reference counting per run, sending logs.subscribe /
 * logs.unsubscribe on the WebSocket, dispatching frames to the open terminals.
 */

export interface LogListener {
  data: (chunk: Uint8Array) => void
  reset: () => void
  end?: () => void
}

type Send = (msg: Record<string, unknown>) => boolean

const listeners = new Map<string, Set<LogListener>>()
let send: Send = () => false

export function bindLogSocket(fn: Send): void {
  send = fn
}

export function subscribeLogs(runId: string, l: LogListener): () => void {
  let set = listeners.get(runId)
  if (!set) {
    set = new Set()
    listeners.set(runId, set)
  }
  set.add(l)
  if (set.size === 1) send({ type: "logs.subscribe", run_id: runId })
  return () => {
    const s = listeners.get(runId)
    if (!s) return
    s.delete(l)
    if (s.size === 0) {
      listeners.delete(runId)
      send({ type: "logs.unsubscribe", run_id: runId })
    }
  }
}

export function dispatchLogs(runId: string, data: Uint8Array): void {
  for (const l of listeners.get(runId) ?? []) l.data(data)
}

export function dispatchReset(runId: string): void {
  for (const l of listeners.get(runId) ?? []) l.reset()
}

export function dispatchEnd(runId: string): void {
  for (const l of listeners.get(runId) ?? []) l.end?.()
}

/** After a reconnection: clear the terminals then resubscribe (the server resends the buffer). */
export function resubscribeAll(): void {
  for (const [runId, set] of listeners) {
    for (const l of set) l.reset()
    send({ type: "logs.subscribe", run_id: runId })
  }
}

export function subscribedRuns(): string[] {
  return [...listeners.keys()]
}
