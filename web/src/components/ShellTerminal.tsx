import type { FitAddon } from "@xterm/addon-fit"
import type { Terminal } from "@xterm/xterm"
import { createEffect, createSignal, on, onCleanup, onMount, Show } from "solid-js"
import { resolvedTheme } from "~/stores/theme"
import { IconMaximize, IconMinimize, IconRefresh, IconSpinner } from "./Icons"
import { loadXterm, xtermTheme } from "./LogTerminal"

type ShellState = "connecting" | "open" | "closed"

/**
 * Interactive shell in a run's container (``docker exec -it`` through ``/ws/shell/<id>``).
 * The session lives as long as the component is mounted; “Reconnect” opens a new one.
 */
export function ShellTerminal(props: {
  runId: string
  containerName: string
  active: boolean
  fullscreen: boolean
  onToggleFullscreen: () => void
}) {
  let host!: HTMLDivElement
  let term: Terminal | undefined
  let fit: FitAddon | undefined
  let ws: WebSocket | undefined
  let disposed = false
  let ro: ResizeObserver | undefined
  const encoder = new TextEncoder()
  const [state, setState] = createSignal<ShellState>("connecting")
  const [height, setHeight] = createSignal(360)

  const sendResize = () => {
    if (term && ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }))
    }
  }

  const refit = () => {
    try {
      fit?.fit()
    } catch {
      /* terminal hidden */
    }
  }

  const connect = () => {
    if (!term || disposed) return
    ws?.close()
    setState("connecting")
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    const q = new URLSearchParams({ cols: String(term.cols), rows: String(term.rows) })
    const sock = new WebSocket(
      `${proto}//${window.location.host}/ws/shell/${encodeURIComponent(props.runId)}?${q}`,
    )
    sock.binaryType = "arraybuffer"
    ws = sock
    sock.onopen = () => {
      setState("open")
      term?.focus()
    }
    sock.onmessage = (msg) => {
      if (typeof msg.data === "string") {
        try {
          const data = JSON.parse(msg.data) as { type: string; message?: string }
          if (data.type === "error") term?.write(`\r\n\x1b[31m${data.message ?? "error"}\x1b[0m\r\n`)
        } catch {
          /* non-JSON message */
        }
        return
      }
      term?.write(new Uint8Array(msg.data as ArrayBuffer))
    }
    sock.onclose = () => {
      if (ws !== sock) return
      setState("closed")
      term?.write("\r\n\x1b[2m[session ended]\x1b[0m\r\n")
    }
  }

  onMount(async () => {
    const [{ Terminal }, { FitAddon }, , { WebLinksAddon }] = await loadXterm()
    if (disposed) return
    term = new Terminal({
      fontFamily:
        getComputedStyle(document.documentElement).getPropertyValue("--font-mono") ||
        "IBM Plex Mono, monospace",
      fontSize: 12,
      lineHeight: 1.25,
      scrollback: 10_000,
      cursorBlink: true,
      allowProposedApi: true,
      theme: xtermTheme(),
    })
    fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(host)
    refit()
    term.onData((data) => {
      if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(data))
    })
    term.onResize(sendResize)
    ro = new ResizeObserver(refit)
    ro.observe(host)
    connect()
  })

  createEffect(
    on(resolvedTheme, () => {
      if (term) term.options.theme = xtermTheme()
    }),
  )
  createEffect(
    on(
      () => [props.active, props.fullscreen] as const,
      ([active]) =>
        requestAnimationFrame(() => {
          refit()
          if (active) term?.focus()
        }),
      { defer: true },
    ),
  )

  onCleanup(() => {
    disposed = true
    ro?.disconnect()
    const sock = ws
    ws = undefined
    sock?.close()
    term?.dispose()
  })

  const startResize = (e: PointerEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const start = height()
    const move = (ev: PointerEvent) =>
      setHeight(Math.max(140, Math.min(window.innerHeight * 0.85, start + ev.clientY - startY)))
    const up = () => {
      window.removeEventListener("pointermove", move)
      window.removeEventListener("pointerup", up)
    }
    window.addEventListener("pointermove", move)
    window.addEventListener("pointerup", up)
  }

  const toolBtn =
    "flex size-6 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"

  return (
    <div
      class="relative flex flex-col overflow-hidden bg-[var(--term-bg)]"
      classList={{
        "rounded-md border border-line-weak": !props.fullscreen,
        "min-h-0 flex-1": props.fullscreen,
      }}
      style={props.fullscreen ? undefined : { height: `${height()}px` }}
      data-testid="shell-terminal"
      data-state={state()}
      onClick={(e) => e.stopPropagation()}
    >
      <div class="flex h-7 shrink-0 items-center gap-0.5 border-b border-line-weaker px-2 text-xs text-fg-weak">
        <span class="mr-auto truncate font-mono">
          shell · {props.containerName}
          <Show when={state() === "closed"}>
            <span class="ml-2 text-fg-weaker">disconnected</span>
          </Show>
        </span>
        <Show when={state() === "connecting"}>
          <IconSpinner size={11} class="mr-1" />
        </Show>
        <Show when={state() === "closed"}>
          <button
            type="button"
            class="flex h-6 items-center gap-1 rounded px-1.5 text-fg hover:bg-surface-hover"
            onClick={() => {
              term?.reset()
              connect()
            }}
            data-testid="shell-reconnect"
          >
            <IconRefresh size={11} /> Reconnect
          </button>
        </Show>
        <button
          type="button"
          class={toolBtn}
          title={props.fullscreen ? "Exit fullscreen" : "Fullscreen"}
          aria-label={props.fullscreen ? "Exit fullscreen" : "Fullscreen"}
          onClick={props.onToggleFullscreen}
        >
          <Show when={props.fullscreen} fallback={<IconMaximize size={12} />}>
            <IconMinimize size={12} />
          </Show>
        </button>
      </div>
      <div ref={host} class="terminal-host min-h-0 flex-1" data-testid="shell-host" />
      <Show when={!props.fullscreen}>
        <div
          class="absolute inset-x-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-line-weak"
          onPointerDown={startResize}
          title="Resize"
        />
      </Show>
    </div>
  )
}
