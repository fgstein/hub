import type {
  CheckpointDeleteResult,
  CheckpointListing,
  CheckpointSourceView,
  ConfigList,
  InventoryView,
  MachineGpus,
  MachineLoad,
  MlflowDeleteResult,
  MlflowOrphanListing,
  PreviewResult,
  ProjectView,
  RunAction,
  RunView,
  Selection,
  UiState,
} from "./types"

/** Business error returned by the API (readable message, shown as is). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    })
  } catch {
    throw new ApiError("TrainHub server unreachable", 0)
  }
  if (res.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`
    throw new ApiError("authentication required", 401)
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data?.detail === "string") detail = data.detail
    } catch {
      /* non-JSON body */
    }
    throw new ApiError(detail, res.status)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  inventory: () => request<InventoryView>("GET", "/api/inventory"),
  configs: (project: string, machine?: string, refresh = false) => {
    const q = new URLSearchParams()
    if (machine) q.set("machine", machine)
    if (refresh) q.set("refresh", "true")
    return request<ConfigList>("GET", `/api/projects/${encodeURIComponent(project)}/configs?${q}`)
  },
  setCommand: (project: string, command: string) =>
    request<ProjectView>("PATCH", `/api/projects/${encodeURIComponent(project)}`, { command }),
  resetCommand: (project: string) =>
    request<ProjectView>("PATCH", `/api/projects/${encodeURIComponent(project)}`, { reset: true }),
  gpus: (machine: string) => request<MachineGpus>("GET", `/api/machines/${encodeURIComponent(machine)}/gpus`),
  load: (machine: string) => request<MachineLoad>("GET", `/api/machines/${encodeURIComponent(machine)}/load`),
  uiState: () => request<UiState>("GET", "/api/ui-state"),
  putUiState: (state: UiState) => request<UiState>("PUT", "/api/ui-state", state),
  preview: (sel: Selection) => request<PreviewResult>("POST", "/api/preview", sel),
  launch: (sel: Selection) => request<RunView>("POST", "/api/runs", sel),
  action: (id: string, action: RunAction) =>
    request<RunView>("POST", `/api/runs/${encodeURIComponent(id)}/${action}`),
  remove: (id: string) => request<void>("DELETE", `/api/runs/${encodeURIComponent(id)}`),
  checkpointSources: () => request<CheckpointSourceView[]>("GET", "/api/checkpoints"),
  checkpoints: (source: string, refresh = false) =>
    request<CheckpointListing>(
      "GET",
      `/api/checkpoints/${encodeURIComponent(source)}${refresh ? "?refresh=true" : ""}`,
    ),
  mlflowOrphans: (refresh = false) =>
    request<MlflowOrphanListing>("GET", `/api/mlflow-orphans${refresh ? "?refresh=true" : ""}`),
  deleteMlflowOrphans: (runIds: string[]) =>
    request<MlflowDeleteResult>("POST", "/api/mlflow-orphans/delete", { run_ids: runIds }),
  deleteCheckpoints: (source: string, paths: string[]) =>
    request<CheckpointDeleteResult>("POST", `/api/checkpoints/${encodeURIComponent(source)}/delete`, {
      paths,
    }),
  logsUrl: (id: string) => `/api/runs/${encodeURIComponent(id)}/logs`,
}
