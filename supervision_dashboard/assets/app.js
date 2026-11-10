(()=>{'use strict';const root=document.documentElement;
const theme=localStorage.getItem('masakhane_theme');if(theme==='dark')root.dataset.theme='dark';
const sidebar=document.getElementById('sidebar');const scrim=document.querySelector('[data-scrim]');
document.querySelector('[data-sidebar-toggle]')?.addEventListener('click',()=>{if(window.matchMedia('(max-width:850px)').matches){sidebar?.classList.toggle('opened');scrim?.classList.toggle('visible');}else{root.classList.toggle('sidebar-mini');localStorage.setItem('masakhane_sidebar',root.classList.contains('sidebar-mini')?'mini':'full');}});
scrim?.addEventListener('click',()=>{sidebar?.classList.remove('opened');scrim?.classList.remove('visible');});
if(localStorage.getItem('masakhane_sidebar')==='mini')root.classList.add('sidebar-mini');
const darkToggle=document.querySelector('[data-dark-toggle]');darkToggle?.addEventListener('click',()=>{root.dataset.theme=root.dataset.theme==='dark'?'light':'dark';localStorage.setItem('masakhane_theme',root.dataset.theme);});
const refreshStatus=()=>{fetch('index.php?page=api_status',{credentials:'same-origin',cache:'no-store'}).then(r=>r.ok?r.json():null).then(data=>{if(!data)return;document.querySelectorAll('.badge-count,.unread-pill').forEach(el=>{el.hidden=!data.unread;el.textContent=data.unread>99?'99+':String(data.unread);});}).catch(()=>{});};
// Notification state is now updated by the authenticated five-second live loop below.
document.querySelectorAll('a.disabled').forEach(a=>a.addEventListener('click',e=>e.preventDefault()));

// Profile menu: keyboard accessible, closes on outside click and Escape.
const accountToggle=document.querySelector('[data-account-toggle]');
const accountMenu=document.getElementById('account-menu');
function closeAccount(){if(!accountMenu||!accountToggle)return;accountMenu.hidden=true;accountToggle.setAttribute('aria-expanded','false');}
accountToggle?.addEventListener('click',()=>{if(!accountMenu)return;const open=accountMenu.hidden;accountMenu.hidden=!open;accountToggle.setAttribute('aria-expanded',String(open));});
document.addEventListener('click',e=>{if(!e.target.closest('.account-dropdown'))closeAccount();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeAccount();});
document.querySelectorAll('[data-password-toggle]').forEach(btn=>btn.addEventListener('click',()=>{const field=document.getElementById(btn.dataset.passwordToggle);if(!field)return;const show=field.type==='password';field.type=show?'text':'password';btn.textContent=show?'Hide':'Show';btn.setAttribute('aria-label',`${show?'Hide':'Show'} password`);}));
const profileForm=document.getElementById('profile-photo-form');
if(profileForm){
 const fileInput=document.getElementById('profile-photo-input');
 const box=document.getElementById('photo-crop-box');
 const canvas=document.getElementById('profile-crop-canvas');
 const slider=document.getElementById('photo-zoom');
 const feedback=document.getElementById('photo-feedback');
 const ctx=canvas?.getContext('2d');
 let photo=null,offX=0,offY=0,zoom=1,drag=null,ready=false;
 const setMessage=(message)=>{if(feedback)feedback.textContent=message;};
 function drawCrop(){
  if(!ctx||!photo)return;
  const w=canvas.width,h=canvas.height;
  const scale=Math.max(w/photo.naturalWidth,h/photo.naturalHeight)*zoom;
  const dw=photo.naturalWidth*scale,dh=photo.naturalHeight*scale;
  offX=Math.min((dw-w)/2,Math.max(-(dw-w)/2,offX));
  offY=Math.min((dh-h)/2,Math.max(-(dh-h)/2,offY));
  ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);
  ctx.drawImage(photo,(w-dw)/2+offX,(h-dh)/2+offY,dw,dh);
 }
 fileInput?.addEventListener('change',()=>{
  ready=false;photo=null;box.hidden=true;setMessage('');
  const file=fileInput.files?.[0];if(!file)return;
  if(!['image/jpeg','image/png','image/webp'].includes(file.type)||file.size>2097152){setMessage('Choose a JPG, PNG or WebP image smaller than 2 MB.');fileInput.value='';return;}
  const url=URL.createObjectURL(file),img=new Image();
  img.onload=()=>{URL.revokeObjectURL(url);if(img.naturalWidth<120||img.naturalHeight<120){setMessage('Image is too small.');return;}
   photo=img;offX=0;offY=0;zoom=1;slider.value='100';box.hidden=false;ready=true;drawCrop();setMessage('Crop preview ready. Drag to reposition or adjust zoom.');};
  img.onerror=()=>{URL.revokeObjectURL(url);setMessage('Could not preview that photo. Choose another image.');};img.src=url;
 });
 slider?.addEventListener('input',()=>{zoom=Number(slider.value)/100;drawCrop();});
 canvas?.addEventListener('pointerdown',e=>{if(!ready)return;drag={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);});
 canvas?.addEventListener('pointermove',e=>{if(!drag||!ready)return;const rect=canvas.getBoundingClientRect();offX+=(e.clientX-drag.x)*canvas.width/rect.width;offY+=(e.clientY-drag.y)*canvas.height/rect.height;drag={x:e.clientX,y:e.clientY};drawCrop();});
 const endDrag=()=>{drag=null;};canvas?.addEventListener('pointerup',endDrag);canvas?.addEventListener('pointercancel',endDrag);
 profileForm.addEventListener('submit',e=>{
  if(!ready||!photo||!ctx||!window.fetch||!canvas.toBlob)return; // normal upload fallback
  e.preventDefault();setMessage('Saving your photo...');
  canvas.toBlob(async blob=>{if(!blob){setMessage('Could not prepare the photo.');return;}
   const payload=new FormData(profileForm);payload.set('profile_photo',blob,'cropped-profile.jpg');
   try {const response=await fetch(profileForm.action,{method:'POST',body:payload,credentials:'same-origin',redirect:'follow'});if(response.ok){window.location.assign('index.php?page=profile');}else{setMessage('Could not save the image. Refresh and try again.');}}
   catch{setMessage('Network issue. Please try again.');}
  },'image/jpeg',0.9);
 });
}

// Silent, server-coordinated production updates every 5 seconds while online.
// All changes are fetched over the existing authenticated session. Only one
// request at a time can probe the gateway (server-side advisory lock).
const refreshMs=Math.max(5000,Math.min(60000,Number(document.querySelector('meta[name="poll-interval"]')?.content||5)*1000));
const liveToken=document.querySelector('meta[name="csrf-token"]')?.content;
const currentPage=document.querySelector('meta[name="current-page"]')?.content||'';
const syncState=document.querySelector('[data-sync-state]');
const mainContent=document.getElementById('main-content');
let observedRevision=document.querySelector('meta[name="live-revision"]')?.content||'';
let observedSync=document.querySelector('meta[name="last-synced"]')?.content||'';
let pendingMarkup=false, liveErrorCount=0, liveRunning=false, updatingMarkup=false, syncTimer=null;
let dirtyContent=false;
const protectedPages=new Set(['profile','supervisors','assignments','settings']);
mainContent?.addEventListener('input',event=>{if(event.target.closest('input:not([type="hidden"]),textarea,select,[contenteditable]'))dirtyContent=true;});
mainContent?.addEventListener('change',event=>{if(event.target.closest('input:not([type="hidden"]),textarea,select'))dirtyContent=true;});
function safeToUpdate(){
 if(protectedPages.has(currentPage)||dirtyContent||document.hidden)return false;
 if(mainContent?.contains(document.activeElement) && document.activeElement?.matches('input,textarea,select,[contenteditable="true"]'))return false;
 for(const audio of document.querySelectorAll('audio'))if(!audio.paused)return false;
 return true;
}
function scheduleLive(delay=refreshMs){clearTimeout(syncTimer);syncTimer=setTimeout(runLive,delay);}
async function quietlyRefreshContent(){
 if(updatingMarkup||!safeToUpdate())return false;
 updatingMarkup=true;
 try{
  const response=await fetch(location.href,{credentials:'same-origin',cache:'no-store',headers:{'Accept':'text/html'}});
  if(response.status===401||response.redirected){location.assign('index.php?page=login');return false;}
  if(!response.ok)throw Error('Page refresh returned '+response.status);
  const parsed=new DOMParser().parseFromString(await response.text(),'text/html');
  const next=parsed.querySelector('#main-content');
  if(!next||!mainContent)throw Error('Unexpected dashboard refresh content');
  const before=window.scrollY;
  if(currentPage==='voice'){
    // Do not interrupt an open audio player or discard a reviewer form. Only
    // update the official QC summary cards in-place.
    const existingCards=mainContent.querySelector('.metrics');
    const updatedCards=next.querySelector('.metrics');
    if(existingCards&&updatedCards)existingCards.replaceWith(document.importNode(updatedCards,true));
  }else{
    // Keeps the user's date/search filters (in URL), vertical position, and
    // active navigation. No full-page reload and no manual refresh banner.
    mainContent.replaceChildren(...Array.from(next.childNodes).map(n=>document.importNode(n,true)));
    window.scrollTo({top:before,behavior:'instant'});
  }
  return true;
 }catch(error){return false;}finally{updatingMarkup=false;}
}
async function runLive(){
 if(!liveToken||liveRunning||document.hidden||!navigator.onLine){scheduleLive(refreshMs);return;}
 liveRunning=true;
 try{
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),25000);
  let response;
  try{response=await fetch('index.php?page=live_refresh',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:new URLSearchParams({csrf:liveToken}),credentials:'same-origin',cache:'no-store',signal:controller.signal});}finally{clearTimeout(timeout);}
  if(response.status===401||response.redirected){location.assign('index.php?page=login');return;}
  if(!response.ok)throw Error('HTTP '+response.status);
  const data=await response.json();
  document.querySelectorAll('.badge-count,.unread-pill').forEach(el=>{el.hidden=!data.unread;el.textContent=data.unread>99?'99+':String(data.unread||0);});
  if(data.error)throw Error(data.error);
  liveErrorCount=0;
  if(syncState && data.last_sync_ml){syncState.textContent='Synced '+data.last_sync_ml;syncState.classList.remove('offline');}
  // Compare both revision AND synchronized time, including changes detected by
  // another browser while this tab was open.
  const rev=String(data.revision||'');const stamp=String(data.last_sync||'');
  if(data.changed||(rev&&rev!==observedRevision)||(stamp&&stamp!==observedSync))pendingMarkup=true;
  if(pendingMarkup && await quietlyRefreshContent()){
    observedRevision=rev;observedSync=stamp;pendingMarkup=false;
  }
 }catch(error){liveErrorCount++;if(syncState && liveErrorCount>=3)syncState.title='Connection to production is temporarily unavailable; showing last synchronized data';}
 finally{
  liveRunning=false;
  scheduleLive(liveErrorCount?Math.min(60000,refreshMs*2**Math.min(3,liveErrorCount)):refreshMs);
 }
}
if(liveToken&&document.querySelector('.topbar'))scheduleLive(refreshMs);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&liveToken)scheduleLive(400);});

// Media preflight provides a visible reason instead of an inert 0:00/0:00 player.
const player=document.querySelector('[data-production-audio]');
if(player){
 const message=document.querySelector('[data-audio-message]');
 const retry=document.querySelector('[data-audio-retry]');
 const setAudioMessage=(str,isError=false)=>{if(message){message.textContent=str;message.classList.toggle('audio-error',isError);}};
 const audioSrc=player.getAttribute('src');
 let checked=false;
 async function checkAudio(){
  const params=new URL(audioSrc,location.href).searchParams;
  setAudioMessage('Checking private audio availability…');
  try{
   const response=await fetch('index.php?page=audio_status&id='+encodeURIComponent(params.get('id')||''),{credentials:'same-origin',cache:'no-store'});
   const data=await response.json();
   if(!response.ok||!data.available)throw new Error(data.message||'Audio is not available');
   checked=true;setAudioMessage('Audio is ready. Press Play to listen.');
   player.load();
  }catch(e){checked=false;setAudioMessage(e.message||'Private audio connection failed. Contact the administrator.',true);}
 }
 player.addEventListener('loadedmetadata',()=>setAudioMessage('Ready to play · '+Math.round(player.duration||0)+' seconds'));
 player.addEventListener('error',()=>setAudioMessage('The audio source could not be decoded or loaded. Ask the administrator to check the gateway file-access diagnostic.',true));
 retry?.addEventListener('click',()=>{player.pause();player.removeAttribute('src');player.load();player.setAttribute('src',audioSrc);checkAudio();});
 checkAudio();
}
})();

// Bulk contributor assignment interface: client-side convenience only; server revalidates all permissions.
(()=>{
 const form=document.getElementById('bulk-assignment-form');if(!form)return;
 const cards=[...form.querySelectorAll('[data-assignment-card]')];
 const search=form.querySelector('[data-assignment-search]');
 const all=form.querySelector('[data-assignment-select-all]');
 const count=form.querySelector('[data-assignment-count]');
 const checks=[...form.querySelectorAll('[data-assignment-checkbox]')];
 const update=()=>{const n=checks.filter(x=>x.checked).length;if(count)count.textContent=n+' selected';
    for(const box of checks)box.closest('.assignment-card')?.classList.toggle('chosen',box.checked);};
 search?.addEventListener('input',()=>{const q=search.value.trim().toLowerCase();for(const card of cards)card.hidden=!card.dataset.pid?.includes(q);});
 all?.addEventListener('change',()=>{for(const card of cards)if(!card.hidden){const box=card.querySelector('input[type=checkbox]');if(box)box.checked=all.checked;}update();});
 for(const box of checks)box.addEventListener('change',update);
 form.addEventListener('submit',e=>{if(!checks.some(x=>x.checked)){e.preventDefault();alert('Select at least one contributor.');return;}
   if(!confirm('Assign selected contributors exclusively to this supervisor? Existing assignments for these contributors will be replaced.'))e.preventDefault();});
 update();
})();
