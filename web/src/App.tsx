import { Navigate, Route, Router, useNavigate, useParams } from "@solidjs/router"
import { createEffect, on, onCleanup, onMount, Show } from "solid-js"
import { ProjectPage } from "./components/ProjectPage"
import { StatusBar } from "./components/StatusBar"
import { TabBar } from "./components/TabBar"
import { Toaster } from "./components/Toaster"
import {
  addDraft,
  connect,
  fullscreenRun,
  project,
  setActiveTab,
  setFullscreenRun,
  state,
} from "./stores/app"
import { applyTheme } from "./stores/theme"

function isTyping(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null
  if (!t) return false
  return t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || !!t.closest(".xterm")
}

function Shell(props: { children?: unknown }) {
  const navigate = useNavigate()

  const onKey = (e: KeyboardEvent) => {
    // Escape belongs to the interactive shell (vim…) when it has focus.
    const inShell = !!(e.target as HTMLElement | null)?.closest?.('[data-testid="shell-terminal"]')
    if (e.key === "Escape" && fullscreenRun() && !inShell) {
      e.preventDefault()
      setFullscreenRun(null)
      return
    }
    if (e.ctrlKey && e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      const tabs = state.ui.open_tabs
      if (!tabs.length) return
      e.preventDefault()
      const i = Math.max(0, tabs.indexOf(state.ui.active_tab ?? ""))
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length]
      if (next) navigate(`/p/${encodeURIComponent(next)}`)
      return
    }
    if ((e.key === "n" || e.key === "N") && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping(e)) {
      const id = state.ui.active_tab
      if (id && project(id)) {
        e.preventDefault()
        addDraft(id)
      }
    }
  }
  onMount(() => window.addEventListener("keydown", onKey))
  onCleanup(() => window.removeEventListener("keydown", onKey))

  return (
    <div class="flex h-full flex-col">
      <TabBar />
      <main class="flex min-h-0 flex-1 flex-col">
        <Show
          when={state.ready}
          fallback={<div class="p-6 text-sm text-fg-weak">Connecting to the server…</div>}
        >
          {props.children as never}
        </Show>
      </main>
      <StatusBar />
      <Toaster />
    </div>
  )
}

function ProjectRoute() {
  const params = useParams<{ id: string }>()
  const navigate = useNavigate()
  createEffect(
    on(
      () => [params.id, state.ready] as const,
      ([id, ready]) => {
        if (!ready) return
        if (project(id)) {
          if (state.ui.active_tab !== id || !state.ui.open_tabs.includes(id)) setActiveTab(id)
        } else {
          navigate("/", { replace: true })
        }
      },
    ),
  )
  return (
    <Show when={project(params.id)} keyed>
      {(p) => <ProjectPage project={p} />}
    </Show>
  )
}

function Home() {
  return (
    <Show
      when={state.ui.active_tab && project(state.ui.active_tab)}
      fallback={
        <div
          class="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-fg-weak"
          data-testid="no-tab"
        >
          <p>No project open.</p>
          <p class="text-xs">Use “+” in the tab bar to open one.</p>
        </div>
      }
    >
      <Navigate href={`/p/${encodeURIComponent(state.ui.active_tab!)}`} />
    </Show>
  )
}

export function App() {
  onMount(() => connect())
  createEffect(applyTheme)
  return (
    <Router root={Shell}>
      <Route path="/p/:id" component={ProjectRoute} />
      <Route path="*" component={Home} />
    </Router>
  )
}
