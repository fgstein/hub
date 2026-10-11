import { createSignal } from "solid-js"
import type { ServerEvent } from "./types"

export type ConnectionStatus = "connecting" | "open" | "reconnecting"

const HEADER = 8
const decoder = new TextDecoder("ascii")

/** Décode une trame binaire de logs : 8 octets d'id de run + données. */
export function parseLogFrame(buf: ArrayBuffer): { runId: string; data: Uint8Array } {
  const bytes = new Uint8Array(buf)
  return { runId: decoder.decode(bytes.subarray(0, HEADER)).trimEnd(), data: bytes.subarray(HEADER) }
}

export interface SocketHandlers {
  onEvent: (ev: ServerEvent) => void
  onLogs: (runId: string, data: Uint8Array) => void
  onOpen: () => void
}

/** WebSocket unique avec reconnexion automatique (backoff exponentiel plafonné). */
export class Socket {
  private ws: WebSocket | null = null
  private attempt = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private closed = false
  readonly status: () => ConnectionStatus
  private setStatus: (s: ConnectionStatus) => void

  constructor(
    private readonly url: string,
    private readonly handlers: SocketHandlers,
  ) {
    const [status, setStatus] = createSignal<ConnectionStatus>("connecting")
    this.status = status
    this.setStatus = setStatus
  }

  connect(): void {
    this.closed = false
    const ws = new WebSocket(this.url)
    ws.binaryType = "arraybuffer"
    this.ws = ws
    ws.onopen = () => {
      this.attempt = 0
      this.setStatus("open")
      this.handlers.onOpen()
    }
    ws.onmessage = (msg) => {
      if (typeof msg.data === "string") {
        try {
          this.handlers.onEvent(JSON.parse(msg.data) as ServerEvent)
        } catch (e) {
          console.error("message WebSocket invalide", e)
        }
      } else {
        const { runId, data } = parseLogFrame(msg.data as ArrayBuffer)
        this.handlers.onLogs(runId, data)
      }
    }
    ws.onclose = (ev) => {
      this.ws = null
      if (this.closed) return
      if (ev.code === 4401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`
        return
      }
      this.setStatus("reconnecting")
      const delay = Math.min(10_000, 400 * 2 ** this.attempt) * (0.8 + Math.random() * 0.4)
      this.attempt++
      this.timer = setTimeout(() => this.connect(), delay)
    }
  }

  send(msg: Record<string, unknown>): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg))
      return true
    }
    return false
  }

  close(): void {
    this.closed = true
    clearTimeout(this.timer)
    this.ws?.close()
  }
}
