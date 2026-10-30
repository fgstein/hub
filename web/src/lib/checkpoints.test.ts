import { describe, expect, it } from "vitest"
import {
  checkState,
  flatFiles,
  hostPath,
  inferenceCheckpoint,
  inferenceState,
  inferencesByFile,
  pruneSelection,
  selectionSummary,
  toggleFiles,
  viewRuns,
} from "./checkpoints"
import { formatAgo, formatBytes } from "./format"
import type { CheckpointFile, CheckpointInference, CheckpointListing, CheckpointRun } from "./types"

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
    const withMlflow: CheckpointListing = {
      ...LISTING,
      runs: LISTING.runs.map((r) =>
        r.name === "beta" ? { ...r, mlflow_runs: [{ run_id: "a1b2c3d4", run_name: "unet-large" }] } : r,
      ),
    }
    for (const query of ["UNET-LA", "a1b2"]) {
      const byMlflow = viewRuns(withMlflow, { ...opts, query })
      expect(byMlflow.map((r) => [r.run.name, r.expand])).toEqual([["beta", false]])
    }
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

describe("inferences", () => {
  const inf = (checkpoint: string, extra: Partial<CheckpointInference> = {}): CheckpointInference => ({
    checkpoint,
    exists: true,
    ...extra,
  })

  it("takes the state of the MLflow run, else of the TrainHub run", () => {
    expect(inferenceState(inf("a/x", { mlflow: { run_id: "m", status: "RUNNING" }, status: "failed" }))).toBe(
      "running",
    )
    expect(inferenceState(inf("a/x", { mlflow: { run_id: "m", status: "KILLED" } }))).toBe("failed")
    expect(inferenceState(inf("a/x", { mlflow: { run_id: "m", status: "FINISHED" } }))).toBe("finished")
    expect(inferenceState(inf("a/x", { status: "succeeded" }))).toBe("finished")
    expect(inferenceState(inf("a/x", { status: "stopped" }))).toBe("other")
    expect(inferenceState(inf("a/x"))).toBe("other")
  })

  it("groups by checkpoint and strips the run directory", () => {
    const run = { inferences: [inf("a/c/x.ckpt"), inf("a/c/y.ckpt"), inf("a/c/x.ckpt", { config: "b" })] }
    const by = inferencesByFile(run)
    expect(by.get("a/c/x.ckpt")?.map((i) => i.config ?? null)).toEqual([null, "b"])
    expect(by.get("a/c/y.ckpt")).toHaveLength(1)
    expect(inferenceCheckpoint("a", inf("a/c/x.ckpt"))).toBe("c/x.ckpt")
    expect(inferenceCheckpoint("b", inf("a/c/x.ckpt"))).toBe("a/c/x.ckpt")
  })
})

describe("flatFiles", () => {
  it("lists every checkpoint of a run by name, with its group", () => {
    const groups = [
      {
        name: "topk",
        path: "r/checkpoints/topk",
        size: 0,
        count: 2,
        files: [f("r/c/topk/e3.ckpt"), f("r/c/topk/e1.ckpt")],
      },
      { name: "last", path: "r/checkpoints/last", size: 0, count: 1, files: [f("r/c/last/e3.ckpt")] },
    ]
    const flat = flatFiles({ groups, others: [f("r/a_final.ckpt")] })
    expect(flat.map((x) => [x.file.path, x.group])).toEqual([
      ["r/a_final.ckpt", null],
      ["r/c/topk/e1.ckpt", "topk"],
      ["r/c/last/e3.ckpt", "last"],
      ["r/c/topk/e3.ckpt", "topk"],
    ])
  })
})
