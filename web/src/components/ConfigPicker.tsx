import { Popover } from "@kobalte/core/popover"
import { createEffect, createMemo, createSignal, For, on, Show } from "solid-js"
import {
  aggregate,
  ancestors,
  buildTree,
  filterTree,
  type RunCounts,
  type TreeNode,
  visibleRows,
} from "~/lib/configTree"
import { isActive } from "~/lib/types"
import { state } from "~/stores/app"
import { IconChevronDown, IconChevronRight, IconRefresh, IconSpinner } from "./Icons"

/** Runs par config d'un projet, calculés en direct depuis le store (tous statuts / actifs). */
function useRunCounts(projectId: () => string) {
  return createMemo(() => {
    const counts: Record<string, RunCounts> = {}
    for (const r of Object.values(state.runs)) {
      if (r.project_id !== projectId()) continue
      let c = counts[r.config]
      if (!c) {
        c = { total: 0, active: 0 }
        counts[r.config] = c
      }
      c.total++
      if (isActive(r.status)) c.active++
    }
    return counts
  })
}

function CountBadge(props: { counts: RunCounts; dim?: boolean }) {
  const title = () =>
    props.counts.total === 0
      ? "Aucun run lancé sur cette config"
      : `${props.counts.total} run${props.counts.total > 1 ? "s" : ""} lancé${props.counts.total > 1 ? "s" : ""}` +
        (props.counts.active ? ` dont ${props.counts.active} en cours` : "")
  return (
    <span
      class="ml-auto flex shrink-0 items-center gap-1 pl-3 font-mono text-xs tabular"
      title={title()}
      data-testid="config-count"
    >
      <Show when={props.counts.active > 0}>
        <span class="dot" style={{ "--dot-color": "var(--status-green)", width: "6px", height: "6px" }} />
      </Show>
      <span
        classList={{
          "text-fg-weaker": props.counts.total === 0,
          "text-fg-weak": props.counts.total > 0 && !!props.dim,
          "rounded bg-surface-active px-1 text-fg-strong": props.counts.total > 0 && !props.dim,
        }}
      >
        {props.counts.total}
      </span>
    </span>
  )
}

export function ConfigPicker(props: {
  projectId: string
  value: string | null
  configs: string[]
  root?: string | null
  loading?: boolean
  error?: string | null
  onChange: (v: string) => void
  onRefresh?: () => void
}) {
  const [open, setOpen] = createSignal(false)
  const [query, setQuery] = createSignal("")
  const [collapsed, setCollapsed] = createSignal<Set<string>>(new Set())
  const [highlight, setHighlight] = createSignal(0)
  let listEl: HTMLDivElement | undefined

  const counts = useRunCounts(() => props.projectId)
  const tree = createMemo(() => buildTree(props.configs))
  const filtered = createMemo(() => filterTree(tree(), query()))
  const rows = createMemo(() => visibleRows(filtered(), collapsed(), query().trim() !== ""))

  // À l'ouverture : réinitialise la recherche et se place sur la config sélectionnée.
  createEffect(
    on(open, (o) => {
      if (!o) return
      setQuery("")
      if (props.value) {
        const anc = new Set(ancestors(props.value))
        setCollapsed((c) => new Set([...c].filter((p) => !anc.has(p))))
      }
      queueMicrotask(() => {
        const i = rows().findIndex((r) => r.path === props.value && r.kind === "file")
        setHighlight(Math.max(0, i))
        scrollToHighlight()
      })
    }),
  )

  const scrollToHighlight = () =>
    listEl?.querySelector<HTMLElement>(`[data-row="${highlight()}"]`)?.scrollIntoView({ block: "nearest" })

  const toggleDir = (path: string, force?: boolean) =>
    setCollapsed((c) => {
      const next = new Set(c)
      const close = force ?? !next.has(path)
      if (close) next.add(path)
      else next.delete(path)
      return next
    })

  const choose = (n: TreeNode) => {
    if (n.kind === "dir") {
      toggleDir(n.path)
      return
    }
    props.onChange(n.path)
    setOpen(false)
  }

  const onKeyDown = (e: KeyboardEvent) => {
    // Les événements d'un portail Solid remontent vers le composant parent (le brouillon) :
    // Entrée/Échap ici ne doivent ni lancer le run ni fermer le brouillon.
    e.stopPropagation()
    const list = rows()
    const cur = list[highlight()]
    const move = (i: number) => {
      setHighlight(Math.max(0, Math.min(list.length - 1, i)))
      scrollToHighlight()
    }
    if (e.key === "ArrowDown") {
      e.preventDefault()
      move(highlight() + 1)
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      move(highlight() - 1)
    } else if (e.key === "Enter") {
      e.preventDefault()
      if (cur) choose(cur)
    } else if (e.key === "ArrowRight" && cur?.kind === "dir" && !query()) {
      e.preventDefault()
      toggleDir(cur.path, false)
    } else if (e.key === "ArrowLeft" && cur && !query()) {
      e.preventDefault()
      if (cur.kind === "dir" && !collapsed().has(cur.path)) toggleDir(cur.path, true)
      else {
        const parent = ancestors(cur.path).pop()
        const i = list.findIndex((r) => r.path === parent && r.kind === "dir")
        if (i >= 0) move(i)
      }
    }
  }

  const selectedCounts = () => (props.value ? counts()[props.value] : undefined)

  return (
    <Popover open={open()} onOpenChange={setOpen} placement="bottom-start" gutter={4}>
      <Popover.Trigger
        class="flex h-7 w-full min-w-0 items-center rounded border bg-input pl-2 text-left transition-colors hover:bg-input-hover focus-visible:border-line"
        classList={{ "border-line-weak": !props.error, "border-line-critical": !!props.error }}
        aria-label="config"
        title={props.value ?? "Choisir une config"}
        data-testid="field-config"
      >
        <span
          class="min-w-0 flex-1 truncate font-mono text-sm"
          classList={{ "text-fg-strong": !!props.value, "font-sans text-fg-weaker": !props.value }}
          dir="rtl"
        >
          <bdi>{props.value ?? "config"}</bdi>
        </span>
        <Show when={selectedCounts()?.total}>
          <span
            class="shrink-0 pl-1 font-mono text-xs text-fg-weak"
            title="runs déjà lancés sur cette config"
          >
            ×{selectedCounts()?.total}
          </span>
        </Show>
        <Show when={props.loading}>
          <IconSpinner size={11} class="ml-1 shrink-0 text-icon" />
        </Show>
        <span class="flex shrink-0 items-center px-1 text-icon">
          <IconChevronDown size={11} />
        </span>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          class="menu-content z-[70] flex w-[400px] max-w-[calc(100vw-2rem)] flex-col rounded-md bg-float shadow-float outline-none"
          onKeyDown={onKeyDown}
          data-testid="config-picker"
        >
          <div class="flex items-center gap-1 border-b border-line-weaker p-1">
            <input
              autofocus
              value={query()}
              onInput={(e) => {
                setQuery(e.currentTarget.value)
                // pendant une recherche, on se place sur le premier fichier correspondant
                setHighlight(
                  Math.max(
                    0,
                    rows().findIndex((r) => r.kind === "file"),
                  ),
                )
              }}
              placeholder="Rechercher une config…"
              class="h-7 min-w-0 flex-1 rounded bg-input px-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker"
              data-testid="config-search"
            />
            <Show when={props.onRefresh}>
              <button
                type="button"
                class="flex size-7 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
                title="Rafraîchir (relit l'arborescence sur la machine)"
                aria-label="Rafraîchir les configs"
                onClick={() => props.onRefresh?.()}
                data-testid="config-refresh"
              >
                <Show when={props.loading} fallback={<IconRefresh size={12} />}>
                  <IconSpinner size={12} />
                </Show>
              </button>
            </Show>
          </div>
          <Show when={props.root}>
            <div class="truncate px-3 pt-1.5 font-mono text-xxs text-fg-weak" title={props.root ?? ""}>
              {props.root}
            </div>
          </Show>
          <div ref={listEl} class="max-h-[380px] overflow-y-auto p-1" role="tree" aria-label="Configs">
            <For each={rows()}>
              {(n, i) => {
                const isDir = n.kind === "dir"
                const nodeCounts = () =>
                  isDir ? aggregate(n, counts()) : (counts()[n.path] ?? { total: 0, active: 0 })
                const expanded = () => query().trim() !== "" || !collapsed().has(n.path)
                return (
                  <div
                    role="treeitem"
                    tabIndex={-1}
                    aria-expanded={isDir ? expanded() : undefined}
                    aria-selected={n.path === props.value && !isDir}
                    data-row={i()}
                    data-testid="config-node"
                    data-kind={n.kind}
                    data-path={n.path}
                    class="flex h-7 cursor-default select-none items-center gap-1.5 rounded pr-2 text-sm"
                    classList={{
                      "bg-surface-active": highlight() === i(),
                      "text-fg-strong": !isDir,
                      "text-fg": isDir,
                    }}
                    style={{ "padding-left": `${8 + n.depth * 14}px` }}
                    onMouseMove={() => highlight() !== i() && setHighlight(i())}
                    onClick={() => choose(n)}
                  >
                    <span class="flex w-3 shrink-0 justify-center text-icon">
                      <Show when={isDir}>
                        <IconChevronRight
                          size={10}
                          class={`transition-transform duration-100 ${expanded() ? "rotate-90" : ""}`}
                        />
                      </Show>
                    </span>
                    <span
                      class="min-w-0 truncate font-mono"
                      classList={{ "font-medium": n.path === props.value }}
                    >
                      {n.name}
                      {isDir ? "/" : ""}
                    </span>
                    <Show when={n.path === props.value && !isDir}>
                      <span class="shrink-0 text-xs text-fg-interactive">✓</span>
                    </Show>
                    <CountBadge counts={nodeCounts()} dim={isDir} />
                  </div>
                )
              }}
            </For>
            <Show when={rows().length === 0}>
              <div class="px-2 py-2 text-sm text-fg-weak">
                {props.loading
                  ? "Lecture de l'arborescence…"
                  : props.error
                    ? props.error
                    : query()
                      ? "Aucune config ne correspond"
                      : "Aucune config trouvée"}
              </div>
            </Show>
          </div>
          <div class="flex items-center gap-3 border-t border-line-weaker px-3 py-1.5 text-xxs text-fg-weak">
            <span>
              {props.configs.length} config{props.configs.length > 1 ? "s" : ""}
            </span>
            <span class="ml-auto flex items-center gap-1">
              <span class="rounded bg-surface-active px-1 font-mono text-fg-strong">n</span> runs lancés
            </span>
            <span class="flex items-center gap-1">
              <span
                class="dot"
                style={{ "--dot-color": "var(--status-green)", width: "6px", height: "6px" }}
              />{" "}
              en cours
            </span>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover>
  )
}
