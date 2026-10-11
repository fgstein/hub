import type { FitAddon } from "@xterm/addon-fit"
import type { SearchAddon } from "@xterm/addon-search"
import type { ITheme, Terminal } from "@xterm/xterm"
import { createEffect, createSignal, on, onCleanup, onMount, Show } from "solid-js"
import { api } from "~/lib/api"
import { subscribeLogs } from "~/stores/logs"
import { resolvedTheme } from "~/stores/theme"
import { toast } from "~/stores/toasts"
import {
  IconArrowDown,
  IconCopy,
  IconDownload,
  IconMaximize,
  IconMinimize,
  IconSearch,
  IconSpinner,
} from "./Icons"

/** Chargement paresseux d'xterm (hors du bundle principal). */
type XtermModules = readonly [
  typeof import("@xterm/xterm"),
  typeof import("@xterm/addon-fit"),
  typeof import("@xterm/addon-search"),
  typeof import("@xterm/addon-web-links"),
]
let xtermModules: Promise<XtermModules> | null = null

function loadXterm(): Promise<XtermModules> {
  xtermModules ??= Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
    import("@xterm/addon-search"),
    import("@xterm/addon-web-links"),
    import("@xterm/xterm/css/xterm.css"),
  ]).then(([a, b, c, d]) => [a, b, c, d] as const)
  return xtermModules
}

const DARK_ANSI = {
  black: "#1a1a1a",
  red: "#fc6b55",
  green: "#4ade80",
  yellow: "#fcd34d",
  blue: "#7aa2ff",
  magenta: "#e6a8ea",
  cyan: "#5fd4e6",
  white: "#d6d6d6",
  brightBlack: "#6b6b6b",
  brightRed: "#ff8a75",
  brightGreen: "#86efac",
  brightYellow: "#fde68a",
  brightBlue: "#a5c0ff",
  brightMagenta: "#f0c4f3",
  brightCyan: "#99e6f2",
  brightWhite: "#ffffff",
}

const LIGHT_ANSI = {
  black: "#2a2a2a",
  red: "#d43a22",
  green: "#178a12",
  yellow: "#a46a00",
  blue: "#034cff",
  magenta: "#a753ae",
  cyan: "#007b80",
  white: "#8f8f8f",
  brightBlack: "#6f6f6f",
  brightRed: "#ed4831",
  brightGreen: "#2dba26",
  brightYellow: "#c47f00",
  brightBlue: "#2f6bff",
  brightMagenta: "#c06bc7",
  brightCyan: "#0092a8",
  brightWhite: "#171717",
}

function xtermTheme(): ITheme {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string) => css.getPropertyValue(name).trim()
  return {
    background: v("--term-bg"),
    foreground: v("--term-fg"),
    cursor: v("--term-cursor"),
    cursorAccent: v("--term-bg"),
    selectionBackground: v("--term-selection"),
    scrollbarSliderBackground: "rgba(128,128,128,0.25)",
    scrollbarSliderHoverBackground: "rgba(128,128,128,0.4)",
    ...(resolvedTheme() === "dark" ? DARK_ANSI : LIGHT_ANSI),
  }
}

const MAX_PER_FRAME = 512 * 1024

export function LogTerminal(props: { runId: string; fullscreen: boolean; onToggleFullscreen: () => void }) {
  let host!: HTMLDivElement
  let term: Terminal | undefined
  let fit: FitAddon | undefined
  let search: SearchAddon | undefined
  let searchInput: HTMLInputElement | undefined
  const queue: Uint8Array[] = []
  let raf = 0
  let disposed = false

  const [height, setHeight] = createSignal(360)
  const [ready, setReady] = createSignal(false)
  const [atBottom, setAtBottom] = createSignal(true)
  const [newLines, setNewLines] = createSignal(false)
  const [showSearch, setShowSearch] = createSignal(false)
  const [query, setQuery] = createSignal("")

  const checkBottom = () => {
    if (!term) return
    const b = term.buffer.active
    const bottom = b.viewportY >= b.baseY
    setAtBottom(bottom)
    if (bottom) setNewLines(false)
  }

  const flush = () => {
    raf = 0
    if (!term || queue.length === 0) return
    let size = 0
    const parts: Uint8Array[] = []
    while (queue.length && size < MAX_PER_FRAME) {
      const c = queue.shift()!
      parts.push(c)
      size += c.length
    }
    const merged = parts.length === 1 ? parts[0]! : new Uint8Array(size)
    if (parts.length > 1) {
      let o = 0
      for (const p of parts) {
        merged.set(p, o)
        o += p.length
      }
    }
    const wasBottom = atBottom()
    term.write(merged, () => {
      if (!wasBottom) setNewLines(true)
      else term?.scrollToBottom()
    })
    if (queue.length) raf = requestAnimationFrame(flush)
  }

  const enqueue = (chunk: Uint8Array) => {
    queue.push(chunk.slice())
    if (!raf) raf = requestAnimationFrame(flush)
  }

  onMount(async () => {
    const [{ Terminal }, { FitAddon }, { SearchAddon }, { WebLinksAddon }] = await loadXterm()
    if (disposed) return
    term = new Terminal({
      fontFamily:
        getComputedStyle(document.documentElement).getPropertyValue("--font-mono") ||
        "IBM Plex Mono, monospace",
      fontSize: 12,
      lineHeight: 1.25,
      scrollback: 50_000,
      convertEol: false,
      disableStdin: true,
      cursorBlink: false,
      cursorStyle: "bar",
      cursorInactiveStyle: "none",
      allowProposedApi: true,
      theme: xtermTheme(),
    })
    fit = new FitAddon()
    search = new SearchAddon()
    term.loadAddon(fit)
    term.loadAddon(search)
    term.loadAddon(new WebLinksAddon())
    term.open(host)
    term.onScroll(checkBottom)
    host.addEventListener("wheel", () => requestAnimationFrame(checkBottom), { passive: true })
    fit.fit()
    setReady(true)
    const ro = new ResizeObserver(() => {
      try {
        fit?.fit()
      } catch {
        /* terminal pas encore visible */
      }
    })
    ro.observe(host)
    const unsubscribe = subscribeLogs(props.runId, {
      data: enqueue,
      reset: () => {
        queue.length = 0
        term?.reset()
        setNewLines(false)
        setAtBottom(true)
      },
    })
    onCleanup(() => {
      ro.disconnect()
      unsubscribe()
    })
  })

  createEffect(
    on(resolvedTheme, () => {
      if (term) term.options.theme = xtermTheme()
    }),
  )
  createEffect(
    on(
      () => props.fullscreen,
      () => requestAnimationFrame(() => fit?.fit()),
      { defer: true },
    ),
  )

  onCleanup(() => {
    disposed = true
    cancelAnimationFrame(raf)
    term?.dispose()
  })

  const scrollDown = () => {
    term?.scrollToBottom()
    setNewLines(false)
    setAtBottom(true)
  }

  const copy = async () => {
    if (!term) return
    let text = term.getSelection()
    if (!text) {
      term.selectAll()
      text = term.getSelection()
      term.clearSelection()
    }
    try {
      await navigator.clipboard.writeText(text)
      toast("logs copiés", "success")
    } catch {
      toast("copie impossible", "error")
    }
  }

  const find = (backwards = false) => {
    const q = query()
    if (!q || !search) return
    const opts = {
      caseSensitive: false,
      decorations: {
        matchOverviewRuler: "#fcb239",
        activeMatchColorOverviewRuler: "#fc533a",
        matchBackground: "#fcb23955",
        activeMatchBackground: "#fc533a88",
      },
    }
    if (backwards) search.findPrevious(q, opts)
    else search.findNext(q, opts)
  }

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
      data-testid="terminal"
      onClick={(e) => e.stopPropagation()}
    >
      <div class="flex h-7 shrink-0 items-center gap-0.5 border-b border-line-weaker px-2 text-xs text-fg-weak">
        <span class="mr-auto font-mono">logs · {props.runId}</span>
        <Show when={!ready()}>
          <IconSpinner size={11} class="mr-1" />
        </Show>
        <Show when={showSearch()}>
          <input
            ref={searchInput}
            value={query()}
            placeholder="Rechercher…"
            class="mr-1 h-5 w-44 rounded bg-input px-1.5 font-mono text-xs text-fg-strong outline-none"
            onInput={(e) => {
              setQuery(e.currentTarget.value)
              search?.clearDecorations()
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") find(e.shiftKey)
              if (e.key === "Escape") {
                e.stopPropagation()
                setShowSearch(false)
                search?.clearDecorations()
              }
            }}
            data-testid="terminal-search-input"
          />
        </Show>
        <button
          type="button"
          class={toolBtn}
          title="Rechercher (Entrée : suivant, Maj+Entrée : précédent)"
          aria-label="Rechercher"
          onClick={() => {
            setShowSearch((v) => !v)
            queueMicrotask(() => searchInput?.focus())
          }}
        >
          <IconSearch size={12} />
        </button>
        <button
          type="button"
          class={toolBtn}
          title="Copier (sélection ou tout)"
          aria-label="Copier"
          onClick={copy}
        >
          <IconCopy size={12} />
        </button>
        <a
          class={toolBtn}
          href={api.logsUrl(props.runId)}
          download={`trainhub-${props.runId}.log`}
          title="Télécharger les logs"
          aria-label="Télécharger les logs"
        >
          <IconDownload size={12} />
        </a>
        <button
          type="button"
          class={toolBtn}
          title={props.fullscreen ? "Quitter le plein écran (Échap)" : "Plein écran"}
          aria-label={props.fullscreen ? "Quitter le plein écran" : "Plein écran"}
          onClick={props.onToggleFullscreen}
          data-testid="terminal-fullscreen"
        >
          <Show when={props.fullscreen} fallback={<IconMaximize size={12} />}>
            <IconMinimize size={12} />
          </Show>
        </button>
      </div>
      <div ref={host} class="terminal-host min-h-0 flex-1" data-testid="terminal-host" />
      <Show when={newLines() && !atBottom()}>
        <button
          type="button"
          class="anim-in absolute bottom-4 right-6 flex items-center gap-1 rounded-full bg-primary px-2.5 py-1 text-xs text-primary-fg shadow-float"
          onClick={scrollDown}
          data-testid="terminal-new-lines"
        >
          <IconArrowDown size={11} /> nouvelles lignes
        </button>
      </Show>
      <Show when={!props.fullscreen}>
        <div
          class="absolute inset-x-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-line-weak"
          onPointerDown={startResize}
          title="Redimensionner"
        />
      </Show>
    </div>
  )
}
