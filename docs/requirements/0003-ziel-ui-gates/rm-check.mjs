import { chromium } from "playwright";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
const url=pathToFileURL(`${dir}/${process.env.MOCK||"0003-ziel-ui-mockup-v8.html"}`).href;
const b=await chromium.launch();
for (const rm of ["reduce","no-preference"]) {
  const p=await b.newPage({viewport:{width:1280,height:900},reducedMotion:rm});
  await p.goto(url); await p.waitForTimeout(200);
  const r=await p.evaluate(()=>{
    const moving=[];
    for(const el of document.querySelectorAll("main *")){
      for(const pseudo of [null,"::after","::before"]){
        const cs=getComputedStyle(el,pseudo);
        if(cs.animationName && cs.animationName!=="none")
          moving.push((el.tagName.toLowerCase()+"."+[...el.classList].join("."))+(pseudo||"")+" → "+cs.animationName);
      }
    }
    // per frame count
    const perFrame=[];
    document.querySelectorAll(".frame,.strip,.sheets").forEach((f,i)=>{
      let n=0;
      for(const el of f.querySelectorAll("*")) for(const ps of [null,"::after"]){
        const cs=getComputedStyle(el,ps); if(cs.animationName&&cs.animationName!=="none") n++;
      }
      const t=f.previousElementSibling&&f.previousElementSibling.textContent?f.previousElementSibling.textContent.trim().slice(0,40):"";
      perFrame.push({frame:i+1,title:t,moving:n});
    });
    return {moving,perFrame};
  });
  console.log("### reducedMotion="+rm+" total moving="+r.moving.length);
  console.log(JSON.stringify([...new Set(r.moving)],null,0));
  if(rm==="no-preference") console.log(JSON.stringify(r.perFrame.filter(f=>f.moving!==1),null,0));
  await p.close();
}
await b.close();
