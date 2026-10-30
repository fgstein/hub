import { Tooltip } from "@kobalte/core/tooltip"
import { createEffect, createSignal, type JSX, on, onCleanup, Show } from "solid-js"
import { prettyCommand, shellJoin } from "~/lib/command"
import { elapsedSeconds, formatDateTime, formatDuration, pausedSeconds } from "~/lib/format"
import { isActive, isFinished, type RunView, shellAvailable } from "~/lib/types"
import {
  displayStatus,
  fullscreenRun,
  openShell,
  openTerminals,
  panelViews,
  setFullscreenRun,
  setPanelView,
  setTerminalOpen,
  state,
} from "~/stores/app"
import { now } from "~/stores/clock"
import { toast } from "~/stores/toasts"
import { ActionButton } from "./ActionButton"
import { IconAgent, IconChevronDown, IconCopy, IconTerminal } from "./Icons"
import { LogTerminal } from "./LogTerminal"
import { ShellTerminal } from "./ShellTerminal"
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

/** Left + middle + right part of a run (reused in the fullscreen header). */
function RunLine(props: { id: string; open?: boolean; onToggle?: () => void; header?: boolean }) {
  const run = () => state.runs[props.id]!
  const status = () => displayStatus(props.id)
  const elapsed = () =>
    run().started_at ? formatDuration(elapsedSeconds(run(), isActive(run().status) ? now() : 0)) : "—"
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
            <div>started: {formatDateTime(run().started_at)}</div>
            <Show when={run().finished_at}>
              <div>finished: {formatDateTime(run().finished_at)}</div>
            </Show>
            <Show when={paused() > 0}>
              <div>of which paused: {formatDuration(paused())}</div>
            </Show>
            <Show when={run().exit_code != null}>
              <div>exit code: {run().exit_code}</div>
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
        <Show when={run().checkpoint}>
          <Tip
            class="min-w-0 shrink truncate rounded bg-surface px-1 text-xs text-fg-weak"
            content={<span class="font-mono">{run().checkpoint}</span>}
          >
            <span data-testid="run-checkpoint">ckpt {run().checkpoint?.split("/").slice(-2).join("/")}</span>
          </Tip>
        </Show>
        <Show when={run().cuda_visible_devices}>
          <Tip class="shrink-0 rounded bg-surface px-1 text-xs text-fg-weak" content="CUDA_VISIBLE_DEVICES">
            gpu {run().cuda_visible_devices}
          </Tip>
        </Show>
      </div>
      <Show when={run().container_alive && isFinished(run().status)}>
        <Tip
          class="shrink-0"
          content="The command has finished but the container is kept: shell available. “Close container” in the menu stops it."
        >
          <button
            type="button"
            class="flex h-5 items-center gap-1 rounded bg-surface px-1.5 text-xs text-fg-weak hover:bg-surface-hover hover:text-fg-strong"
            onClick={(e) => {
              e.stopPropagation()
              openShell(props.id)
            }}
            data-testid="run-container-alive"
          >
            <IconTerminal size={11} /> container alive
          </button>
        </Tip>
      </Show>
      <Show when={run().source === "mcp"}>
        <Tip class="shrink-0 text-icon" content="Launched by an agent (MCP)">
          <IconAgent size={14} />
        </Tip>
      </Show>
      <Show when={run().source === "import"}>
        <Tip class="shrink-0 text-xs text-fg-weak" content="Container found on the machine and imported">
          imported
        </Tip>
      </Show>
      <ActionButton run={run()} />
    </div>
  )
}

/** Command of the run: resolved generic command, or full command (ssh + docker run). */
function CommandBar(props: { run: RunView }) {
  const [full, setFull] = createSignal(false)
  const fullText = () =>
    props.run.full_command?.length ? shellJoin(props.run.full_command) : props.run.command
  const text = () =>
    full() && props.run.full_command?.length ? prettyCommand(props.run.full_command) : props.run.command
  const copy = async () => {
    try {
      // Exact copy (one line); the multi-line display only escapes one level.
      await navigator.clipboard.writeText(full() ? fullText() : props.run.command)
      toast(full() ? "full command copied" : "command copied", "success")
    } catch {
      toast("could not copy", "error")
    }
  }
  const tab = (active: boolean) =>
    `h-5 rounded px-1.5 ${active ? "bg-surface-active text-fg-strong" : "text-fg-weak hover:text-fg-strong"}`
  return (
    <div
      class="flex min-w-0 items-start gap-2 rounded-md border border-line-weaker bg-bg-strong px-2 py-1 text-xs"
      data-testid="run-command"
      onClick={(e) => e.stopPropagation()}
    >
      <div class="flex shrink-0 items-center gap-0.5">
        <button
          type="button"
          class={tab(!full())}
          onClick={() => setFull(false)}
          title="Resolved generic command"
        >
          command
        </button>
        <button
          type="button"
          class={tab(full())}
          onClick={() => setFull(true)}
          title="Exact command run by TrainHub (ssh + docker run + script), including the project's Docker options (YAML inventory)"
          data-testid="run-command-full"
        >
          full
        </button>
      </div>
      <code
        class="min-w-0 flex-1 select-text py-0.5 font-mono text-fg-strong"
        classList={{ truncate: !full(), "max-h-60 overflow-y-auto whitespace-pre-wrap break-words": full() }}
        title={full() ? undefined : props.run.command}
        data-testid="run-command-text"
      >
        {text()}
      </code>
      <button
        type="button"
        class="flex size-5 shrink-0 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
        title="Copy"
        aria-label="Copy command"
        onClick={copy}
      >
        <IconCopy size={11} />
      </button>
    </div>
  )
}

export function RunRow(props: { id: string }) {
  const run = () => state.runs[props.id]
  const open = () => openTerminals[props.id] === true
  const setOpen = (fn: (o: boolean) => boolean) => setTerminalOpen(props.id, fn(open()))
  const [mounted, setMounted] = createSignal(open())
  let unmountTimer: ReturnType<typeof setTimeout> | undefined
  const fullscreen = () => fullscreenRun() === props.id

  createEffect(
    on(open, (o) => {
      clearTimeout(unmountTimer)
      if (o) setMounted(true)
      // Collapsing a terminal unsubscribes from the stream after 10 s.
      else unmountTimer = setTimeout(() => setMounted(false), UNSUBSCRIBE_DELAY_MS)
    }),
  )
  onCleanup(() => clearTimeout(unmountTimer))

  const toggle = () => setOpen((o) => !o)
  const view = () => panelViews[props.id] ?? "logs"
  const canShell = () =>
    shellAvailable({ status: displayStatus(props.id), container_alive: run()?.container_alive ?? false })
  // The shell stays mounted (session kept) when going back to the logs.
  const [shellMounted, setShellMounted] = createSignal(false)
  createEffect(() => {
    if (view() === "shell" && mounted()) setShellMounted(true)
    if (!mounted()) setShellMounted(false)
  })
  const tabCls = (active: boolean) =>
    `flex h-6 items-center gap-1 rounded px-2 ${active ? "bg-surface-active text-fg-strong" : "text-fg-weak hover:text-fg-strong"}`

  return (
    <Show when={run()}>
      <div
        class="anim-in border-b border-line-weaker"
        data-testid="run-row"
        data-run-id={props.id}
        data-status={displayStatus(props.id)}
      >
        <div class="group">
          <div class="relative">
            <RunLine id={props.id} onToggle={toggle} />
            <button
              type="button"
              class="absolute bottom-0 left-1/2 flex h-3 w-10 -translate-x-1/2 items-center justify-center text-fg-weaker opacity-40 transition-opacity group-hover:opacity-100"
              aria-label={open() ? "Collapse logs" : "Expand logs"}
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
          <Show when={(run()!.status === "unreachable" || run()!.status === "failed") && run()!.error}>
            <div
              class="-mt-1 truncate px-4 pb-2 pl-[100px] text-xs text-fg-critical"
              data-testid="run-error"
              title={run()!.error ?? ""}
            >
              {run()!.error}
            </div>
          </Show>
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
                <div
                  class="flex shrink-0 flex-col gap-1.5"
                  classList={{ "mb-1.5": !fullscreen(), "px-4 py-1.5": fullscreen() }}
                >
                  <CommandBar run={run()!} />
                  <div class="flex items-center gap-0.5 text-xs" onClick={(e) => e.stopPropagation()}>
                    <button
                      type="button"
                      class={tabCls(view() === "logs")}
                      onClick={() => setPanelView(props.id, "logs")}
                      data-testid="panel-logs"
                    >
                      logs
                    </button>
                    <button
                      type="button"
                      class={`${tabCls(view() === "shell")} disabled:opacity-40`}
                      disabled={!canShell() && !shellMounted()}
                      title={
                        canShell()
                          ? "Interactive shell in the container (docker exec -it), venv activated"
                          : "Container stopped (or paused): no shell"
                      }
                      onClick={() => setPanelView(props.id, "shell")}
                      data-testid="panel-shell"
                    >
                      <IconTerminal size={11} /> shell
                    </button>
                  </div>
                </div>
                <div classList={{ hidden: view() !== "logs", contents: view() === "logs" }}>
                  <LogTerminal
                    runId={props.id}
                    fullscreen={fullscreen()}
                    onToggleFullscreen={() => setFullscreenRun(fullscreen() ? null : props.id)}
                  />
                </div>
                <Show when={shellMounted()}>
                  <div classList={{ hidden: view() !== "shell", contents: view() === "shell" }}>
                    <ShellTerminal
                      runId={props.id}
                      containerName={run()!.container_name}
                      active={view() === "shell"}
                      fullscreen={fullscreen()}
                      onToggleFullscreen={() => setFullscreenRun(fullscreen() ? null : props.id)}
                    />
                  </div>
                </Show>
              </div>
            </Show>
          </div>
        </div>
      </div>
    </Show>
  )
}
