import { chromium } from "playwright";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
const url=pathToFileURL(`${dir}/${process.env.MOCK||"0003-ziel-ui-mockup-v8.html"}`).href;
const b=await chromium.launch();
for (const scheme of ["light","dark"]) {
  const p=await b.newPage({viewport:{width:1280,height:900},colorScheme:scheme});
  await p.goto(url); await p.waitForTimeout(200);
  const res=await p.evaluate(()=>{
    const parse=(c)=>{const m=c.match(/[\d.]+/g).map(Number);return {r:m[0],g:m[1],b:m[2],a:m[3]===undefined?1:m[3]};};
    const lum=({r,g,b})=>{const f=(v)=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b);};
    const over=(fg,bg)=>({r:fg.r*fg.a+bg.r*(1-fg.a),g:fg.g*fg.a+bg.g*(1-fg.a),b:fg.b*fg.a+bg.b*(1-fg.a),a:1});
    const bgOf=(el)=>{let cur=el,acc=null;
      while(cur){const c=parse(getComputedStyle(cur).backgroundColor);
        if(c.a>0){acc=acc?over(acc,c):c; if(c.a===1)break;}
        cur=cur.parentElement;}
      return acc||{r:255,g:255,b:255,a:1};};
    const ratio=(a,bb)=>{const l1=lum(a),l2=lum(bb);return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);};
    const bad=[];
    for(const el of document.querySelectorAll("main *")){
      const txt=[...el.childNodes].filter(n=>n.nodeType===3&&n.textContent.trim()).map(n=>n.textContent.trim()).join(" ");
      if(!txt) continue;
      const cs=getComputedStyle(el);
      if(cs.visibility==="hidden"||cs.display==="none"||parseFloat(cs.opacity)<0.5) continue;
      const r=el.getBoundingClientRect(); if(r.width<1||r.height<1) continue;
      const fg=over(parse(cs.color),bgOf(el));
      const size=parseFloat(cs.fontSize), wgt=parseInt(cs.fontWeight)||400;
      const large=size>=24||(size>=18.66&&wgt>=700);
      const need=large?3:4.5;
      const cr=ratio(fg,bgOf(el));
      if(cr<need) bad.push({sel:el.tagName.toLowerCase()+"."+[...el.classList].join("."),txt:txt.slice(0,52),px:+size.toFixed(1),w:wgt,cr:+cr.toFixed(2),need});
    }
    for(const el of document.querySelectorAll("main input[placeholder]")){
      const cs=getComputedStyle(el,"::placeholder"); const r=el.getBoundingClientRect(); if(r.width<1) continue;
      const fg=over(parse(cs.color),bgOf(el)); const cr=ratio(fg,bgOf(el));
      if(cr<4.5) bad.push({sel:"input::placeholder",txt:el.placeholder.slice(0,40),px:+parseFloat(cs.fontSize).toFixed(1),w:400,cr:+cr.toFixed(2),need:4.5});
    }
    // tap targets
    const sels=[".btn",".ref",".queue .act",".rail .later-slot",".rail a.place",".rail a.project",".filters .f",".filters input",".tabs span",".card",".row",".piece",".done",".src .act",".card .pills .act",".also .act",".ear input","details summary",".menu .opt",".in",".sheet .foot .btn","table.list td div","table.list .grip",".queue summary",".head .switcher",".rail a.place.foot"];
    const targets=[];
    for(const s of sels) for(const el of document.querySelectorAll(s)){
      const r=el.getBoundingClientRect(); if(r.width<1) continue;
      targets.push({sel:s,txt:(el.textContent||"").trim().slice(0,28),w:+r.width.toFixed(1),h:+r.height.toFixed(1)});
    }
    const small=targets.filter(t=>t.h<24||t.w<24);
    const under44=targets.filter(t=>t.h<44);
    const byselMin={};
    for(const t of under44){ const k=t.sel; if(!byselMin[k]||t.h<byselMin[k].h) byselMin[k]=t; }
    return {bad,small,under44min:Object.values(byselMin)};
  });
  // dedupe contrast findings
  const seen=new Map();
  for(const f of res.bad){const k=f.sel+"|"+f.cr; if(!seen.has(k))seen.set(k,f);}
  console.log("=== "+scheme+" contrast failures ("+seen.size+" distinct) ===");
  console.log(JSON.stringify([...seen.values()],null,1));
  console.log("=== "+scheme+" targets < 24px ===");
  console.log(JSON.stringify(res.small.slice(0,20),null,1));
  console.log("=== "+scheme+" smallest per selector (<44px) ===");
  console.log(JSON.stringify(res.under44min,null,1));
  await p.close();
}
await b.close();
