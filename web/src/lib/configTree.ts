/**
 * Config tree: a config is a path relative to the root, without extension
 * (e.g. "experiments/sweep/unet"). Pure, tested functions.
 */

export interface RunCounts {
  total: number
  active: number
}

export interface TreeNode {
  /** Full path (config identifier for a file, prefix for a directory). */
  path: string
  name: string
  kind: "dir" | "file"
  depth: number
  children: TreeNode[]
}

export function buildTree(paths: readonly string[]): TreeNode[] {
  const root: TreeNode = { path: "", name: "", kind: "dir", depth: -1, children: [] }
  for (const p of paths) {
    const parts = p.split("/").filter(Boolean)
    let node = root
    parts.forEach((part, i) => {
      const isFile = i === parts.length - 1
      const path = parts.slice(0, i + 1).join("/")
      let child = node.children.find((c) => c.name === part && c.kind === (isFile ? "file" : "dir"))
      if (!child) {
        child = { path, name: part, kind: isFile ? "file" : "dir", depth: i, children: [] }
        node.children.push(child)
      }
      node = child
    })
  }
  const sort = (n: TreeNode) => {
    n.children.sort((a, b) =>
      a.kind !== b.kind ? (a.kind === "dir" ? -1 : 1) : a.name.localeCompare(b.name, "en", { numeric: true }),
    )
    n.children.forEach(sort)
  }
  sort(root)
  return root.children
}

/** Keep the files whose path contains the query (and their parent directories). */
export function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  const q = query.trim().toLowerCase()
  if (!q) return nodes
  const out: TreeNode[] = []
  for (const n of nodes) {
    if (n.kind === "file") {
      if (n.path.toLowerCase().includes(q)) out.push(n)
    } else {
      const children = filterTree(n.children, q)
      if (children.length) out.push({ ...n, children })
    }
  }
  return out
}

/** Visible rows (depth-first walk), taking collapsed directories into account. */
export function visibleRows(
  nodes: TreeNode[],
  collapsed: ReadonlySet<string>,
  forceOpen = false,
): TreeNode[] {
  const rows: TreeNode[] = []
  const walk = (list: TreeNode[]) => {
    for (const n of list) {
      rows.push(n)
      if (n.kind === "dir" && (forceOpen || !collapsed.has(n.path))) walk(n.children)
    }
  }
  walk(nodes)
  return rows
}

/** Totals of a directory (sum of the files it contains). */
export function aggregate(node: TreeNode, counts: Readonly<Record<string, RunCounts>>): RunCounts {
  if (node.kind === "file") return counts[node.path] ?? { total: 0, active: 0 }
  const acc = { total: 0, active: 0 }
  for (const c of node.children) {
    const r = aggregate(c, counts)
    acc.total += r.total
    acc.active += r.active
  }
  return acc
}

/** Ancestor directories of a path (to expand down to the selected config). */
export function ancestors(path: string): string[] {
  const parts = path.split("/")
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"))
}
