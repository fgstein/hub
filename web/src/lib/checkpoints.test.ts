import { describe, expect, it } from "vitest"
import { checkState, hostPath, pruneSelection, selectionSummary, toggleFiles, viewRuns } from "./checkpoints"
import { formatAgo, formatBytes } from "./format"
import type { CheckpointFile, CheckpointListing, CheckpointRun } from "./types"

const f = (path: string, size = 10, mtime = "2026-10-20T10:00:00Z"): CheckpointFile => ({
  path,
  name: path.split("/").pop() ?? path,
  size,
  mtime,
})

function run(
  name: string,
  mtime: string | null,
  groups: Record<string, CheckpointFile[]>,
  others: CheckpointFile[] = [],
): CheckpointRun {
  const gs = Object.entries(groups).map(([g, files]) => ({
    name: g,
    path: `${name}/checkpoints/${g}`,
    size: files.reduce((a, x) => a + x.size, 0),
    count: files.length,
    files,
  }))
  const all = [...gs.flatMap((g) => g.files), ...others]
  return { name, mtime, size: all.reduce((a, x) => a + x.size, 0), count: all.length, groups: gs, others }
}

const LISTING: CheckpointListing = {
  source: "s",
  machine: "m",
  root: "/out",
  layout: "{name}/checkpoints/{group}/*.ckpt",
  size: 0,
  count: 0,
  runs: [
    run("alpha", "2026-10-20T10:00:00Z", {
      last: [f("alpha/checkpoints/last/epoch0000009.ckpt", 100)],
      topk: [
        f("alpha/checkpoints/topk/epoch0000003.ckpt", 100),
        f("alpha/checkpoints/topk/epoch0000007.ckpt", 100),
      ],
    }),
    run("beta", "2026-10-25T10:00:00Z", { topk: [f("beta/checkpoints/topk/epoch0000001.ckpt", 5)] }, [
      f("beta/final.ckpt", 7),
    ]),
    run("crashed", "2026-10-27T10:00:00Z", {}),
  ],
  others: [f("stray.ckpt", 1)],
}

describe("viewRuns", () => {
  const opts = { query: "", sort: "date" as const, hideEmpty: false }

  it("sorts by date, name and size", () => {
    expect(viewRuns(LISTING, opts).map((r) => r.run.name)).toEqual(["crashed", "beta", "alpha"])
    expect(viewRuns(LISTING, { ...opts, sort: "name" }).map((r) => r.run.name)).toEqual([
      "alpha",
      "beta",
      "crashed",
    ])
    expect(viewRuns(LISTING, { ...opts, sort: "size" }).map((r) => r.run.name)).toEqual([
      "alpha",
      "beta",
      "crashed",
    ])
  })

  it("hides empty runs", () => {
    expect(viewRuns(LISTING, { ...opts, hideEmpty: true }).map((r) => r.run.name)).toEqual(["beta", "alpha"])
  })

  it("searches run names (whole run) or files (expanded on the matches)", () => {
    const byName = viewRuns(LISTING, { ...opts, query: "ALP" })
    expect(byName).toHaveLength(1)
    expect(byName[0]?.expand).toBe(false)
    expect(byName[0]?.groups.map((g) => g.files.length)).toEqual([1, 2])
    const byFile = viewRuns(LISTING, { ...opts, query: "epoch0000007" })
    expect(byFile.map((r) => r.run.name)).toEqual(["alpha"])
    expect(byFile[0]?.expand).toBe(true)
    expect(byFile[0]?.groups.map((g) => g.name)).toEqual(["topk"])
    expect(byFile[0]?.groups[0]?.files.map((x) => x.name)).toEqual(["epoch0000007.ckpt"])
    const byGroup = viewRuns(LISTING, { ...opts, query: "/last/" })
    expect(byGroup[0]?.groups.map((g) => g.name)).toEqual(["last"])
    expect(viewRuns(LISTING, { ...opts, query: "final" })[0]?.others.map((x) => x.name)).toEqual([
      "final.ckpt",
    ])
    expect(viewRuns(undefined, opts)).toEqual([])
  })
})

describe("selection", () => {
  const topk = LISTING.runs[0]!.groups[1]!.files

  it("toggles a group: all, then none", () => {
    let sel = toggleFiles(topk, new Set())
    expect(checkState(topk, sel)).toBe("all")
    sel = toggleFiles(topk, sel)
    expect(checkState(topk, sel)).toBe("none")
    sel = toggleFiles(topk, new Set([topk[0]!.path]))
    expect(sel.size).toBe(2)
    expect(checkState(topk, new Set([topk[0]!.path]))).toBe("some")
  })

  it("summarizes files, size, whole groups and runs", () => {
    const sel = new Set([...topk.map((x) => x.path), "beta/final.ckpt"])
    expect(selectionSummary(LISTING, sel)).toEqual({ files: 3, size: 207, groups: 1, runs: 2 })
    expect(selectionSummary(LISTING, new Set())).toEqual({ files: 0, size: 0, groups: 0, runs: 0 })
  })

  it("prunes paths gone from the listing", () => {
    const sel = new Set(["alpha/checkpoints/topk/epoch0000003.ckpt", "gone/x.ckpt", "stray.ckpt"])
    expect([...pruneSelection(LISTING, sel)].sort()).toEqual([
      "alpha/checkpoints/topk/epoch0000003.ckpt",
      "stray.ckpt",
    ])
  })

  it("builds machine paths", () => {
    expect(hostPath("/out/", "a/b.ckpt")).toBe("/out/a/b.ckpt")
  })
})

describe("formatBytes / formatAgo", () => {
  it.each([
    [0, "0 B"],
    [999, "999 B"],
    [1500, "1.50 kB"],
    [412_000_000, "412 MB"],
    [3_712_000_000, "3.71 GB"],
    [19_400_000_000, "19.4 GB"],
    [null, "—"],
  ])("%s → %s", (v, out) => expect(formatBytes(v)).toBe(out))

  it("formats ages", () => {
    const now = Date.parse("2026-10-29T12:00:00Z")
    expect(formatAgo("2026-10-29T11:59:30Z", now)).toBe("just now")
    expect(formatAgo("2026-10-29T11:55:00Z", now)).toBe("5 min ago")
    expect(formatAgo("2026-10-29T09:00:00Z", now)).toBe("3 h ago")
    expect(formatAgo("2026-10-25T12:00:00Z", now)).toBe("4 d ago")
    expect(formatAgo(null, now)).toBe("—")
  })
})
