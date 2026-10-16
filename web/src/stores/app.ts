/**
 * Main store: inventory, runs indexed by id (fine-grained updates), UI state,
 * drafts and real-time connection.
 */
import { batch, createRoot, createSignal } from "solid-js"
import { createStore, produce, reconcile } from "solid-js/store"
import { ApiError, api } from "~/lib/api"
import { shellJoin, splitArgs } from "~/lib/command"
import type {
  ConfigList,
  InventoryView,
  LastSelection,
  MachineGpus,
  MachineLoad,
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
  /** Run-specific extra arguments (overrides) (argument line, split as a shell would). */
  overrides: string
  /** Values of the free command parameters (``{name}``), by name. */
  params: Record<string, string>
  launching: boolean
  /** Draft created by “Relaunch”: highlighted and scrolled into view. */
  focus?: boolean
  /** Machine already chosen (Machines tab): do not open the machine list. */
  noAutofocus?: boolean
}

interface AppState {
  ready: boolean
  inventory: InventoryView | null
  runs: Record<string, RunView>
  ui: UiState
  seq: number
  /** TrainHub version of the server (from the snapshot). */
  version: string | null
  drafts: Record<string, Draft[]>
  /** Status shown during an optimistic action, per run. */
  pending: Record<string, RunStatus | undefined>
}

const [state, setState] = createStore<AppState>({
  ready: false,
  inventory: null,
  runs: {},
  ui: { open_tabs: [], active_tab: null, last_selection: {} },
  seq: 0,
  version: null,
  drafts: {},
  pending: {},
})

export { state }

// --------------------------------------------------------------------------- real time

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
        setState("version", ev.version)
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
      // Last selections are shared; the active tab stays this client's own.
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

// --------------------------------------------------------------------------- selectors

export function project(id: string | null | undefined): ProjectView | undefined {
  return state.inventory?.projects.find((p) => p.id === id)
}

/** Active runs of a project, oldest first. */
export function activeRunIds(projectId: string): string[] {
  return Object.values(state.runs)
    .filter((r) => r.project_id === projectId && isActive(r.status))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map((r) => r.id)
}

/** Finished runs of a project, most recent first. */
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

/** Displayed status (optimistic during an ongoing action). */
export function displayStatus(id: string): RunStatus {
  return state.pending[id] ?? state.runs[id]?.status ?? "failed"
}

// --------------------------------------------------------------------------- UI state

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

// --------------------------------------------------------------------------- drafts

let draftKey = 1

function defaultsFor(projectId: string): LastSelection {
  return state.ui.last_selection?.[projectId] ?? {}
}

export function addDraft(projectId: string, init: Partial<Draft> = {}): void {
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
    overrides: "",
    params: { ...(last.params ?? {}) },
    launching: false,
    ...init,
  }
  setState("drafts", projectId, (d) => [...(d ?? []), draft])
}

/** New draft on a machine and one of its GPUs (Machines tab), in an open, active tab. */
export function draftOnGpu(projectId: string, machine: string, gpu: number): void {
  const p = project(projectId)
  if (!p) return
  setActiveTab(projectId)
  addDraft(projectId, {
    machine: p.machines.includes(machine) ? machine : null,
    cuda: String(gpu),
    focus: true,
    noAutofocus: true,
  })
}

/** “Relaunch”: new draft with the same choices as the run (nothing is launched). */
export function draftFromRun(runId: string): void {
  const r = state.runs[runId]
  if (!r) return
  const p = project(r.project_id)
  if (!p) {
    toast(`project “${r.project_id}” missing from the inventory`, "error")
    return
  }
  const keep = (v: string, allowed: string[]) => (allowed.includes(v) ? v : null)
  const machine = keep(r.machine_id, p.machines)
  const image = keep(r.image_id, p.images)
  const venv = keep(r.venv_id, compatibleVenvs(p, image))
  addDraft(r.project_id, {
    machine,
    image,
    venv,
    config: r.config,
    cuda: r.cuda_visible_devices ?? "",
    overrides: shellJoin(r.overrides ?? []),
    params: { ...(state.ui.last_selection?.[r.project_id]?.params ?? {}), ...(r.params ?? {}) },
    focus: true,
  })
  if (state.ui.active_tab !== r.project_id) setActiveTab(r.project_id)
  const missing = [
    !machine && `machine ${r.machine_id}`,
    !image && `image ${r.image_id}`,
    !venv && `venv ${r.venv_id}`,
  ].filter(Boolean)
  if (missing.length) toast(`no longer available for this project: ${missing.join(", ")}`, "error")
}

export function compatibleVenvs(p: ProjectView, image: string | null): string[] {
  return p.venvs.filter((v) => {
    const imgs = p.venv_images[v]
    return !imgs || !image || imgs.includes(image)
  })
}

export function updateDraft(projectId: string, key: number, patch: Partial<Draft>): void {
  setState("drafts", projectId, (d) => d?.key === key, patch)
  // a venv restricted to some images disappears if the image no longer matches
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

/** Project command parameters whose draft value is invalid. */
export function draftParamErrors(projectId: string, d: Draft): Record<string, string> {
  const out: Record<string, string> = {}
  for (const k of project(projectId)?.params ?? []) {
    const err = splitArgs(d.params[k] ?? "").error
    if (err) out[k] = err
  }
  return out
}

export function draftSelection(projectId: string, d: Draft): Selection | null {
  if (!d.machine || !d.image || !d.venv || !d.config) return null
  const { args, error } = splitArgs(d.overrides)
  if (error || Object.keys(draftParamErrors(projectId, d)).length) return null
  const names = project(projectId)?.params ?? []
  return {
    project: projectId,
    machine: d.machine,
    image: d.image,
    venv: d.venv,
    config: d.config,
    overrides: args,
    cuda_visible_devices: d.cuda.trim() || null,
    params: Object.fromEntries(names.map((k) => [k, d.params[k] ?? ""])),
  }
}

export async function launchDraft(projectId: string, key: number): Promise<void> {
  const d = state.drafts[projectId]?.find((x) => x.key === key)
  if (!d || d.launching) return
  const sel = draftSelection(projectId, d)
  if (!sel) {
    const err = splitArgs(d.overrides).error
    const paramErr = Object.entries(draftParamErrors(projectId, d))[0]
    toast(
      err
        ? `invalid overrides: ${err}`
        : paramErr
          ? `invalid parameter {${paramErr[0]}}: ${paramErr[1]}`
          : "incomplete selection: machine, image, venv and config are required",
      "error",
    )
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
          params: { ...(state.ui.last_selection?.[projectId]?.params ?? {}), ...(sel.params ?? {}) },
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
  close: "succeeded",
}

/** Optimistic action: the status dot changes right away, then is confirmed or reverted. */
export async function runAction(id: string, action: RunAction): Promise<void> {
  if (action !== "relaunch" && action !== "close") setState("pending", id, OPTIMISTIC[action])
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

// --------------------------------------------------------------------------- project command

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

// --------------------------------------------------------------------------- GPUs

/** GPU state per machine (``nvidia-smi``), loaded on demand. */
const [gpusCache, setGpusCache] = createStore<
  Record<string, { loading: boolean; data?: MachineGpus; error?: string; at?: number }>
>({})

export { gpusCache }

/** Reload the GPUs of a machine unless they are being loaded or are fresh (``maxAgeMs``). */
export async function loadGpus(machine: string, maxAgeMs = 5000) {
  const cur = gpusCache[machine]
  if (cur?.loading || (cur?.at && Date.now() - cur.at < maxAgeMs)) return
  setGpusCache(machine, { loading: true, data: cur?.data, error: undefined })
  try {
    const data = await api.gpus(machine)
    setGpusCache(machine, { loading: false, data, error: data.error ?? undefined, at: Date.now() })
  } catch (e) {
    setGpusCache(machine, {
      loading: false,
      data: undefined,
      error: e instanceof Error ? e.message : String(e),
      at: Date.now(),
    })
  }
}

// --------------------------------------------------------------------------- machine load

/** CPU/GPU load per machine (``/proc`` + ``nvidia-smi``), refreshed by the Machines tab. */
const [loadCache, setLoadCache] = createStore<
  Record<string, { loading: boolean; data?: MachineLoad; error?: string; at?: number }>
>({})

export { loadCache }

/** Reload the load of a machine unless it is being loaded (one request at a time). */
export async function loadMachine(machine: string) {
  const cur = loadCache[machine]
  if (cur?.loading) return
  setLoadCache(machine, { ...cur, loading: true })
  try {
    const data = await api.load(machine)
    setLoadCache(machine, { loading: false, data, error: undefined, at: Date.now() })
  } catch (e) {
    setLoadCache(machine, {
      loading: false,
      error: e instanceof Error ? e.message : String(e),
      at: Date.now(),
    })
  }
}

// --------------------------------------------------------------------------- misc

export const [fullscreenRun, setFullscreenRun] = createSignal<string | null>(null)

export function machineReachable(id: string): boolean | null | undefined {
  return state.inventory?.machines.find((m) => m.id === id)?.status.reachable
}

/** Projects whose command being edited is invalid (launch disabled). */
const [commandErrors, setCommandErrors] = createStore<Record<string, boolean>>({})

export { commandErrors, setCommandErrors }

/** Expanded terminals, per run (kept when a run moves to “Finished”). */
const [openTerminals, setOpenTerminals] = createStore<Record<string, boolean>>({})

export { openTerminals }
export function setTerminalOpen(id: string, open: boolean): void {
  setOpenTerminals(id, open)
}

export type PanelView = "logs" | "shell"

/** View shown in a run's expanded panel (logs by default). */
const [panelViews, setPanelViews] = createStore<Record<string, PanelView>>({})

export { panelViews }
export function setPanelView(id: string, view: PanelView): void {
  setPanelViews(id, view)
}

/** Expand the run and show its shell. */
export function openShell(id: string): void {
  batch(() => {
    setPanelViews(id, "shell")
    setOpenTerminals(id, true)
  })
}
