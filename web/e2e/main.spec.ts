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
  const draft = await newDraft(page)
  await draft.getByTestId("gpu-trigger").click()
  const picker = page.getByTestId("gpu-picker")
  await expect(picker.getByTestId("gpu-empty")).toHaveText("Choose a machine first")
  await page.keyboard.press("Escape")
  await expect(picker).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)

  await pick(page, "field-machine", "gpu-01")
  await draft.getByTestId("gpu-trigger").click()
  await expect(picker.getByTestId("gpu-row")).toHaveCount(4)
  await picker.locator("[data-testid=gpu-row][data-index='2']").click()
  await picker.locator("[data-testid=gpu-row][data-index='0']").click()
  await expect(draft.getByTestId("field-gpu")).toHaveValue("0,2")
  await picker.getByTestId("gpu-all").click()
  await expect(draft.getByTestId("field-gpu")).toHaveValue("")
  await page.keyboard.press("Escape")

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
  await page.getByTestId("draft-launch").click()
  const row = page.locator('[data-testid=run-row][data-status="running"]').first()
  await expect(row).toBeVisible({ timeout: 10_000 })
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
  const stopped = page.getByTestId("finished-runs").locator('[data-testid=run-row][data-status="stopped"]')
  await expect(stopped).toHaveCount(1, { timeout: 15_000 })
  await expect(stopped.locator("[data-status=stopped]").first()).not.toHaveAttribute("data-training", /.*/)

  // project without MLflow: no progress
  await newDraft(page, "det")
  await pickConfig(page, "yolo_s")
  await page.getByTestId("draft-launch").click()
  const det = page.locator('[data-testid=run-row][data-status="running"]').first()
  await expect(det).toBeVisible({ timeout: 10_000 })
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
