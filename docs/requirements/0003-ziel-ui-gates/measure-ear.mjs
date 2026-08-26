import { chromium } from "playwright";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
const url = pathToFileURL(`${dir}/${process.env.MOCK||"0003-ziel-ui-mockup-v8.html"}`).href;
const b = await chromium.launch();
for (const width of [1280, 390]) {
  const p = await b.newPage({ viewport: { width, height: 900 } });
  await p.goto(url); await p.waitForTimeout(200);
  const r = await p.evaluate(() => {
    const frame = document.querySelector(".frame.room");
    frame.scrollIntoView({ block: "start" });
    const stage = frame.querySelector(".stage"), ear = frame.querySelector(".ear");
    const f = frame.getBoundingClientRect(), e = ear.getBoundingClientRect();
    const pipes = [...document.querySelectorAll(".pipe")].map((el) => ({ overflow: el.scrollWidth > el.clientWidth, scrollbar: el.offsetHeight - el.clientHeight }));
    const rail = document.querySelector("nav.rail a.place");
    const lbl = rail.querySelector(".lbl").getBoundingClientRect();
    return {
      frameHeight: Math.round(f.height), stageHasMoreBelow: stage.scrollHeight > stage.clientHeight, stageScrollTop: stage.scrollTop,
      earBottomWithinFrame: e.bottom <= f.bottom + 1 && e.top >= f.top, earVisibleWithoutScroll: e.bottom <= innerHeight && e.top >= 0,
      pipesOverflowing: pipes.filter((x) => x.overflow).length, pipesWithLaidOutScrollbar: pipes.filter((x) => x.overflow && x.scrollbar > 0).length,
      railLabelVisible: lbl.width > 2, railAria: rail.getAttribute("aria-label"), railTitle: rail.getAttribute("title"),
    };
  });
  console.log(width, JSON.stringify(r));
  await p.close();
}
await b.close();
