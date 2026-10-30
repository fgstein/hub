import { Popover } from "@kobalte/core/popover"
import { useLocation, useNavigate } from "@solidjs/router"
import { createMemo, createSignal, For, Show } from "solid-js"
import { formatBytes } from "~/lib/format"
import { activeCount, closeTab, moveTab, project, setActiveTab, state } from "~/stores/app"
import { cycleTheme, themePref } from "~/stores/theme"
import { IconClose, IconDatabase, IconMonitor, IconMoon, IconPlus, IconSun } from "./Icons"

export function TabBar() {
  const navigate = useNavigate()
  const location = useLocation()
  const onCheckpoints = () => location.pathname.startsWith("/checkpoints")
  const [dragging, setDragging] = createSignal<string | null>(null)
  const [dropIndex, setDropIndex] = createSignal<number | null>(null)

  const go = (id: string | null) => navigate(id ? `/p/${encodeURIComponent(id)}` : "/")

  const onClose = (e: MouseEvent, id: string) => {
    e.stopPropagation()
    const wasActive = state.ui.active_tab === id
    const next = closeTab(id)
    if (wasActive) go(next)
  }

  return (
    <nav
      class="flex h-10 shrink-0 items-stretch border-b border-line-weak bg-bg-stronger"
      aria-label="Projects"
      data-testid="tabbar"
    >
      <div class="flex min-w-0 items-stretch overflow-x-auto [scrollbar-width:none]" role="tablist">
        <Show when={state.inventory?.checkpoint_sources?.length}>
          <CheckpointsTab active={onCheckpoints()} onClick={() => navigate("/checkpoints")} />
        </Show>
        <For each={state.ui.open_tabs}>
          {(id, index) => {
            const p = () => project(id)
            const active = () => !onCheckpoints() && state.ui.active_tab === id
            const count = () => activeCount(id)
            return (
              <div
                role="tab"
                tabIndex={active() ? 0 : -1}
                aria-selected={active()}
                data-testid={`tab-${id}`}
                draggable="true"
                onDragStart={(e) => {
                  setDragging(id)
                  e.dataTransfer?.setData("text/plain", id)
                  if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"
                }}
                onDragOver={(e) => {
                  if (!dragging()) return
                  e.preventDefault()
                  const rect = e.currentTarget.getBoundingClientRect()
                  setDropIndex(e.clientX < rect.left + rect.width / 2 ? index() : index() + 1)
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  const d = dragging()
                  const to = dropIndex()
                  if (d && to !== null) {
                    const from = state.ui.open_tabs.indexOf(d)
                    moveTab(d, to > from ? to - 1 : to)
                  }
                  setDragging(null)
                  setDropIndex(null)
                }}
                onDragEnd={() => {
                  setDragging(null)
                  setDropIndex(null)
                }}
                onClick={() => go(id)}
                onAuxClick={(e) => e.button === 1 && onClose(e, id)}
                onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && go(id)}
                class="group relative flex max-w-[240px] shrink-0 cursor-default select-none items-center gap-2 border-r border-line-weak pl-3 pr-2 text-sm transition-colors"
                classList={{
                  "bg-bg text-fg-strong": active(),
                  "text-fg hover:text-fg-strong": !active(),
                  "opacity-50": dragging() === id,
                }}
              >
                <Show when={dropIndex() === index() && dragging() && dragging() !== id}>
                  <span class="absolute inset-y-1 left-0 w-0.5 rounded bg-line-selected" />
                </Show>
                <Show when={dropIndex() === index() + 1 && dragging() && dragging() !== id}>
                  <span class="absolute inset-y-1 right-0 w-0.5 rounded bg-line-selected" />
                </Show>
                <Show when={active()}>
                  <span class="absolute inset-x-0 bottom-[-1px] h-px bg-bg" />
                  <span class="absolute inset-x-0 top-0 h-px bg-fg-strong opacity-60" />
                </Show>
                <span class="truncate font-medium">{p()?.label ?? id}</span>
                <Show when={count() > 0}>
                  <span class="tabular text-xs text-fg-weak" data-testid="tab-count">
                    · {count()}
                  </span>
                </Show>
                <button
                  type="button"
                  aria-label={`Close ${p()?.label ?? id}`}
                  class="flex size-4 items-center justify-center rounded text-icon opacity-0 hover:bg-surface-hover hover:text-icon-hover focus-visible:opacity-100 group-hover:opacity-100"
                  classList={{ "opacity-60": active() }}
                  onClick={(e) => onClose(e, id)}
                >
                  <IconClose size={10} />
                </button>
              </div>
            )
          }}
        </For>
      </div>
      <AddTabMenu onPick={(id) => go(id)} />
      <div class="flex-1" />
      <ThemeButton />
    </nav>
  )
}

/** Pinned tab (not a project): runs already executed and their checkpoints. */
function CheckpointsTab(props: { active: boolean; onClick: () => void }) {
  const total = () => {
    const srcs = state.inventory?.checkpoint_sources ?? []
    if (!srcs.some((s) => s.size != null)) return null
    return srcs.reduce((a, s) => a + (s.size ?? 0), 0)
  }
  return (
    <div
      role="tab"
      tabIndex={props.active ? 0 : -1}
      aria-selected={props.active}
      data-testid="tab-checkpoints"
      onClick={() => props.onClick()}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && props.onClick()}
      class="relative flex shrink-0 cursor-default select-none items-center gap-2 border-r border-line-weak px-3 text-sm transition-colors"
      classList={{
        "bg-bg text-fg-strong": props.active,
        "text-fg hover:text-fg-strong": !props.active,
      }}
      title="Runs already executed and their checkpoints"
    >
      <Show when={props.active}>
        <span class="absolute inset-x-0 bottom-[-1px] h-px bg-bg" />
        <span class="absolute inset-x-0 top-0 h-px bg-fg-strong opacity-60" />
      </Show>
      <IconDatabase size={13} class="shrink-0 text-icon" />
      <span class="font-medium">Checkpoints</span>
      <Show when={total() != null}>
        <span class="tabular font-mono text-xs text-fg-weak">· {formatBytes(total())}</span>
      </Show>
    </div>
  )
}

function AddTabMenu(props: { onPick: (id: string) => void }) {
  const [open, setOpen] = createSignal(false)
  const [query, setQuery] = createSignal("")
  const [highlight, setHighlight] = createSignal(0)
  const candidates = createMemo(() => {
    const q = query().trim().toLowerCase()
    return (state.inventory?.projects ?? []).filter(
      (p) =>
        !state.ui.open_tabs.includes(p.id) &&
        (!q || p.id.toLowerCase().includes(q) || p.label.toLowerCase().includes(q)),
    )
  })
  const pick = (id: string) => {
    setActiveTab(id)
    props.onPick(id)
    setOpen(false)
    setQuery("")
  }
  return (
    <Popover
      open={open()}
      onOpenChange={(o) => {
        setOpen(o)
        setHighlight(0)
      }}
      placement="bottom-start"
      gutter={4}
    >
      <Popover.Trigger
        class="flex w-9 shrink-0 items-center justify-center text-icon hover:bg-surface-hover hover:text-icon-hover"
        aria-label="Open a project"
        data-testid="tab-add"
      >
        <IconPlus />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content class="menu-content z-[70] w-64 rounded-md bg-float p-1 shadow-float outline-none">
          <input
            autofocus
            value={query()}
            onInput={(e) => {
              setQuery(e.currentTarget.value)
              setHighlight(0)
            }}
            onKeyDown={(e) => {
              const list = candidates()
              if (e.key === "ArrowDown") {
                e.preventDefault()
                setHighlight((h) => Math.min(h + 1, list.length - 1))
              } else if (e.key === "ArrowUp") {
                e.preventDefault()
                setHighlight((h) => Math.max(h - 1, 0))
              } else if (e.key === "Enter") {
                const p = list[highlight()]
                if (p) pick(p.id)
              }
            }}
            placeholder="Search projects…"
            class="mb-1 h-7 w-full rounded bg-input px-2 text-sm text-fg-strong outline-none placeholder:text-fg-weaker"
          />
          <Show
            when={candidates().length}
            fallback={
              <div class="px-2 py-1.5 text-sm text-fg-weak">
                {(state.inventory?.projects.length ?? 0) === state.ui.open_tabs.length
                  ? "All projects are open"
                  : "No projects"}
              </div>
            }
          >
            <For each={candidates()}>
              {(p, i) => (
                <button
                  type="button"
                  class="flex h-7 w-full items-center gap-2 rounded px-2 text-left text-sm text-fg-strong"
                  classList={{ "bg-surface-active": highlight() === i() }}
                  onMouseEnter={() => setHighlight(i())}
                  onClick={() => pick(p.id)}
                  data-testid={`add-project-${p.id}`}
                >
                  <span class="truncate">{p.label}</span>
                  <span class="ml-auto font-mono text-xs text-fg-weak">{p.id}</span>
                </button>
              )}
            </For>
          </Show>
        </Popover.Content>
      </Popover.Portal>
    </Popover>
  )
}

function ThemeButton() {
  const label = () =>
    themePref() === "system" ? "Theme: system" : themePref() === "dark" ? "Theme: dark" : "Theme: light"
  return (
    <button
      type="button"
      class="flex w-9 items-center justify-center text-icon hover:text-icon-hover"
      onClick={cycleTheme}
      title={label()}
      aria-label={label()}
      data-testid="theme-toggle"
    >
      <Show when={themePref() === "system"}>
        <IconMonitor />
      </Show>
      <Show when={themePref() === "dark"}>
        <IconMoon />
      </Show>
      <Show when={themePref() === "light"}>
        <IconSun />
      </Show>
    </button>
  )
}
