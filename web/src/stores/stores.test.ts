import { describe, expect, it, vi } from "vitest"
import type { InventoryView, RunView, Snapshot } from "~/lib/types"

class FakeWS {
  static instances: FakeWS[] = []
  static OPEN = 1
  readyState = 0
  binaryType = ""
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((m: { data: unknown }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  constructor(readonly url: string) {
    FakeWS.instances.push(this)
  }
  send(s: string) {
    this.sent.push(s)
  }
  close() {}
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  emit(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) })
  }
}
vi.stubGlobal("WebSocket", FakeWS)

const run = (id: string, over: Partial<RunView> = {}): RunView =>
  ({
    id,
    project_id: "seg",
    machine_id: "m1",
    image_id: "i1",
    venv_id: "v1",
    config: "base",
    command: "python t.py",
    full_command: [],
    container_name: `th-seg-${id}`,
    status: "running",
    exit_code: null,
    created_at: `2026-01-01T00:00:0${id.slice(-1)}Z`,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: null,
    paused_total_s: 0,
    paused_at: null,
    error: null,
    source: "ui",
    overrides: [],
    cuda_visible_devices: null,
    stop_requested: false,
    container_alive: false,
    active: true,
    elapsed_s: 0,
    paused_s: 0,
    ...over,
  }) as RunView

const inventory = {
  machines: [
    { id: "m1", label: null, host: "h", user: null, port: null, status: { id: "m1", reachable: true } },
  ],
  images: [
    { id: "i1", image: "img:1", label: null },
    { id: "i2", image: "img:2", label: null },
  ],
  venvs: [
    { id: "v1", path: "/v1", label: null, images: null },
    { id: "v2", path: "/v2", label: null, images: ["i2"] },
  ],
  projects: [
    {
      id: "seg",
      label: "Seg",
      command: "python t.py {config}",
      yaml_command: "python t.py {config}",
      command_overridden: false,
      workdir: null,
      machines: ["m1"],
      images: ["i1", "i2"],
      venvs: ["v1", "v2"],
      venv_images: { v1: null, v2: ["i2"] },
      static_configs: ["base"],
      discover: false,
      docker: {},
    },
  ],
  error: null,
  executor: "fake",
} as unknown as InventoryView

describe("main store", async () => {
  const app = await import("./app")
  const logs = await import("./logs")
  app.connect()
  const ws = FakeWS.instances[0]!
  ws.open()
  const snapshot: Snapshot = {
    type: "snapshot",
    seq: 10,
    inventory,
    runs: [run("r1"), run("r2", { status: "succeeded", finished_at: "2026-01-01T01:00:00Z" }), run("r3")],
    ui_state: { open_tabs: ["seg"], active_tab: "seg", last_selection: { seg: { image: "i2", venv: "v2" } } },
    server_time: new Date().toISOString(),
  }
  ws.emit(snapshot)

  it("applies the snapshot and sorts runs", () => {
    expect(app.state.ready).toBe(true)
    expect(app.activeRunIds("seg")).toEqual(["r1", "r3"])
    expect(app.finishedRunIds("seg")).toEqual(["r2"])
    expect(app.activeCount("seg")).toBe(2)
  })

  it("ignores events older than the snapshot", () => {
    ws.emit({ type: "run.updated", seq: 5, run: run("r1", { status: "paused" }) })
    expect(app.state.runs.r1?.status).toBe("running")
    ws.emit({ type: "run.updated", seq: 11, run: run("r1", { status: "paused" }) })
    expect(app.state.runs.r1?.status).toBe("paused")
  })

  it("updates finely: another run's object is not replaced", () => {
    const before = app.state.runs.r3
    ws.emit({ type: "run.updated", seq: 12, run: run("r1", { status: "running" }) })
    expect(app.state.runs.r3).toBe(before)
  })

  it("deletes a run", () => {
    ws.emit({ type: "run.deleted", seq: 13, id: "r3", project_id: "seg" })
    expect(app.state.runs.r3).toBeUndefined()
  })

  it("draft: default values and venv/image compatibility", () => {
    app.addDraft("seg")
    const d = app.state.drafts.seg![0]!
    expect(d.image).toBe("i2")
    expect(d.venv).toBe("v2")
    expect(d.config).toBe("base")
    app.updateDraft("seg", d.key, { image: "i1" })
    expect(app.state.drafts.seg![0]!.venv).toBe("v1")
    expect(app.draftSelection("seg", app.state.drafts.seg![0]!)).toMatchObject({ machine: "m1", image: "i1" })
    app.removeDraft("seg", d.key)
    expect(app.state.drafts.seg).toEqual([])
  })

  it("relaunch opens a prefilled draft without launching anything", () => {
    const done = run("r9", {
      status: "failed",
      image_id: "i2",
      venv_id: "v2",
      config: "exp/big",
      overrides: ["trainer.max_epochs=5", "model.name=a b"],
      cuda_visible_devices: "0,1",
    })
    ws.emit({ type: "run.created", seq: 20, run: done })
    const sent = ws.sent.length
    app.draftFromRun("r9")
    const d = app.state.drafts.seg!.at(-1)!
    expect(d).toMatchObject({ machine: "m1", image: "i2", venv: "v2", config: "exp/big", cuda: "0,1" })
    expect(d.overrides).toBe("trainer.max_epochs=5 'model.name=a b'")
    expect(app.draftSelection("seg", d)?.overrides).toEqual(["trainer.max_epochs=5", "model.name=a b"])
    expect(ws.sent.length).toBe(sent)
    app.updateDraft("seg", d.key, { overrides: "x='oops" })
    expect(app.draftSelection("seg", app.state.drafts.seg!.at(-1)!)).toBeNull()
    app.removeDraft("seg", d.key)
  })

  it("log subscriptions are reference counted", () => {
    const chunks: string[] = []
    const off1 = logs.subscribeLogs("r1", {
      data: (c) => chunks.push(new TextDecoder().decode(c)),
      reset: () => {},
    })
    const off2 = logs.subscribeLogs("r1", { data: () => {}, reset: () => {} })
    const subs = ws.sent.filter((s) => s.includes("logs.subscribe"))
    expect(subs).toHaveLength(1)
    logs.dispatchLogs("r1", new TextEncoder().encode("hello"))
    expect(chunks).toEqual(["hello"])
    off1()
    expect(ws.sent.some((s) => s.includes("logs.unsubscribe"))).toBe(false)
    off2()
    expect(ws.sent.some((s) => s.includes("logs.unsubscribe"))).toBe(true)
  })
})
