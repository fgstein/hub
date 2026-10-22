import type { components } from "./api-types"

type S = components["schemas"]

export type RunView = S["RunView"]
export type RunStatus = S["RunStatus"]
export type InventoryView = S["InventoryView"]
export type ProjectView = S["ProjectView"]
export type MachineView = S["MachineView"]
export type MachineStatus = S["MachineStatus"]
/** UI state (fields always present in server responses). */
export type UiState = Required<S["UiState"]>
export type LastSelection = S["LastSelection"]
export type Selection = S["Selection"]
export type PreviewResult = S["PreviewResult"]
export type ConfigList = S["ConfigList"]
export type Health = S["Health"]
export type MachineGpus = S["MachineGpus"]
export type GpuView = S["GpuView"]
export type MlflowInfo = S["MlflowInfo"]
export type MachineLoad = S["MachineLoad"]
export type CpuView = S["CpuView"]
export type Series = S["Series"]
export type SeriesRequest = S["SeriesRequest"]

export type RunAction = "pause" | "resume" | "stop" | "restart" | "relaunch" | "close"
export type MenuAction = RunAction | "delete"

export interface Snapshot {
  type: "snapshot"
  seq: number
  inventory: InventoryView
  runs: RunView[]
  ui_state: UiState
  server_time: string
  version: string
}

export type ServerEvent =
  | Snapshot
  | { type: "run.created" | "run.updated"; seq: number; run: RunView }
  | { type: "run.deleted"; seq: number; id: string; project_id: string }
  | { type: "inventory.updated"; seq: number; inventory: InventoryView }
  | { type: "machine.status"; seq: number; machine: MachineStatus }
  | { type: "ui_state.updated"; seq: number; ui_state: UiState }
  | { type: "logs.reset" | "logs.end"; run_id: string }
  | { type: "pong"; t?: number }
  | { type: "error"; message: string; run_id?: string }

export const ACTIVE_STATUSES: readonly RunStatus[] = [
  "launching",
  "running",
  "paused",
  "stopping",
  "unreachable",
]
export const FINISHED_STATUSES: readonly RunStatus[] = ["succeeded", "failed", "stopped"]

export const isActive = (s: RunStatus) => ACTIVE_STATUSES.includes(s)
export const isFinished = (s: RunStatus) => FINISHED_STATUSES.includes(s)

/** Allowed starting statuses for each action (section 4.2 of the spec). */
export const ALLOWED_FROM: Record<MenuAction, readonly RunStatus[]> = {
  pause: ["running"],
  resume: ["paused"],
  stop: ["running", "paused"],
  restart: FINISHED_STATUSES,
  relaunch: FINISHED_STATUSES,
  close: FINISHED_STATUSES,
  delete: FINISHED_STATUSES,
}

/** Possible actions; ``close`` requires a kept container that is still started. */
export function allowedActions(status: RunStatus, containerAlive = false): MenuAction[] {
  return (Object.keys(ALLOWED_FROM) as MenuAction[]).filter(
    (a) => ALLOWED_FROM[a].includes(status) && (a !== "close" || containerAlive),
  )
}

/** Can a shell be opened in the run's container? */
export function shellAvailable(run: Pick<RunView, "status" | "container_alive">): boolean {
  if (run.status === "paused") return false
  return !!run.container_alive || run.status === "running" || run.status === "stopping"
}
