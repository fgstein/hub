import { describe, expect, it } from "vitest"
import { formatGiB, parseIndices, toggleIndex } from "./gpus"

describe("gpus", () => {
  it("parses CUDA_VISIBLE_DEVICES", () => {
    expect(parseIndices("")).toBeNull()
    expect(parseIndices("all")).toBeNull()
    expect(parseIndices("0, 2")).toEqual([0, 2])
  })

  it("toggles an index", () => {
    expect(toggleIndex("", 1)).toBe("1")
    expect(toggleIndex("2,0", 1)).toBe("0,1,2")
    expect(toggleIndex("0,1", 0)).toBe("1")
    expect(toggleIndex("1", 1)).toBe("")
  })

  it("formats memory", () => {
    expect(formatGiB(40123)).toBe("39.2")
    expect(formatGiB(81920 * 2)).toBe("160")
    expect(formatGiB(null)).toBe("?")
  })
})
