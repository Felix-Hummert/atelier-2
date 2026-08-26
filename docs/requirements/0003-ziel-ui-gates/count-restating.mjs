// Heuristic re-count of restating instances per frame (.frame/.strip/.sheets), numbered like rm-check/shoot.
// A hit = a sentence or label that repeats what an element already shows; the stage↔transcript echo is excluded.
import { chromium } from "playwright";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
const url = pathToFileURL(`${dir}/${process.env.MOCK||"0003-ziel-ui-mockup-v8.html"}`).href;
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
await p.goto(url);
const r = await p.evaluate(() => {
  const out = [];
  document.querySelectorAll(".frame,.strip,.sheets").forEach((f, i) => {
    const hits = [];
    const t = (el) => (el.textContent || "").trim();
    f.querySelectorAll(".rail .later-slot").forEach((el) => hits.push("rail later slot"));
    f.querySelectorAll(".cell > .shelf, .stage > .cards ~ .shelf, .stage > .filters ~ .shelf").forEach((el) => hits.push("label above self-explaining element: " + t(el)));
    f.querySelectorAll(".panel h6 .word").forEach((el) => hits.push("panel state word: " + t(el)));
    f.querySelectorAll(".lede-s").forEach((el) => hits.push("purpose lede repeated: " + t(el).slice(0, 40)));
    // .piece .why is the run's workflow name (content); .role .from "chosen now" is the deliberate deviation marker.
    f.querySelectorAll(".role .from, .sheet .why, .needs, .started, .ear .line, .fld small, .fld label em").forEach((el) => { if (t(el) !== "chosen now") hits.push("caption under control: " + t(el)); });
    f.querySelectorAll(".in span").forEach((el) => { if (/^(optional|required)$/i.test(t(el))) hits.push("optionality as word: " + t(el)); });
    f.querySelectorAll(".head .meta").forEach((el) => { if (/round \d+ of \d+/.test(t(el)) && f.querySelector(".loop .lbl") && /round \d+ of \d+/.test(t(f.querySelector(".loop .lbl")))) hits.push("round count twice"); if (/\bproject\b/.test(t(el)) && f.querySelector(".rail a.project")) hits.push("'project' word next to title"); if (/switch/.test(t(el))) hits.push("second switcher"); });
    f.querySelectorAll(".loop .lbl").forEach((el) => { if (/conversation/i.test(t(el)) && /Conversation/.test(t(f.querySelector(".head h4") || {}))) hits.push("conversation twice"); });
    f.querySelectorAll("th").forEach((el) => { if (/^took$/i.test(t(el))) hits.push("jargon 'Took'"); });
    f.querySelectorAll(".ask .aside").forEach((el) => { if (/stays|whole story/i.test(t(el))) hits.push("inline explanation: " + t(el)); });
    f.querySelectorAll(".by").forEach((el) => { if (/^you ·/.test(t(el))) hits.push("default provenance: " + t(el)); });
    f.querySelectorAll(".src .d").forEach((el) => { if (/provides|repository|workflows, agents, skills/.test(t(el))) hits.push("default provenance word in source: " + t(el).slice(0, 40)); });
    f.querySelectorAll(".node small").forEach((el) => { if (/claude|codex|grok|opus|sonnet|gpt/i.test(t(el))) hits.push("casting under node: " + t(el)); if (/^you\b/.test(t(el))) hits.push("'you' under gate"); if (/difficulty|\btier\b|\b[123]\b/i.test(t(el))) hits.push("difficulty under node: " + t(el)); });
    f.querySelectorAll(".loop .lbl").forEach((el) => { if (/at most/i.test(t(el))) hits.push("loop bound as prose: " + t(el)); });
    f.querySelectorAll(".card").forEach((c) => { if (c.querySelector(".use") && [...c.querySelectorAll(".pill")].some((p) => /claude|codex|grok/i.test(t(p)))) hits.push("provider twice on card"); });
    f.querySelectorAll(".sheet h5").forEach((el) => { if (/^Found in/.test(t(el))) hits.push("sheet title restates rows"); });
    f.querySelectorAll(".head .right .pill").forEach((el) => { if (/▾/.test(t(el))) hits.push("dropdown where chips belong: " + t(el)); });
    f.querySelectorAll(".also").forEach((el) => hits.push("'Also waiting' line instead of a visible question"));
    f.querySelectorAll(".rounds").forEach((el) => { const l = f.querySelector(".loop .lbl"); if (l && /round \d/i.test(t(l)) && /Round \d/.test(t(el))) hits.push("round twice"); });
    f.querySelectorAll(".piece .now").forEach((el) => { if (/waiting for you/i.test(t(el))) hits.push("state word beside its colour: " + t(el)); });
    f.querySelectorAll("td").forEach((el) => { if (el.querySelector(".pill.warn") && el.querySelector(".btn")) hits.push("state pill beside the button that is the state"); });
    { const counts = [...f.querySelectorAll(".filters .f b")].map(t); if (counts.length && counts.every((n) => n === "0")) hits.push("zero-count chips on an empty room"); }
    out.push({ frame: i + 1, title: (f.previousElementSibling?.textContent || "").trim().slice(0, 28), n: hits.length, hits });
  });
  return out;
});
await b.close();
const total = r.reduce((a, x) => a + x.n, 0);
console.log("total restating hits:", total);
for (const f of r) if (f.n) console.log(f.frame, f.title, f.n, f.hits);
console.log("per-frame:", r.map((f) => `F${f.frame}=${f.n}`).join(" "));
