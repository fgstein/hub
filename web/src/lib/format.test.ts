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
    [2 * 86400 + 4 * 3600 + 5, "2j 04h"],
    [-3, "0s"],
  ])("%d s → %s", (s, out) => {
    expect(formatDuration(s)).toBe(out)
  })
})

describe("durées d'un run", () => {
  const t0 = Date.parse("2026-01-01T00:00:00Z")
  it("actif : now - started_at", () => {
    expect(elapsedSeconds({ started_at: "2026-01-01T00:00:00Z", finished_at: null }, t0 + 90_000)).toBe(90)
  })
  it("terminé : figé", () => {
    const run = { started_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:02:00Z" }
    expect(elapsedSeconds(run, t0 + 999_000)).toBe(120)
  })
  it("pas démarré : 0", () => {
    expect(elapsedSeconds({ started_at: null, finished_at: null }, t0)).toBe(0)
  })
  it("pause en cours comptée", () => {
    expect(pausedSeconds({ paused_total_s: 10, paused_at: "2026-01-01T00:00:00Z" }, t0 + 5000)).toBe(15)
  })
})

describe("commande générique", () => {
  it("colore les placeholders", () => {
    const segs = segments("python t.py --config-name {config} id={run_id} {oops} ${HOME}")
    expect(segments("{config_name}")[0]?.kind).toBe("placeholder")
    expect(segs.filter((s) => s.kind === "placeholder").map((s) => s.text)).toEqual(["{config}", "{run_id}"])
    expect(segs.filter((s) => s.kind === "param").map((s) => s.text)).toEqual(["{oops}"])
    expect(segs.map((s) => s.text).join("")).toBe(
      "python t.py --config-name {config} id={run_id} {oops} ${HOME}",
    )
  })
  it("valide {config}", () => {
    expect(validateCommand("python t.py")).toMatch("{config}")
    expect(validateCommand("python t.py {config} {x}")).toBeNull()
    expect(commandParams("t {config} {opts} --o {x} {opts} {run_id}")).toEqual(["opts", "x"])
    expect(validateCommand("python t.py {config} ${oc.env:X}")).toBeNull()
    expect(validateCommand("  ")).toMatch("vide")
  })
})

describe("actions autorisées", () => {
  it("suit la table de la spec", () => {
    expect(allowedActions("running")).toEqual(["pause", "stop"])
    expect(allowedActions("paused")).toEqual(["resume", "stop"])
    expect(allowedActions("failed")).toEqual(["restart", "relaunch", "delete"])
    expect(allowedActions("launching")).toEqual([])
    expect(allowedActions("unreachable")).toEqual([])
  })
})

describe("trames de logs", () => {
  it("décode l'en-tête de 8 octets", () => {
    const payload = new TextEncoder().encode("\x1b[32mok\r\n")
    const frame = new Uint8Array(8 + payload.length)
    frame.set(new TextEncoder().encode("r7f3k2  "))
    frame.set(payload, 8)
    const { runId, data } = parseLogFrame(frame.buffer)
    expect(runId).toBe("r7f3k2")
    expect(new TextDecoder().decode(data)).toBe("\x1b[32mok\r\n")
  })
})

describe("actions et shell", () => {
  it("fermer le conteneur seulement s'il est conservé démarré", () => {
    expect(allowedActions("succeeded")).not.toContain("close")
    expect(allowedActions("succeeded", true)).toContain("close")
    expect(allowedActions("running", true)).not.toContain("close")
  })
  it("shell disponible si le conteneur tourne", () => {
    expect(shellAvailable({ status: "running", container_alive: true })).toBe(true)
    expect(shellAvailable({ status: "paused", container_alive: true })).toBe(false)
    expect(shellAvailable({ status: "failed", container_alive: true })).toBe(true)
    expect(shellAvailable({ status: "failed", container_alive: false })).toBe(false)
  })
})
