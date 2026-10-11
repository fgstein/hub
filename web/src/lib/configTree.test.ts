import { describe, expect, it } from "vitest"
import { aggregate, ancestors, buildTree, filterTree, visibleRows } from "./configTree"

const paths = ["baseline", "exp/sweep/unet2", "exp/sweep/unet10", "exp/a", "zeta", "exp/sweep/b"]

describe("config tree", () => {
  const tree = buildTree(paths)

  it("directories first, natural sort", () => {
    expect(tree.map((n) => n.name)).toEqual(["exp", "baseline", "zeta"])
    const exp = tree[0]!
    expect(exp.children.map((n) => n.name)).toEqual(["sweep", "a"])
    expect(exp.children[0]!.children.map((n) => n.path)).toEqual([
      "exp/sweep/b",
      "exp/sweep/unet2",
      "exp/sweep/unet10",
    ])
  })

  it("visible rows and collapsing", () => {
    expect(visibleRows(tree, new Set()).length).toBe(8)
    const rows = visibleRows(tree, new Set(["exp"]))
    expect(rows.map((r) => r.path)).toEqual(["exp", "baseline", "zeta"])
    expect(visibleRows(tree, new Set(["exp"]), true).length).toBe(8)
  })

  it("filters while keeping parents", () => {
    const f = filterTree(tree, "UNET1")
    expect(visibleRows(f, new Set()).map((r) => r.path)).toEqual(["exp", "exp/sweep", "exp/sweep/unet10"])
  })

  it("aggregates runs per directory", () => {
    const counts = { "exp/a": { total: 2, active: 1 }, "exp/sweep/b": { total: 3, active: 0 } }
    expect(aggregate(tree[0]!, counts)).toEqual({ total: 5, active: 1 })
    expect(aggregate(tree[1]!, counts)).toEqual({ total: 0, active: 0 })
  })

  it("ancestors", () => {
    expect(ancestors("exp/sweep/b")).toEqual(["exp", "exp/sweep"])
    expect(ancestors("baseline")).toEqual([])
  })
})
