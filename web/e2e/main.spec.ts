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

test("créer un brouillon, lancer, déplier le terminal, plein écran, pause", async ({ page }) => {
  const draft = await newDraft(page)
  // un seul choix possible → label fixe
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

  // plein écran sans rechargement : le même élément xterm est conservé
  const xtermBefore = await term.locator(".xterm").elementHandle()
  await term.getByTestId("terminal-fullscreen").click()
  await expect(page.getByTestId("fullscreen")).toBeVisible()
  const xtermAfter = await page.getByTestId("fullscreen").locator(".xterm").elementHandle()
  expect(await xtermBefore!.evaluate((a, b) => a === b, xtermAfter)).toBe(true)
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("fullscreen")).toHaveCount(0)

  // pause optimiste puis confirmée
  await row.getByTestId("run-primary").click()
  await expect(row).toHaveAttribute("data-status", "paused")
  await expect(row.getByTestId("run-primary")).toHaveAttribute("data-action", "resume")
  await row.getByTestId("run-primary").click()
  await expect(row).toHaveAttribute("data-status", "running")

  // arrêt avec confirmation légère dans le menu
  await row.getByTestId("run-menu").click()
  await page.getByTestId("menu-stop").click()
  await expect(page.getByTestId("menu-stop")).toContainText("Confirmer")
  await page.getByTestId("menu-stop").click()
  const finished = page.getByTestId("finished-runs")
  await expect(finished.locator('[data-testid=run-row][data-status="stopped"]')).toHaveCount(1, {
    timeout: 15_000,
  })
  await expect(page.getByTestId("finished-toggle")).toContainText("Terminés (1)")
})

test("machine injoignable : découverte impossible, échec du lancement affiché", async ({ page }) => {
  await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await pickConfig(page, "baseline")
  await pick(page, "field-machine", "gpu-down")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(page.getByTestId("config-picker")).toContainText("injoignable")
  await page.keyboard.press("Escape")
  await page.getByTestId("draft-launch").click()
  const row = page.locator('[data-testid=run-row][data-status="failed"]').first()
  await expect(row).toBeVisible({ timeout: 10_000 })
  await expect(row.getByTestId("run-error")).toContainText("injoignable")
})

test("commande générique : sauvegarde, validation et retour au YAML", async ({ page }) => {
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

test("onglets : fermer, rouvrir via +, URL et raccourcis", async ({ page }) => {
  await page.goto("/p/seg")
  await expect(page.getByTestId("tab-seg")).toHaveAttribute("aria-selected", "true")
  await page.keyboard.press("Control+Alt+ArrowRight")
  await expect(page).toHaveURL(/\/p\/det$/)
  await page.getByTestId("tab-det").hover()
  await page.getByRole("button", { name: "Fermer Détection" }).click()
  await expect(page.getByTestId("tab-det")).toHaveCount(0)
  await expect(page).toHaveURL(/\/p\/seg$/)
  await page.getByTestId("tab-add").click()
  await page.getByTestId("add-project-det").click()
  await expect(page.getByTestId("tab-det")).toHaveAttribute("aria-selected", "true")
  // configs découvertes chargées pour le projet det
  await page.locator("body").press("n")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(page.locator('[data-testid=config-node][data-path="yolo_m"]')).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("config-picker")).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)
  await page.getByTestId("draft-row").locator("[data-testid=field-gpu]").press("Escape")
  await expect(page.getByTestId("draft-row")).toHaveCount(0)
})

test("mise en page à 900 px : rien ne déborde", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 700 })
  await page.goto("/p/seg")
  await expect(page.getByTestId("project-page")).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})

test("configs : arborescence récursive, compteur de runs, recherche au clavier", async ({ page }) => {
  await newDraft(page)
  await pick(page, "field-machine", "gpu-01")
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  const picker = page.getByTestId("config-picker")
  // arborescence complète, dossiers d'abord, hydra/* exclu
  const nodes = picker.getByTestId("config-node")
  await expect(nodes).toHaveText([/exp\//, /sweep\//, /lr_big/, /lr_small/, /baseline/, /unet_large/])
  const node = (p: string) => picker.locator(`[data-testid=config-node][data-path="${p}"]`)
  const before = Number(await node("exp/sweep/lr_small").getByTestId("config-count").innerText())
  // recherche + Entrée : sélectionne sans lancer
  await page.getByTestId("config-search").fill("small")
  await page.keyboard.press("Enter")
  await expect(picker).toHaveCount(0)
  await expect(page.getByTestId("draft-row")).toHaveCount(1)
  await expect(page.getByTestId("field-config")).toContainText("exp/sweep/lr_small")
  await page.getByTestId("draft-launch").click()
  await expect(
    page.locator("[data-testid=run-row]").filter({ hasText: "exp/sweep/lr_small" }).first(),
  ).toBeVisible()
  // le compteur de la config augmente
  await newDraft(page)
  await page.getByTestId("draft-row").last().getByTestId("field-config").click()
  await expect(node("exp/sweep/lr_small").getByTestId("config-count")).toContainText(String(before + 1))
  await expect(node("exp").getByTestId("config-count")).toContainText(String(before + 1))
})
