import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
import { tmpdir } from "node:os";
const out = process.env.SHOTS || join(tmpdir(), "atelier-mockup-v8-shots");
mkdirSync(out, { recursive: true });
const url = pathToFileURL(`${dir}/${process.env.MOCK||"0003-ziel-ui-mockup-v8.html"}`).href;

const browser = await chromium.launch();
const report = [];
for (const width of [1280, 390]) {
  for (const scheme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: scheme });
    await page.goto(url);
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      wide: [...document.querySelectorAll("*")]
        .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
        .slice(0, 8)
        .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join(".")} right=${Math.round(el.getBoundingClientRect().right)}`),
    }));
    report.push({ width, scheme, ...overflow });
    await page.screenshot({ path: `${out}/full-${width}-${scheme}.png`, fullPage: true });
    {
      const frames = await page.$$(".frame, .sheets, .strip");
      let i = 0;
      for (const f of frames) {
        i += 1;
        await f.screenshot({ path: `${out}/frame-${String(i).padStart(2, "0")}-${width}-${scheme}.png` });
      }
    }
    await page.close();
  }
}
await browser.close();
console.log(JSON.stringify(report, null, 2));
