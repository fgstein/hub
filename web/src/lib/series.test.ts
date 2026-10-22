import { describe, expect, it } from "vitest"
import { DEFAULT_MAX, draftSeries, parseCount, repetitions, seriesRequest } from "./series"
import type { Series } from "./types"

const series = (over: Partial<Series> = {}): Series => ({
  count: 4,
  continue_on_failure: false,
  random: { seed: [0, 99] },
  values: [{ seed: "1" }, { seed: "2" }, { seed: "3" }, { seed: "4" }],
  current: 0,
  exit_codes: [],
  ...over,
})

describe("series", () => {
  it("parses the number of runs", () => {
    expect(parseCount("")).toBe(1)
    expect(parseCount(" 5 ")).toBe(5)
    expect(parseCount("0")).toBeNull()
    expect(parseCount("101")).toBeNull()
    expect(parseCount("2.5")).toBeNull()
  })

  it("builds the request of a draft", () => {
    expect(seriesRequest("", {}, false, ["seed"])).toEqual({ request: null, errors: {} })
    expect(seriesRequest("3", {}, true, ["seed"]).request).toEqual({
      count: 3,
      random: {},
      continue_on_failure: true,
    })
    // a random parameter alone: one run with a drawn value
    expect(seriesRequest("", { seed: { min: "", max: "" } }, false, ["seed"]).request).toEqual({
      count: 1,
      random: { seed: { min: 0, max: DEFAULT_MAX } },
      continue_on_failure: false,
    })
    // ranges of parameters no longer in the command are ignored
    expect(seriesRequest("2", { old: { min: "1", max: "2" } }, false, ["seed"]).request?.random).toEqual({})
  })

  it("reports invalid fields", () => {
    expect(seriesRequest("x", {}, false, []).errors.count).toBeTruthy()
    expect(seriesRequest("2", { seed: { min: "5", max: "1" } }, false, ["seed"]).errors.seed).toMatch(
      "empty range",
    )
    const bad = seriesRequest("2", { seed: { min: "a", max: "" } }, false, ["seed"])
    expect(bad.request).toBeNull()
    expect(bad.errors.seed).toBeTruthy()
  })

  it("prefills a draft from a run's series, default bounds left empty", () => {
    expect(draftSeries(series({ random: { seed: [0, DEFAULT_MAX], fold: [1, 5] } }))).toEqual({
      count: "4",
      random: { seed: { min: "", max: "" }, fold: { min: "1", max: "5" } },
      continueOnFailure: false,
    })
    expect(draftSeries(null)).toEqual({ count: "", random: {}, continueOnFailure: false })
  })

  it("gives the state of each run", () => {
    expect(repetitions(series({ current: 2, exit_codes: [0] }), true)).toEqual([
      "done",
      "running",
      "pending",
      "pending",
    ])
    expect(repetitions(series({ current: 2, exit_codes: [0, 3] }), false)).toEqual([
      "done",
      "failed",
      "skipped",
      "skipped",
    ])
  })
})
