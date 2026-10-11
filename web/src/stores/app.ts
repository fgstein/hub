/**
 * Store principal : inventaire, runs indexés par id (mises à jour fines), état d'interface,
 * brouillons et connexion temps réel.
 */
import { batch, createRoot, createSignal } from "solid-js"
import { createStore, produce, reconcile } from "solid-js/store"
import { ApiError, api } from "~/lib/api"
import type {
  ConfigList,
  InventoryView,
  LastSelection,
  MachineStatus,
  ProjectView,
  RunAction,
  RunStatus,
  RunView,
  Selection,
  ServerEvent,
  UiState,
} from "~/lib/types"
import { isActive, isFinished } from "~/lib/types"
import { Socket } from "~/lib/ws"
import { setServerTime } from "./clock"
import { bindLogSocket, dispatchEnd, dispatchLogs, dispatchReset, resubscribeAll } from "./logs"
import { toast, toastError } from "./toasts"

export interface Draft {
  key: number
  machine: string | null
  image: string | null
  venv: string | null
  config: string | null
  cuda: string
  launching: boolean
}

interface AppState {
  ready: boolean
  inventory: InventoryView | null
  runs: Record<string, RunView>
  ui: UiState
  seq: number
  drafts: Record<string, Draft[]>
  /** Statut affiché pendant une action optimiste, par run. */
  pending: Record<string, RunStatus | undefined>
}

const [state, setState] = createStore<AppState>({
  ready: false,
  inventory: null,
  runs: {},
  ui: { open_tabs: [], active_tab: null, last_selection: {} },
  seq: 0,
  drafts: {},
  pending: {},
})

export { state }

// --------------------------------------------------------------------------- temps réel

function applyEvent(ev: ServerEvent): void {
  switch (ev.type) {
    case "snapshot": {
      setServerTime(ev.server_time)
      const runs: Record<string, RunView> = {}
      for (const r of ev.runs) runs[r.id] = r
      batch(() => {
        setState("inventory", reconcile(ev.inventory))
        setState("runs", reconcile(runs))
        setState("ui", reconcile(ev.ui_state))
        setState("seq", ev.seq)
        setState("ready", true)
      })
      return
    }
    case "run.created":
    case "run.updated":
      if (ev.seq <= state.seq) return
      setState("seq", ev.seq)
      if (state.runs[ev.run.id]) setState("runs", ev.run.id, reconcile(ev.run))
      else setState("runs", ev.run.id, ev.run)
      return
    case "run.deleted":
      if (ev.seq <= state.seq) return
      batch(() => {
        setState("seq", ev.seq)
        setState(
          "runs",
          produce((runs) => {
            delete runs[ev.id]
          }),
        )
      })
      return
    case "inventory.updated":
      setState("seq", Math.max(state.seq, ev.seq))
      setState("inventory", reconcile(ev.inventory))
      return
    case "machine.status":
      updateMachine(ev.machine)
      return
    case "ui_state.updated":
      // Les dernières sélections sont partagées ; l'onglet actif reste celui de ce client.
      setState("ui", "last_selection", reconcile(ev.ui_state.last_selection))
      return
    case "logs.reset":
      dispatchReset(ev.run_id)
      return
    case "logs.end":
      dispatchEnd(ev.run_id)
      return
    case "error":
      toast(ev.message, "error")
      return
    default:
      return
  }
}

function updateMachine(m: MachineStatus): void {
  const inv = state.inventory
  if (!inv) return
  const i = inv.machines.findIndex((x) => x.id === m.id)
  if (i >= 0) setState("inventory", "machines", i, "status", reconcile(m))
}

const socket = createRoot(() => {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return new Socket(`${proto}//${window.location.host}/ws`, {
    onEvent: applyEvent,
    onLogs: dispatchLogs,
    onOpen: () => resubscribeAll(),
  })
})
bindLogSocket((msg) => socket.send(msg))

export const connection = socket.status

export function connect(): void {
  socket.connect()
}

// --------------------------------------------------------------------------- sélecteurs

export function project(id: string | null | undefined): ProjectView | undefined {
  return state.inventory?.projects.find((p) => p.id === id)
}

/** Runs actifs d'un projet, du plus ancien au plus récent. */
export function activeRunIds(projectId: string): string[] {
  return Object.values(state.runs)
    .filter((r) => r.project_id === projectId && isActive(r.status))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map((r) => r.id)
}

/** Runs terminés d'un projet, du plus récent au plus ancien. */
export function finishedRunIds(projectId: string): string[] {
  return Object.values(state.runs)
    .filter((r) => r.project_id === projectId && isFinished(r.status))
    .sort((a, b) => (b.finished_at ?? b.created_at).localeCompare(a.finished_at ?? a.created_at))
    .map((r) => r.id)
}

export function activeCount(projectId: string): number {
  let n = 0
  for (const r of Object.values(state.runs)) if (r.project_id === projectId && isActive(r.status)) n++
  return n
}

/** Statut affiché (optimiste pendant une action en cours). */
export function displayStatus(id: string): RunStatus {
  return state.pending[id] ?? state.runs[id]?.status ?? "failed"
}

// --------------------------------------------------------------------------- état d'interface

let putTimer: ReturnType<typeof setTimeout> | undefined

function persistUi(): void {
  clearTimeout(putTimer)
  putTimer = setTimeout(() => {
    const ui = JSON.parse(JSON.stringify(state.ui)) as UiState
    api.putUiState(ui).catch(toastError)
  }, 300)
}

export function setActiveTab(id: string): void {
  batch(() => {
    if (!state.ui.open_tabs.includes(id)) setState("ui", "open_tabs", (t) => [...t, id])
    setState("ui", "active_tab", id)
  })
  persistUi()
}

export function closeTab(id: string): string | null {
  const tabs = state.ui.open_tabs
  const i = tabs.indexOf(id)
  if (i < 0) return state.ui.active_tab ?? null
  const remaining = tabs.filter((t) => t !== id)
  let active = state.ui.active_tab ?? null
  if (active === id) active = remaining[Math.min(i, remaining.length - 1)] ?? null
  batch(() => {
    setState("ui", "open_tabs", remaining)
    setState("ui", "active_tab", active)
  })
  persistUi()
  return active
}

export function moveTab(id: string, toIndex: number): void {
  const tabs = state.ui.open_tabs.filter((t) => t !== id)
  tabs.splice(Math.max(0, Math.min(toIndex, tabs.length)), 0, id)
  setState("ui", "open_tabs", tabs)
  persistUi()
}

// --------------------------------------------------------------------------- brouillons

let draftKey = 1

function defaultsFor(projectId: string): LastSelection {
  return state.ui.last_selection?.[projectId] ?? {}
}

export function addDraft(projectId: string): void {
  const p = project(projectId)
  if (!p) return
  const last = defaultsFor(projectId)
  const pick = (value: string | null | undefined, allowed: string[]) =>
    value && allowed.includes(value)
      ? value
      : allowed.length === 1
        ? (allowed[0] ?? null)
        : (allowed[0] ?? null)
  const machine = pick(last.machine, p.machines)
  const image = pick(last.image, p.images)
  const venvs = compatibleVenvs(p, image)
  const draft: Draft = {
    key: draftKey++,
    machine,
    image,
    venv: pick(last.venv, venvs),
    config: last.config ?? (p.static_configs?.length === 1 ? (p.static_configs[0] ?? null) : null),
    cuda: last.cuda_visible_devices ?? "",
    launching: false,
  }
  setState("drafts", projectId, (d) => [...(d ?? []), draft])
}

export function compatibleVenvs(p: ProjectView, image: string | null): string[] {
  return p.venvs.filter((v) => {
    const imgs = p.venv_images[v]
    return !imgs || !image || imgs.includes(image)
  })
}

export function updateDraft(projectId: string, key: number, patch: Partial<Draft>): void {
  setState("drafts", projectId, (d) => d?.key === key, patch)
  // un venv restreint à certaines images disparaît si l'image ne correspond plus
  if (patch.image !== undefined) {
    const p = project(projectId)
    const d = state.drafts[projectId]?.find((x) => x.key === key)
    if (p && d) {
      const venvs = compatibleVenvs(p, d.image)
      if (!d.venv || !venvs.includes(d.venv)) {
        setState("drafts", projectId, (x) => x.key === key, "venv", venvs[0] ?? null)
      }
    }
  }
}

export function removeDraft(projectId: string, key: number): void {
  setState("drafts", projectId, (d) => (d ?? []).filter((x) => x.key !== key))
}

export function draftSelection(projectId: string, d: Draft): Selection | null {
  if (!d.machine || !d.image || !d.venv || !d.config) return null
  return {
    project: projectId,
    machine: d.machine,
    image: d.image,
    venv: d.venv,
    config: d.config,
    overrides: [],
    cuda_visible_devices: d.cuda.trim() || null,
  }
}

export async function launchDraft(projectId: string, key: number): Promise<void> {
  const d = state.drafts[projectId]?.find((x) => x.key === key)
  if (!d || d.launching) return
  const sel = draftSelection(projectId, d)
  if (!sel) {
    toast("sélection incomplète : machine, image, venv et config sont requis", "error")
    return
  }
  updateDraft(projectId, key, { launching: true })
  try {
    const run = await api.launch(sel)
    batch(() => {
      if (!state.runs[run.id]) setState("runs", run.id, run)
      setState(
        "ui",
        "last_selection",
        projectId,
        reconcile({
          machine: sel.machine,
          image: sel.image,
          venv: sel.venv,
          config: sel.config,
          cuda_visible_devices: sel.cuda_visible_devices ?? null,
        }),
      )
      removeDraft(projectId, key)
    })
  } catch (e) {
    updateDraft(projectId, key, { launching: false })
    toastError(e)
  }
}

// --------------------------------------------------------------------------- actions

const OPTIMISTIC: Record<RunAction, RunStatus> = {
  pause: "paused",
  resume: "running",
  stop: "stopping",
  restart: "launching",
  relaunch: "launching",
}

/** Action optimiste : la pastille change tout de suite, confirmée ou annulée ensuite. */
export async function runAction(id: string, action: RunAction): Promise<void> {
  if (action !== "relaunch") setState("pending", id, OPTIMISTIC[action])
  try {
    const run = await api.action(id, action)
    if (state.runs[run.id]) setState("runs", run.id, reconcile(run))
    else setState("runs", run.id, run)
  } catch (e) {
    toastError(e)
  } finally {
    setState("pending", id, undefined)
  }
}

export async function deleteRun(id: string): Promise<void> {
  const prev = state.runs[id]
  setState(
    "runs",
    produce((runs) => {
      delete runs[id]
    }),
  )
  try {
    await api.remove(id)
  } catch (e) {
    if (prev && !(e instanceof ApiError && e.status === 404)) setState("runs", id, prev)
    toastError(e)
  }
}

// --------------------------------------------------------------------------- commande du projet

export async function saveCommand(projectId: string, command: string): Promise<boolean> {
  try {
    const view = await api.setCommand(projectId, command)
    replaceProject(view)
    return true
  } catch (e) {
    toastError(e)
    return false
  }
}

export async function resetCommand(projectId: string): Promise<ProjectView | null> {
  try {
    const view = await api.resetCommand(projectId)
    replaceProject(view)
    return view
  } catch (e) {
    toastError(e)
    return null
  }
}

function replaceProject(view: ProjectView): void {
  const i = state.inventory?.projects.findIndex((p) => p.id === view.id) ?? -1
  if (i >= 0) setState("inventory", "projects", i, reconcile(view))
}

// --------------------------------------------------------------------------- configs

const [configsCache, setConfigsCache] = createStore<
  Record<string, { loading: boolean; data?: ConfigList; error?: string }>
>({})

export { configsCache }

export const configKey = (projectId: string, machine: string | null) => `${projectId}@${machine ?? ""}`

export async function loadConfigs(projectId: string, machine: string | null, refresh = false) {
  const key = configKey(projectId, machine)
  const cur = configsCache[key]
  if (cur && (cur.loading || (cur.data && !refresh))) return
  setConfigsCache(key, { loading: true, data: cur?.data })
  try {
    const data = await api.configs(projectId, machine ?? undefined, refresh)
    setConfigsCache(key, { loading: false, data, error: data.discover_error ?? undefined })
  } catch (e) {
    setConfigsCache(key, { loading: false, error: e instanceof Error ? e.message : String(e) })
  }
}

// --------------------------------------------------------------------------- divers

export const [fullscreenRun, setFullscreenRun] = createSignal<string | null>(null)

export function machineReachable(id: string): boolean | null | undefined {
  return state.inventory?.machines.find((m) => m.id === id)?.status.reachable
}

/** Projets dont la commande en cours d'édition est invalide (lancement désactivé). */
const [commandErrors, setCommandErrors] = createStore<Record<string, boolean>>({})

export { commandErrors, setCommandErrors }
