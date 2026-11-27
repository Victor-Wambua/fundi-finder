// Headless-browser smoke test of the demo's key flows, against the built site in demo/_site.
//   python3 demo/build.py && cd demo && npm ci && npx playwright install chromium && npm test
// Optional: CHROME_PATH to use a specific Chrome/Chromium binary.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const SITE = fileURLToPath(new URL("../_site/", import.meta.url));
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".wasm": "application/wasm", ".svg": "image/svg+xml", ".zip": "application/zip", ".md": "text/markdown" };

const server = createServer(async (req, res) => {
  let path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (path.endsWith("/")) path += "index.html";
  const file = normalize(join(SITE, path));
  if (!file.startsWith(SITE)) return res.writeHead(403).end();
  try {
    res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" }).end(await readFile(file));
  } catch {
    res.writeHead(404).end("not found");
  }
});
await new Promise((resolve) => server.listen(Number(process.env.PORT || 0), "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}/`;

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
const foreign = [];
const problems = [];
page.on("request", (r) => {
  if (!r.url().startsWith(base) && !r.url().startsWith("data:") && !r.url().startsWith("blob:")) foreign.push(r.url());
});
page.on("pageerror", (e) => problems.push(e.message));

let step = "";
const run = async (name, fn) => {
  step = name;
  await fn();
  console.log("ok  ", name);
};

const card = (jobType) => page.locator("article.booking", { hasText: jobType });
const ready = async () => {
  await page.waitForSelector("main:not([aria-busy]) h1", { timeout: 120000 });
};

try {
  await run("starts and shows the banner, one h1, and a title", async () => {
    await page.goto(base);
    await ready();
    assert.match(await page.locator(".banner").innerText(), /Static demo\. Runs in your browser on invented data; nothing is sent anywhere/);
    assert.equal(await page.locator("h1").count(), 1);
    assert.match(await page.title(), /Find a fundi/);
    assert.equal(await page.locator(".results > li").count(), 15);
  });

  await run("filters by trade, area and verification", async () => {
    await page.selectOption("#f-trade", "Plumber");
    await page.selectOption("#f-area", "Kilimani");
    await page.check('input[name="verified"]');
    await page.waitForFunction(() => /plumbers? in Kilimani/.test(document.querySelector("#result-count")?.textContent || ""));
    const rows = await page.locator(".results > li").allInnerTexts();
    assert.ok(rows.length >= 2 && rows.length < 5);
    assert.ok(rows.every((t) => t.includes("Plumber") && t.includes("Kilimani")));
    await page.selectOption("#f-area", "Embakasi");
    await page.waitForFunction(() => /plumber/.test(document.querySelector("#result-count")?.textContent || "") && /Embakasi/.test(document.querySelector("#result-count")?.textContent || ""));
    await page.uncheck('input[name="verified"]');
    await page.selectOption("#f-trade", "Tailor");
    await page.fill("#f-max", "100");
    await page.press("#f-max", "Tab");
    await page.waitForFunction(() => /No fundis match/.test(document.querySelector("#result-count")?.textContent || ""));
  });

  await run("a profile shows verified-review counts that match the table of what counts", async () => {
    await page.goto(base + "#/");
    await page.waitForSelector(".results > li");
    await page.locator(".results h2 a", { hasText: "Peter Njoroge" }).click();
    await page.waitForSelector("h1:has-text('Peter Njoroge')");
    assert.match(await page.locator(".rating-line").innerText(), /5\.0\s*from 1 review, each tied to a job confirmed/);
    const stray = page.locator("tr", { hasText: "Reviews on a job without a used code" });
    assert.equal((await stray.locator("td").first().innerText()).trim(), "0");
  });

  await run("a review before the code is refused, with the reason", async () => {
    await page.goto(base + "#/bookings");
    await page.waitForSelector("article.booking");
    const job = card("Unblock a sink or drain");
    await job.locator("details.probe summary").click();
    await job.locator('details.probe input[name="rating"][value="5"]').check();
    await job.locator('details.probe button[type="submit"]').click();
    const alert = job.locator("#m-c-" + (await job.getAttribute("data-req")) + " [role=alert]");
    await alert.waitFor();
    assert.match(await alert.innerText(), /Refused: You can review once the job is complete and confirmed with your completion code/);
  });

  await run("wrong codes count down, lock, and the clock lifts the lock", async () => {
    const job = card("Unblock a sink or drain");
    const req = await job.getAttribute("data-req");
    const code = (await job.locator(".code-ticket .digits").innerText()).replace(/\s/g, "");
    const wrong = code === "111111" ? "222222" : "111111";
    const input = () => page.locator(`#code-c-${req}`);
    await input().fill(wrong);
    await input().press("Enter");
    await page.waitForSelector(`#m-c-${req} :text("4 attempt(s) left")`);
    for (let i = 0; i < 4; i++) {
      await input().fill(wrong);
      await input().press("Enter");
      await page.waitForTimeout(150);
    }
    await page.waitForSelector(`#m-c-${req} :text("Too many wrong codes")`);
    await input().fill(code);
    await input().press("Enter");
    await page.waitForSelector(`#m-c-${req} :text("Too many wrong codes")`); // right code still refused while locked
    assert.ok(await job.locator(":text('Entry is locked until')").count());
    await page.click('[data-skip="1"]');
    await page.waitForFunction((r) => !document.querySelector(`article[data-req="${r}"]`)?.textContent.includes("Entry is locked"), req);
    // the lock lifted; the 5 wrong tries have not been forgiven for the lifetime counter
    await page.locator(`#code-c-${req}`).fill(code);
    await page.locator(`#code-c-${req}`).press("Enter");
    await page.waitForSelector(`#m-c-${req} :text("Confirmed. The job is complete")`);
  });

  await run("after confirmation a review is accepted, counted once, and the code cannot be reused", async () => {
    const job = card("Unblock a sink or drain");
    const req = await job.getAttribute("data-req");
    await job.locator('form[data-action="review"] input[name="rating"][value="4"]').first().check();
    await job.locator('form[data-action="review"] button[type="submit"]').first().click();
    const flash = page.locator(`#m-c-${req} .msg`);
    await flash.filter({ hasText: "Review published" }).waitFor();
    assert.match(await flash.innerText(), /confirmed reviews: 31 to 32/);
    await job.locator("details.probe summary").click();
    await job.locator('input[name="code"]').last().fill("123456");
    await job.locator('form[data-action="probe_reuse"] button').click();
    await page.waitForSelector(`#m-c-${req} :text("This code has already been used")`);
    await job.locator("details.probe summary").click().catch(() => {});
    await job.locator('form[data-action="review"] input[name="rating"][value="1"]').first().check();
    await job.locator('form[data-action="review"] button[type="submit"]').first().click();
    await page.waitForSelector(`#m-c-${req} :text("already reviewed")`);
  });

  await run("the review window closes after 31 days on the demo clock", async () => {
    await page.click('[data-skip="744"]');
    const job = card("Make curtains, per window");
    await job.locator("text=30-day window").first().waitFor();
  });

  await run("request, quote, accept, start, mark done: the booking path", async () => {
    await page.goto(base + "#/");
    await page.waitForSelector(".results > li");
    await page.locator(".results h2 a", { hasText: "Isaac Kibet" }).click();
    await page.waitForSelector("h1:has-text('Isaac Kibet')");
    await page.fill("#q-desc", "short");
    await page.click('#quote-form button[type="submit"]');
    assert.match(await page.locator("#q-desc-err").innerText(), /at least 15 characters. You have 5/);
    await page.fill("#q-desc", "Repaint the lounge, two coats, walls only.");
    await page.click('#quote-form button[type="submit"]');
    await page.waitForSelector("#quote-msg .msg.ok");
    await page.goto(base + "#/bookings");
    const job = card("Paint a room");
    await job.waitFor();
    await job.locator('input[name="amount"]').fill("abc");
    await job.locator('form[data-action="send_quote"] button[type="submit"]').click();
    assert.match(await job.locator('[id$="-amt-err"]').innerText(), /whole number/);
    await job.locator('input[name="amount"]').fill("14,500");
    await job.locator('form[data-action="send_quote"] button[type="submit"]').click();
    await job.locator("text=Accept and book").first().click();
    await job.locator(".code-ticket .digits").waitFor();
    assert.match(await job.locator(".code-ticket .digits").innerText(), /^\d{3} \d{3}$/);
    await job.locator('form[data-event="start"] button').click();
    await job.locator('form[data-event="mark_complete"] button').click();
    await job.locator("text=Waiting for the code").first().waitFor();
    // the fundi cannot finish the job without the code
    await job.locator('#code-f-' + (await job.getAttribute("data-req"))).fill("000000");
    await job.locator('form[data-actor="fundi"][data-action="confirm"] button').click();
    await job.locator("text=That code is not right").first().waitFor();
  });

  await run("state survives a reload, and Reset restores the starting data", async () => {
    await page.waitForTimeout(800); // the page saves a moment after each change
    await page.reload();
    await ready();
    await page.goto(base + "#/bookings");
    await page.waitForSelector("article.booking");
    assert.ok(await card("Paint a room").count(), "the booking made above is still there");
    await page.click("#foot-reset");
    await page.waitForFunction(() => document.querySelectorAll("article.booking").length === 4);
    assert.equal(await card("Paint a room").count(), 0);
  });

  await run("works offline after one load, and made no request to another site", async () => {
    await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.ready;
      return reg.active.state;
    });
    await page.waitForTimeout(3000); // let the worker finish caching
    await context.setOffline(true);
    await page.goto(base + "#/");
    await page.reload();
    await ready();
    assert.equal(await page.locator(".results > li").count(), 15);
    await context.setOffline(false);
    assert.deepEqual(foreign, []);
  });

  assert.deepEqual(problems, [], "no uncaught page errors");
  console.log("All smoke checks passed.");
} catch (error) {
  console.error(`FAILED at step: ${step}`);
  console.error(error);
  await page.screenshot({ path: "smoke-failure.png", fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
  server.close();
}
