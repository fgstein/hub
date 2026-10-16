import { describe, expect, it } from "vitest"
import { loadColor, percent, summarize } from "./load"
import type { MachineLoad } from "./types"

const gpu = (processes: number) => ({
  index: 0,
  name: "A100",
  memory_used_mib: 1,
  memory_total_mib: 2,
  utilization_pct: 0,
  temperature_c: 30,
  processes,
  runs: [],
})

const load = (over: Partial<MachineLoad>): MachineLoad => ({
  machine: "m",
  reachable: true,
  checked_at: "2026-01-01T00:00:00Z",
  ...over,
})

describe("load helpers", () => {
  it("maps usage to one hue", () => {
    expect(loadColor(null)).toBe("var(--load-empty)")
    expect(loadColor(42.4)).toBe("color-mix(in oklab, var(--load-full) 42%, var(--load-empty))")
    expect(loadColor(150)).toContain(" 100%")
    expect(loadColor(-3)).toContain(" 0%")
  })
  it("computes percentages", () => {
    expect(percent(1, 4)).toBe(25)
    expect(percent(null, 4)).toBeNull()
    expect(percent(1, 0)).toBeNull()
  })
  it("summarizes the machines", () => {
    const cpu = (cores: number, usage_pct: number) => ({
      cores,
      usage_pct,
      per_core_pct: [],
      load_avg: null,
      memory_used_mib: null,
      memory_total_mib: null,
    })
    const s = summarize([
      load({ cpu: cpu(8, 100), gpus: [gpu(1), gpu(0)] }),
      load({ cpu: cpu(24, 0), gpus: [gpu(0)] }),
      load({ reachable: false, gpus: [gpu(3)] }),
      undefined,
    ])
    expect(s).toEqual({ machines: 4, reachable: 2, gpus: 3, busyGpus: 1, cores: 32, cpuPct: 25 })
    expect(summarize([]).cpuPct).toBeNull()
  })
})
