import { describe, expect, it } from "vitest"
import { commandParams, segments, validateCommand } from "./command"
import { elapsedSeconds, formatDuration, pausedSeconds } from "./format"
import { allowedActions, shellAvailable } from "./types"
import { parseLogFrame } from "./ws"

describe("formatDuration", () => {
  it.each([
    [0, "0s"],
    [12, "12s"],
    [60, "1m 00s"],
    [245, "4m 05s"],
    [3 * 3600 + 12 * 60, "3h 12m"],
    [2 * 86400 + 4 * 3600 + 5, "2d 04h"],
    [-3, "0s"],
  ])("%d s → %s", (s, out) => {
    expect(formatDuration(s)).toBe(out)
  })
})

describe("run durations", () => {
  const t0 = Date.parse("2026-01-01T00:00:00Z")
  it("active: now - started_at", () => {
    expect(elapsedSeconds({ started_at: "2026-01-01T00:00:00Z", finished_at: null }, t0 + 90_000)).toBe(90)
  })
  it("finished: frozen", () => {
    const run = { started_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:02:00Z" }
    expect(elapsedSeconds(run, t0 + 999_000)).toBe(120)
  })
  it("not started: 0", () => {
    expect(elapsedSeconds({ started_at: null, finished_at: null }, t0)).toBe(0)
  })
  it("ongoing pause counted", () => {
    expect(pausedSeconds({ paused_total_s: 10, paused_at: "2026-01-01T00:00:00Z" }, t0 + 5000)).toBe(15)
  })
})

describe("generic command", () => {
  it("highlights placeholders", () => {
    const segs = segments("python t.py --config-name {config} id={run_id} {oops} ${HOME}")
    expect(segments("{config_name}")[0]?.kind).toBe("placeholder")
    expect(segs.filter((s) => s.kind === "placeholder").map((s) => s.text)).toEqual(["{config}", "{run_id}"])
    expect(segs.filter((s) => s.kind === "param").map((s) => s.text)).toEqual(["{oops}"])
    expect(segs.map((s) => s.text).join("")).toBe(
      "python t.py --config-name {config} id={run_id} {oops} ${HOME}",
    )
  })
  it("validates {config}", () => {
    expect(validateCommand("python t.py")).toMatch("{config}")
    expect(validateCommand("python t.py {config} {x}")).toBeNull()
    expect(commandParams("t {config} {opts} --o {x} {opts} {run_id}")).toEqual(["opts", "x"])
    expect(validateCommand("python t.py {config} ${oc.env:X}")).toBeNull()
    expect(validateCommand("  ")).toMatch("empty")
  })
})

describe("allowed actions", () => {
  it("follows the spec table", () => {
    expect(allowedActions("running")).toEqual(["pause", "stop", "force_finish"])
    expect(allowedActions("paused")).toEqual(["resume", "stop", "force_finish"])
    expect(allowedActions("failed")).toEqual(["restart", "relaunch", "delete"])
    expect(allowedActions("launching")).toEqual(["force_finish"])
    expect(allowedActions("unreachable")).toEqual(["force_finish"])
    expect(allowedActions("stopping")).toEqual(["force_finish"])
  })
})

describe("log frames", () => {
  it("decodes the 8-byte header", () => {
    const payload = new TextEncoder().encode("\x1b[32mok\r\n")
    const frame = new Uint8Array(8 + payload.length)
    frame.set(new TextEncoder().encode("r7f3k2  "))
    frame.set(payload, 8)
    const { runId, data } = parseLogFrame(frame.buffer)
    expect(runId).toBe("r7f3k2")
    expect(new TextDecoder().decode(data)).toBe("\x1b[32mok\r\n")
  })
})

describe("actions and shell", () => {
  it("close container only if it is kept started", () => {
    expect(allowedActions("succeeded")).not.toContain("close")
    expect(allowedActions("succeeded", true)).toContain("close")
    expect(allowedActions("running", true)).not.toContain("close")
  })
  it("shell available if the container is running", () => {
    expect(shellAvailable({ status: "running", container_alive: true })).toBe(true)
    expect(shellAvailable({ status: "paused", container_alive: true })).toBe(false)
    expect(shellAvailable({ status: "failed", container_alive: true })).toBe(true)
    expect(shellAvailable({ status: "failed", container_alive: false })).toBe(false)
  })
})
