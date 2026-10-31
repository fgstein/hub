import { describe, expect, it } from "vitest"
import { externalTraining, formatEpoch, progress, trainingState } from "./mlflow"

describe("trainingState", () => {
  it("keeps the docker color without MLflow", () => {
    expect(trainingState("running", null)).toBeNull()
    expect(trainingState("succeeded", undefined)).toBeNull()
  })
  it("maps running runs", () => {
    expect(trainingState("running", "RUNNING")).toBe("healthy")
    expect(trainingState("running", "SCHEDULED")).toBe("healthy")
    expect(trainingState("running", "FAILED")).toBe("failed")
    expect(trainingState("running", "KILLED")).toBe("failed")
    expect(trainingState("running", "FINISHED")).toBe("finished")
  })
  it("shows a stalled training (no new metric) only while it runs", () => {
    expect(trainingState("running", "RUNNING", true)).toBe("stale")
    expect(trainingState("running", "FAILED", true)).toBe("failed")
    expect(trainingState("succeeded", "FINISHED", true)).toBe("finished")
    expect(trainingState("paused", "RUNNING", true)).toBeNull()
  })
  it("maps finished runs", () => {
    expect(trainingState("succeeded", "FINISHED")).toBe("finished")
    expect(trainingState("succeeded", "RUNNING")).toBe("finished")
    expect(trainingState("succeeded", "FAILED")).toBe("failed")
    expect(trainingState("failed", "FINISHED")).toBe("failed")
  })
  it("never overrides container states", () => {
    for (const s of ["launching", "paused", "stopping", "stopped", "unreachable"] as const)
      expect(trainingState(s, "FAILED")).toBeNull()
  })
})

describe("externalTraining", () => {
  it("maps the MLflow status", () => {
    expect(externalTraining("RUNNING")).toBe("healthy")
    expect(externalTraining("RUNNING", true)).toBe("stale")
    expect(externalTraining("FINISHED")).toBe("finished")
    expect(externalTraining("KILLED")).toBe("failed")
  })
})

describe("progress", () => {
  it("needs a matched MLflow run", () => {
    expect(progress({ mlflow: null })).toBeNull()
    expect(progress({ mlflow: { run_id: null, epoch: 3, max_epochs: 10 } })).toBeNull()
    expect(progress({ mlflow: { run_id: "a" } })).toBeNull()
  })
  it("computes the fraction", () => {
    expect(progress({ mlflow: { run_id: "a", epoch: 3, max_epochs: 10 } })).toEqual({
      epoch: 3,
      max: 10,
      fraction: 0.3,
    })
    expect(progress({ mlflow: { run_id: "a", epoch: 12, max_epochs: 10 } })?.fraction).toBe(1)
    expect(progress({ mlflow: { run_id: "a", max_epochs: 10 } })?.fraction).toBe(0)
    expect(progress({ mlflow: { run_id: "a", epoch: 4 } })?.fraction).toBeNull()
  })
  it("formats epochs", () => {
    expect(formatEpoch(3)).toBe("3")
    expect(formatEpoch(2.54)).toBe("2.5")
    expect(formatEpoch(null)).toBe("?")
  })
})
