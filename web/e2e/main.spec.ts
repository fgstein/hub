import { expect, type Page, test } from "@playwright/test"

async function newDraft(page: Page, project = "seg") {
  await page.goto(`/p/${project}`)
  await expect(page.getByTestId("project-page")).toBeVisible()
  await page.locator("body").press("n")
  const draft = page.getByTestId("draft-row").last()
  await expect(draft).toBeVisible()
  return draft
}

async function pickConfig(page: Page, path: string) {
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  const picker = page.getByTestId("config-picker")
  await expect(picker).toBeVisible()
  await picker.locator(`[data-testid=config-node][data-path="${path}"]`).click()
  await expect(picker).toHaveCount(0)
}

/** Launch the last draft and return the row of the new run (other tests leave runs behind). */
async function launchedRow(page: Page) {
  const ids = async () =>
    page
      .locator("[data-testid=run-row]")
      .evaluateAll((rows) => rows.map((r) => r.getAttribute("data-run-id")))
  const before = new Set(await ids())
  await page.getByTestId("draft-launch").click()
  let id: string | null | undefined
  await expect(async () => {
    id = (await ids()).find((x) => x && !before.has(x))
    expect(id).toBeTruthy()
  }).toPass({ timeout: 10_000 })
  return page.locator(`[data-testid=run-row][data-run-id="${id}"]`)
}

async function pick(page: Page, field: string, value: string) {
  const draft = page.getByTestId("draft-row").last()
  const input = draft.getByTestId(field).locator("input")
  await input.click()
  await input.fill(value)
  await page.getByRole("option", { name: value, exact: true }).click()
}

test("create a draft, launch, expand the terminal, fullscreen, pause", async ({ page }) => {
  const draft = await newDraft(page)
  // a single possible choice → fixed label
  await expect(draft.getByTestId("field-image")).toHaveAttribute("data-fixed", "")
  await pick(page, "field-machine", "gpu-01")
  await pickConfig(page, "unet_large")
  await draft.getByTestId("draft-launch").click()

  const running = page.locator('[data-testid=run-row][data-status="running"]').first()
  await expect(running).toBeVisible({ timeout: 10_000 })
  const runId = await running.getAttribute("data-run-id")
  const row = page.locator(`[data-testid=run-row][data-run-id="${runId}"]`)
  await expect(page.getByTestId("draft-row")).toHaveCount(0)
  await expect(page.getByTestId("tab-seg").getByTestId("tab-count")).toHaveText("· 1")
  await expect(row).toContainText("unet_large")

  // terminal
  await row.getByTestId("run-line").click()
  const term = row.getByTestId("terminal")
  await expect(term).toBeVisible()
  await expect(term.locator(".xterm-rows")).toContainText("Epoch", { timeout: 10_000 })

  // fullscreen without reload: the same xterm element is kept
  const xtermBefore = await term.locator(".xterm").elementHandle()
  await term.getByTestId("terminal-fullscreen").click()
  await expect(page.getByTestId("fullscreen")).toBeVisible()
  const xtermAfter = await page.getByTestId("fullscreen").locator(".xterm").elementHandle()
  expect(await xtermBefore!.evaluate((a, b) => a === b, xtermAfter)).toBe(true)
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("fullscreen")).toHaveCount(0)

  // optimistic then confirmed pause
  await row.getByTestId("run-primary").click()
  await expect(row).toHaveAttribute("data-status", "paused")
  await expect(row.getByTestId("run-primary")).toHaveAttribute("data-action", "resume")
  await row.getByTestId("run-primary").click()
  await expect(row).toHaveAttribute("data-status", "running")

  // stop with a light confirmation in the menu
  await row.getByTestId("run-menu").click()
  await page.getByTestId("menu-stop").click()
  await expect(page.getByTestId("menu-stop")).toContainText("Confirm")
  await page.getByTestId("menu-stop").click()
  const finished = page.getByTestId("finished-runs")
  await expect(finished.locator('[data-testid=run-row][data-status="stopped"]')).toHaveCount(1, {
    timeout: 15_000,
  })
  await expect(page.getByTestId("finished-toggle")).toContainText("Finished (1)")
})

test("unreachable machine: discovery fails, launch failure shown", async ({ page }) => {
  await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await pickConfig(page, "baseline")
  await pick(page, "field-machine", "gpu-down")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(page.getByTestId("config-picker")).toContainText("unreachable")
  await page.keyboard.press("Escape")
  await page.getByTestId("draft-launch").click()
  const row = page.locator('[data-testid=run-row][data-status="failed"]').first()
  await expect(row).toBeVisible({ timeout: 10_000 })
  await expect(row.getByTestId("run-error")).toContainText("unreachable")
})

test("Escape reaching the draft while a picker is open only closes the picker", async ({ page }) => {
  const draft = await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await draft.getByTestId("field-config").click()
  const picker = page.getByTestId("config-picker")
  await expect(picker).toBeVisible()
  // as when the picker has not taken the focus yet (slow machine): Escape lands on a field
  const machine = draft.getByTestId("field-machine").locator("input")
  await machine.dispatchEvent("keydown", { key: "Escape", bubbles: true })
  await expect(picker).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)
  // nothing open any more: Escape cancels the draft
  await draft.getByTestId("field-gpu").press("Escape")
  await expect(page.getByTestId("draft-row")).toHaveCount(0)
})

test("generic command: saving, validation and back to YAML", async ({ page }) => {
  await page.goto("/p/seg")
  const input = page.getByTestId("command-input")
  await input.fill("python train.py")
  await expect(page.getByTestId("command-error")).toContainText("{config}")
  await page.getByTestId("add-draft").click()
  await expect(page.getByTestId("draft-launch")).toBeDisabled()
  await input.fill("python train.py --config-name {config} +debug=true")
  await expect(page.getByTestId("command-saved")).toBeVisible()
  await page.reload()
  await expect(page.getByTestId("command-input")).toHaveValue(
    "python train.py --config-name {config} +debug=true",
  )
  await page.getByTestId("command-reset").click()
  await expect(page.getByTestId("command-input")).toHaveValue("python train.py --config-name {config}")
  await page.getByTestId("preview-toggle").click()
  await expect(page.getByTestId("preview")).toContainText("docker run -d -t --init")
})

test("tabs: close, reopen with +, URL and shortcuts", async ({ page }) => {
  await page.goto("/p/seg")
  await expect(page.getByTestId("tab-seg")).toHaveAttribute("aria-selected", "true")
  await page.keyboard.press("Control+Alt+ArrowRight")
  await expect(page).toHaveURL(/\/p\/det$/)
  await page.getByTestId("tab-det").hover()
  await page.getByRole("button", { name: "Close Detection" }).click()
  await expect(page.getByTestId("tab-det")).toHaveCount(0)
  await expect(page).toHaveURL(/\/p\/seg$/)
  await page.getByTestId("tab-add").click()
  await page.getByTestId("add-project-det").click()
  await expect(page.getByTestId("tab-det")).toHaveAttribute("aria-selected", "true")
  // discovered configs loaded for the det project
  await page.locator("body").press("n")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(page.locator('[data-testid=config-node][data-path="yolo_m"]')).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("config-picker")).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)
  await page.getByTestId("draft-row").locator("[data-testid=field-gpu]").press("Escape")
  await expect(page.getByTestId("draft-row")).toHaveCount(0)
})

test("GPU picker: nvidia-smi state of the machine, click toggles CUDA_VISIBLE_DEVICES", async ({ page }) => {
  // A new draft always has a machine (the last one used, else the first allowed): the picker's
  // “Choose a machine first” state cannot be reached from a draft.
  const draft = await newDraft(page)
  const picker = page.getByTestId("gpu-picker")
  await pick(page, "field-machine", "gpu-01")
  await draft.getByTestId("gpu-trigger").click()
  await expect(picker.getByTestId("gpu-row")).toHaveCount(4)
  await picker.locator("[data-testid=gpu-row][data-index='2']").click()
  await picker.locator("[data-testid=gpu-row][data-index='0']").click()
  await expect(draft.getByTestId("field-gpu")).toHaveValue("0,2")
  await picker.getByTestId("gpu-all").click()
  await expect(draft.getByTestId("field-gpu")).toHaveValue("")
  await page.keyboard.press("Escape")
  await expect(picker).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)

  await pick(page, "field-machine", "gpu-down")
  await draft.getByTestId("gpu-trigger").click()
  await expect(picker.getByTestId("gpu-empty")).toContainText("unreachable")
})

test("layout at 900 px: nothing overflows", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 700 })
  await page.goto("/p/seg")
  await expect(page.getByTestId("project-page")).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})

test("configs: recursive tree, run counter, keyboard search", async ({ page }) => {
  await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  const picker = page.getByTestId("config-picker")
  // full tree, directories first, hydra/* excluded
  const nodes = picker.getByTestId("config-node")
  await expect(nodes).toHaveText([/exp\//, /sweep\//, /lr_big/, /lr_small/, /baseline/, /unet_large/])
  const node = (p: string) => picker.locator(`[data-testid=config-node][data-path="${p}"]`)
  const before = Number(await node("exp/sweep/lr_small").getByTestId("config-count").innerText())
  // search + Enter: selects without launching
  await page.getByTestId("config-search").fill("small")
  await page.keyboard.press("Enter")
  await expect(picker).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)
  await expect(page.getByTestId("field-config")).toContainText("exp/sweep/lr_small")
  await page.getByTestId("draft-launch").click()
  await expect(
    page.locator("[data-testid=run-row]").filter({ hasText: "exp/sweep/lr_small" }).first(),
  ).toBeVisible()
  // the config counter goes up
  await newDraft(page)
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(node("exp/sweep/lr_small").getByTestId("config-count")).toContainText(String(before + 1))
  await expect(node("exp").getByTestId("config-count")).toContainText(String(before + 1))
})

test("MLflow: progress bar, dot color and link to the run", async ({ page }) => {
  await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await pickConfig(page, "baseline")
  const row = await launchedRow(page)
  await expect(row).toHaveAttribute("data-status", "running", { timeout: 10_000 })
  await expect(row.getByTestId("run-progress")).toBeVisible({ timeout: 10_000 })
  await expect(row.getByTestId("run-epoch")).toHaveText(/^\d+\/\d+$/)
  await expect(row.locator("[data-training]")).toHaveAttribute("data-training", "healthy")
  await expect(row.getByTestId("run-mlflow-link")).toHaveAttribute(
    "href",
    /^http:\/\/mlflow\.e2e:5000\/#\/experiments\/\d+\/runs\/[0-9a-f]+$/,
  )
  // stopped: MLflow says KILLED, but a deliberate stop keeps its grey dot
  await row.getByTestId("run-menu").click()
  await page.getByTestId("menu-stop").click()
  await page.getByTestId("menu-stop").click()
  const stopped = page
    .getByTestId("finished-runs")
    .locator(`[data-run-id="${await row.getAttribute("data-run-id")}"]`)
  await expect(stopped).toHaveAttribute("data-status", "stopped", { timeout: 15_000 })
  await expect(stopped.locator("[data-status=stopped]").first()).not.toHaveAttribute("data-training", /.*/)

  // project without MLflow: no progress
  await newDraft(page, "det")
  await pickConfig(page, "yolo_s")
  const det = await launchedRow(page)
  await expect(det).toHaveAttribute("data-status", "running", { timeout: 10_000 })
  await page.waitForTimeout(2500)
  await expect(det.getByTestId("run-progress")).toHaveCount(0)
})

test("Machines tab: CPU and GPU load of every machine", async ({ page }) => {
  await page.goto("/p/seg")
  await page.getByTestId("tab-machines").click()
  await expect(page).toHaveURL(/\/machines$/)
  await expect(page.getByTestId("machines-page")).toBeVisible()
  // the project tab is no longer the active one
  await expect(page.getByTestId("tab-seg")).toHaveAttribute("aria-selected", "false")
  const up = page.getByTestId("machine-card-gpu-01")
  await expect(up).toHaveAttribute("data-reachable", "true", { timeout: 10_000 })
  await expect(up.getByTestId("machine-cores").getByRole("listitem")).toHaveCount(64)
  await expect(up.getByTestId("machine-gpu")).toHaveCount(4)
  await expect(up.getByTestId("machine-cpu-usage")).toContainText("64 cores")
  const down = page.getByTestId("machine-card-gpu-down")
  await expect(down).toHaveAttribute("data-reachable", "false", { timeout: 10_000 })
  await expect(down.getByTestId("machine-error")).toBeVisible()
  await expect(page.getByTestId("machines-summary")).toContainText("1/2 reachable")
  // back to the project
  await page.getByTestId("tab-seg").click()
  await expect(page.getByTestId("project-page")).toBeVisible()
})

test("Machines tab: a click on a GPU prepares a draft on that machine and GPU", async ({ page }) => {
  await page.goto("/machines")
  const up = page.getByTestId("machine-card-gpu-01")
  const gpu2 = up.locator("[data-testid=machine-gpu][data-index='2']").getByTestId("machine-gpu-launch")
  await expect(gpu2).toBeVisible({ timeout: 10_000 })
  // seg and det can both run on gpu-01: the project is asked
  await gpu2.click()
  await expect(page.getByTestId("gpu-project-menu")).toBeVisible()
  await page.getByTestId("gpu-project-seg").click()
  await expect(page).toHaveURL(/\/p\/seg$/)
  const draft = page.getByTestId("draft-row").last()
  await expect(draft).toBeVisible()
  await expect(draft.getByTestId("field-gpu")).toHaveValue("2")
  await expect(draft.getByTestId("field-machine").locator("input")).toHaveValue("gpu-01")
  // the machine is already chosen: its list does not open
  await expect(page.getByRole("listbox")).toHaveCount(0)
})

test("series: several runs in a row, a parameter drawn at random", async ({ page }) => {
  await page.request.patch("/api/projects/det", {
    data: { command: "python -m det.train --config-name {config} seed={seed}" },
  })
  try {
    const draft = await newDraft(page, "det")
    await pickConfig(page, "yolo_m")
    await draft.getByTestId("toggle-random-seed").click()
    await expect(draft.getByTestId("field-param-seed")).toHaveCount(0)
    await draft.getByTestId("field-min-seed").fill("5")
    await draft.getByTestId("field-max-seed").fill("1")
    await expect(draft.getByTestId("draft-launch")).toBeDisabled()
    await draft.getByTestId("field-max-seed").fill("7")
    await draft.getByTestId("field-count").fill("3")
    await expect(draft.getByTestId("field-continue-on-failure")).not.toBeChecked()
    await draft.getByTestId("draft-launch").click()

    const row = page.locator('[data-testid=run-row][data-status="running"]').filter({ hasText: "yolo_m" })
    await expect(row).toHaveCount(1, { timeout: 10_000 })
    await expect(row.getByTestId("run-series-progress")).toHaveText("1/3", { timeout: 10_000 })
    await row.getByTestId("run-line").click()
    await expect(row.getByTestId("run-command-text")).toContainText(/seed=[567]/)
    const runId = await row.getAttribute("data-run-id")
    const logs = await (await page.request.get(`/api/runs/${runId}/logs`)).text()
    expect(logs).toMatch(/series run 1\/3 started · seed=[567]/)
  } finally {
    await page.request.patch("/api/projects/det", { data: { reset: true } })
  }
})

test("checkpoints: totals, send to an inference draft, launch, delete with the slider", async ({ page }) => {
  await page.goto("/checkpoints")
  const source = page.getByTestId("ckpt-source-out")
  await expect(source.getByTestId("ckpt-source-total")).toContainText("9 checkpoints")
  await expect(page.getByTestId("tab-checkpoints")).toHaveAttribute("aria-selected", "true")
  await expect(page.getByTestId("ckpt-run")).toHaveCount(3)

  // search on a file name: the run opens on the matches only
  await page.getByTestId("ckpt-search").fill("model_final")
  await expect(page.getByTestId("ckpt-file")).toHaveCount(1)
  await page.getByTestId("ckpt-search").fill("")

  const runA = page.locator("[data-testid=ckpt-run][data-run=run_a]")
  await runA.getByRole("button").first().click()
  // checkpoints in a single list, each labelled with its group
  const topk = runA
    .getByTestId("ckpt-file")
    .filter({ has: page.getByTestId("ckpt-file-group").getByText("topk", { exact: true }) })
  await expect(topk).toHaveCount(3)
  const file = topk.first()
  const path = await file.getAttribute("data-path")
  await file.click()
  await expect(page.getByTestId("ckpt-send-bar")).toContainText("1 checkpoint selected")
  await page.getByTestId("ckpt-send").click()

  await expect(page).toHaveURL(/\/p\/infer$/)
  const draft = page.getByTestId("draft-row").last()
  await expect(draft.getByTestId("field-checkpoint")).toHaveValue(`/out/${path}`)
  await expect(draft.getByTestId("checkpoint-origin")).toHaveText("Outputs · run_a")
  await pickConfig(page, "baseline")
  await draft.getByTestId("draft-launch").click()
  await expect(page.getByTestId("run-checkpoint")).toContainText("topk/")

  // back to the checkpoints (MLflow is the only source of the inferences: no TrainHub count)
  await page.getByTestId("tab-checkpoints").click()
  await runA.getByRole("button").first().click()
  await expect(runA.locator(`[data-path="${path}"]`)).toBeVisible()
  await expect(runA.getByTestId("ckpt-file-runs")).toHaveCount(0)

  // delete mode: the checkpoints of a group, a partial slide does nothing, the full slide deletes
  await page.getByTestId("ckpt-delete-mode").click()
  for (const check of await topk.getByTestId("ckpt-file-check").all()) await check.check()
  await expect(page.getByTestId("ckpt-delete-summary")).toContainText("1 group, 3 checkpoints in 1 run")
  const handle = page.getByTestId("ckpt-slide-handle")
  const box = (await handle.boundingBox())!
  await page.mouse.move(box.x + 10, box.y + 10)
  await page.mouse.down()
  await page.mouse.move(box.x + 120, box.y + 10, { steps: 5 })
  await page.mouse.up()
  await expect(topk).toHaveCount(3)
  await handle.focus()
  for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowRight")
  await expect(page.getByText("3 checkpoints deleted")).toBeVisible()
  await expect(topk).toHaveCount(0)
  await expect(page.getByTestId("ckpt-delete-bar")).toHaveCount(0)
  await expect(source.getByTestId("ckpt-source-total")).toContainText("6 checkpoints")

  // whole run directories: the directory itself goes, not only its checkpoints
  await page.getByTestId("ckpt-delete-mode").click()
  await page.getByTestId("ckpt-delete-scope-runs").click()
  await expect(page.getByTestId("ckpt-file-check")).toHaveCount(0)
  await page.locator('[data-testid=ckpt-run][data-run="run_b"]').getByTestId("ckpt-rundir-check").check()
  await expect(page.getByTestId("ckpt-delete-summary")).toContainText("1 run directory")
  await handle.focus()
  for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowRight")
  await expect(page.getByText("1 run directory deleted")).toBeVisible()
  await expect(page.getByTestId("ckpt-run")).toHaveCount(2)
  await expect(page.locator('[data-testid=ckpt-run][data-run="run_b"]')).toHaveCount(0)
})

test("deep links: ?run= opens a run directory, and brings a project's run into view", async ({ page }) => {
  // The links Lookin's badges hand out (see the lookin section of the README).
  await page.goto("/checkpoints/out?run=run_c")
  const runC = page.locator("[data-testid=ckpt-run][data-run=run_c]")
  await expect(runC.getByTestId("ckpt-run-info")).toBeVisible()
  await expect(page.getByTestId("ckpt-run-info")).toHaveCount(1)
  // no lookin section in this inventory: no Lookin badge nor button anywhere
  await expect(page.getByTestId("lookin-links")).toHaveCount(0)

  await newDraft(page)
  await pickConfig(page, "baseline")
  const id = await (await launchedRow(page)).getAttribute("data-run-id")
  await page.goto("/checkpoints")
  await page.goto(`/p/seg?run=${id}`)
  await expect(page.locator(`[data-testid=run-row][data-run-id="${id}"]`)).toBeInViewport()
})
