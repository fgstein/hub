import { Tooltip } from "@kobalte/core/tooltip"
import { createEffect, createSignal, type JSX, on, onCleanup, Show } from "solid-js"
import { elapsedSeconds, formatDateTime, formatDuration, pausedSeconds } from "~/lib/format"
import { isActive } from "~/lib/types"
import { displayStatus, fullscreenRun, setFullscreenRun, state } from "~/stores/app"
import { now } from "~/stores/clock"
import { ActionButton } from "./ActionButton"
import { IconAgent, IconChevronDown } from "./Icons"
import { LogTerminal } from "./LogTerminal"
import { StatusDot, statusLabel } from "./StatusDot"

const UNSUBSCRIBE_DELAY_MS = 10_000

function Tip(props: { content: JSX.Element; children: JSX.Element; class?: string }) {
  return (
    <Tooltip openDelay={350} placement="top">
      <Tooltip.Trigger as="span" class={props.class}>
        {props.children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content class="z-[90] max-w-md rounded-md bg-float px-2.5 py-1.5 text-xs text-fg shadow-float">
          {props.content}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip>
  )
}

/** Partie gauche + centrale + droite d'un run (réutilisée dans l'en-tête plein écran). */
function RunLine(props: { id: string; open?: boolean; onToggle?: () => void; header?: boolean }) {
  const run = () => state.runs[props.id]!
  const status = () => displayStatus(props.id)
  const elapsed = () => formatDuration(elapsedSeconds(run(), isActive(run().status) ? now() : 0))
  const paused = () => pausedSeconds(run(), now())
  const inv = () => state.inventory
  const imageFull = () => inv()?.images.find((i) => i.id === run().image_id)?.image ?? run().image_id
  const machine = () => inv()?.machines.find((m) => m.id === run().machine_id)
  const venvPath = () => inv()?.venvs.find((v) => v.id === run().venv_id)?.path ?? run().venv_id

  return (
    <div
      class="group/line flex h-12 items-center gap-3 px-4"
      classList={{ "cursor-pointer": !!props.onToggle }}
      onClick={() => props.onToggle?.()}
      data-testid="run-line"
    >
      <StatusDot status={status()} />
      <Tip
        class="w-[68px] shrink-0 font-mono text-sm tabular text-fg-strong"
        content={
          <div class="space-y-0.5 font-mono">
            <div>
              {statusLabel(status())} · {run().id}
            </div>
            <div>démarré : {formatDateTime(run().started_at)}</div>
            <Show when={run().finished_at}>
              <div>terminé : {formatDateTime(run().finished_at)}</div>
            </Show>
            <Show when={paused() > 0}>
              <div>dont en pause : {formatDuration(paused())}</div>
            </Show>
            <Show when={run().exit_code != null}>
              <div>code de sortie : {run().exit_code}</div>
            </Show>
          </div>
        }
      >
        <span data-testid="run-elapsed">{elapsed()}</span>
      </Tip>
      <div class="flex min-w-0 flex-1 items-center gap-1.5 whitespace-nowrap font-mono text-sm text-fg">
        <Tip
          class="min-w-0 truncate"
          content={`${machine()?.label ?? run().machine_id} — ${machine()?.host ?? ""}`}
        >
          {run().machine_id}
        </Tip>
        <span class="text-fg-weaker">·</span>
        <Tip class="min-w-0 truncate" content={imageFull()}>
          {run().image_id}
        </Tip>
        <span class="text-fg-weaker">·</span>
        <Tip class="min-w-0 truncate" content={venvPath()}>
          {run().venv_id}
        </Tip>
        <span class="text-fg-weaker">·</span>
        <Tip class="min-w-0 truncate text-fg-strong" content={run().command}>
          {run().config}
        </Tip>
        <Show when={run().cuda_visible_devices}>
          <Tip class="shrink-0 rounded bg-surface px-1 text-xs text-fg-weak" content="CUDA_VISIBLE_DEVICES">
            gpu {run().cuda_visible_devices}
          </Tip>
        </Show>
      </div>
      <Show when={run().source === "mcp"}>
        <Tip class="shrink-0 text-icon" content="Lancé par un agent (MCP)">
          <IconAgent size={14} />
        </Tip>
      </Show>
      <Show when={run().source === "import"}>
        <Tip class="shrink-0 text-xs text-fg-weak" content="Conteneur retrouvé sur la machine et importé">
          importé
        </Tip>
      </Show>
      <ActionButton run={run()} />
    </div>
  )
}

export function RunRow(props: { id: string }) {
  const run = () => state.runs[props.id]
  const [open, setOpen] = createSignal(false)
  const [mounted, setMounted] = createSignal(false)
  let unmountTimer: ReturnType<typeof setTimeout> | undefined
  const fullscreen = () => fullscreenRun() === props.id

  createEffect(
    on(open, (o) => {
      clearTimeout(unmountTimer)
      if (o) setMounted(true)
      // Replier un terminal se désabonne du flux après 10 s.
      else unmountTimer = setTimeout(() => setMounted(false), UNSUBSCRIBE_DELAY_MS)
    }),
  )
  onCleanup(() => clearTimeout(unmountTimer))

  const toggle = () => setOpen((o) => !o)

  return (
    <Show when={run()}>
      <div
        class="anim-in border-b border-line-weaker"
        data-testid="run-row"
        data-run-id={props.id}
        data-status={displayStatus(props.id)}
      >
        <div class="group relative">
          <RunLine id={props.id} onToggle={toggle} />
          <Show when={(run()!.status === "unreachable" || run()!.status === "failed") && run()!.error}>
            <div
              class="-mt-2 truncate px-4 pb-1.5 pl-[100px] text-xs text-fg-critical"
              data-testid="run-error"
              title={run()!.error ?? ""}
            >
              {run()!.error}
            </div>
          </Show>
          <button
            type="button"
            class="absolute bottom-0 left-1/2 flex h-3 w-10 -translate-x-1/2 items-center justify-center text-fg-weaker opacity-40 transition-opacity group-hover:opacity-100"
            aria-label={open() ? "Replier les logs" : "Déplier les logs"}
            aria-expanded={open()}
            onClick={toggle}
            data-testid="run-toggle"
          >
            <IconChevronDown
              size={12}
              class={`transition-transform duration-150 ${open() ? "rotate-180" : ""}`}
            />
          </button>
        </div>
        <div class="th-collapse" data-open={open() ? "" : undefined}>
          <div>
            <Show when={mounted()}>
              <div
                classList={{
                  "px-4 pb-3 pt-1": !fullscreen(),
                  "fixed inset-0 z-[60] flex flex-col bg-bg": fullscreen(),
                }}
                data-testid={fullscreen() ? "fullscreen" : undefined}
              >
                <Show when={fullscreen()}>
                  <div class="shrink-0 border-b border-line-weak bg-bg-stronger">
                    <RunLine id={props.id} header />
                  </div>
                </Show>
                <LogTerminal
                  runId={props.id}
                  fullscreen={fullscreen()}
                  onToggleFullscreen={() => setFullscreenRun(fullscreen() ? null : props.id)}
                />
              </div>
            </Show>
          </div>
        </div>
      </div>
    </Show>
  )
}
