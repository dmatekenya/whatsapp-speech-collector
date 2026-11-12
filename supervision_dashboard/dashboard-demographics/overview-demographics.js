/* Masakhane admin Overview — public-safe demographic breakdowns. */
(()=>{
  'use strict';
  const ID='masakhane-dashboard-demographics';
  const URL='dashboard-demographics/api.php';
  const NAMES=[['Gender','gender'],['Country','country'],['Age group','age_group'],['First language','first_language']];
  const COLORS=['#6258f6','#41bca7','#6ba2f4','#f0af4c','#9d90d0','#d9849a','#8aa96e','#6aadb1'];
  const h=(ms)=>(Math.max(0,Number(ms)||0)/3600000).toLocaleString('en-GB',{minimumFractionDigits:3,maximumFractionDigits:3});
  const pct=(ms,total)=>total>0?Math.max(0,100*(Number(ms)||0)/total):0;
  const node=(tag,cls,value)=>{const x=document.createElement(tag);if(cls)x.className=cls;if(value!==undefined)x.textContent=String(value);return x;};
  let busy=false, last='', lastRoot=null;
  function card(label,raw,total,countrySource){
    const item=node('article','mdemo-v8-card');
    item.append(node('h3','mdemo-v8-card-title',label));
    if(label==='Country'&&countrySource==='derived_from_district') item.append(node('p','mdemo-v8-small','Country derived from known Malawi districts; remaining areas unverified.'));
    const rows=Array.isArray(raw)?raw.filter(x=>Number(x.duration_ms)>0):[];
    if(!rows.length){item.append(node('p','mdemo-v8-info','No eligible recordings currently available.'));return item;}
    const ring=node('div','mdemo-v8-donut');const inner=node('div','mdemo-v8-middle');
    inner.append(node('strong','',`${h(total)} h`),node('small','','recorded duration'));ring.append(inner);
    let angle=0;const stops=[];const legend=node('div','mdemo-v8-legend');
    rows.forEach((r,i)=>{
      const end=Math.min(100,angle+pct(r.duration_ms,total));
      const color=/unverified|unknown|not provided/i.test(String(r.name))?'#afb9c6':COLORS[i%COLORS.length];
      stops.push(`${color} ${angle.toFixed(4)}% ${end.toFixed(4)}%`);angle=end;
      const row=node('div','mdemo-v8-line'),swatch=node('i','mdemo-v8-swatch');swatch.style.backgroundColor=color;
      row.append(swatch,node('span','mdemo-v8-name',`${r.name} (${pct(r.duration_ms,total).toFixed(1)}%)`),node('b','mdemo-v8-number',`${h(r.duration_ms)} h`));legend.append(row);
    });
    if(angle<100) stops.push(`#e6e9f1 ${angle.toFixed(4)}% 100%`);
    ring.style.background=`conic-gradient(${stops.join(',')})`;
    ring.setAttribute('role','img');ring.setAttribute('aria-label',`${label}: ${h(total)} recorded hours`);
    item.append(ring,legend);return item;
  }
  function render(root,data){
    const grid=root.querySelector('.mdemo-v8-grid');if(!grid)return;
    const total=Number(data.total_ms)||0;
    const frag=document.createDocumentFragment();
    NAMES.forEach(([label,key])=>frag.append(card(label,data.dimensions?.[key],total,data.country_source)));
    grid.replaceChildren(frag);
  }
  async function update(){
    if(busy||document.hidden)return;
    const root=document.getElementById(ID);
    if(!root)return; // including after a route or page refresh
    busy=true;
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),7000);
    try{
      const r=await fetch(`${URL}?_=${Date.now()}`,{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'},signal:controller.signal});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const data=await r.json();
      if(data.status!=='live'||!(Number(data.total_ms)>0)||!data.dimensions)throw new Error('Data not ready');
      const fingerprint=JSON.stringify([data.total_ms,data.country_source,data.dimensions]);
      if(fingerprint!==last||root!==lastRoot||!root.querySelector('.mdemo-v8-card')){render(root,data);last=fingerprint;lastRoot=root;}
    }catch(_){
      if(!root.querySelector('.mdemo-v8-card')){
        const grid=root.querySelector('.mdemo-v8-grid');
        if(grid)grid.replaceChildren(node('p','mdemo-v8-info','Demographic breakdowns are temporarily unavailable. Production QC remains unaffected.'));
      }
    }finally{clearTimeout(timeout);busy=false;}
  }
  document.addEventListener('DOMContentLoaded',()=>{update();setInterval(update,5000);window.addEventListener('focus',update);window.addEventListener('online',update);document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});});
})();
