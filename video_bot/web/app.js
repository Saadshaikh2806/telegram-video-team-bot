'use strict';
const $ = id => document.getElementById(id);
let model = null, filter = 'open', busy = false, pending = null, requestNumber = 0, toastTimer;
const labels = {queued:'Waiting for editor',dispatching:'Sending assignment',assigned:'Ready to start',editing:'Editing',submitted:'Needs review',revision:'Changes requested',approved:'Complete',cancelled:'Cancelled'};
const titles = {open:'In progress',mine:'My tasks',review:'Needs review',closed:'Completed & cancelled',team:'Editor availability'};
function el(tag, className, text) { const n=document.createElement(tag); if(className)n.className=className; if(text!==undefined)n.textContent=text; return n; }
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,7000); }
async function api(path, data) {
  const response=await fetch(path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:data===undefined?{}:{'Content-Type':'application/json','X-Dashboard':'1'},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){ if(response.status===401)showLogin(); throw new Error(result.error||'Could not save. Please try again.'); }
  return result;
}
function showLogin(){model=null;$('workspace').hidden=true;$('loading').hidden=true;$('login').hidden=false;$('identity').replaceChildren();}
function link(text,url,primary=false){const a=el('a',primary?'primary':'secondary',text);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}
function button(text,fn,primary=false){const b=el('button',primary?'primary':'secondary',text);b.type='button';b.disabled=busy;b.onclick=fn;return b;}
function render(){
  if(!model)return;
  $('loading').hidden=true;$('login').hidden=true;$('workspace').hidden=false;
  const admin=model.user.role==='admin';
  const name=el('span','',admin?'Admin workspace':model.user.name);
  const logout=button('Sign out',async()=>{try{await api('/api/logout',{});showLogin();}catch(e){toast(e.message);}});logout.className='quiet';
  $('identity').replaceChildren(name,logout);$('role-label').textContent=admin?'TEAM OVERVIEW':'YOUR WORKSPACE';
  $('subtitle').textContent=admin?'Keep your team moving. Review edits and clear the next step.':'A clear next step for every video assigned to you.';
  $('team-tab').hidden=!admin;$('test-notice').hidden=!(model.test_mode||model.demo);
  $('test-notice').textContent=model.demo?'Demo workspace · Sample data only. Changes here do not affect your team.':'Solo test mode · New assignments go only to the test admin.';
  $('upload-link').hidden=!model.upload_url;if(model.upload_url)$('upload-link').href=model.upload_url;
  const active=model.jobs.filter(j=>!['approved','cancelled'].includes(j.status));
  const overdue=active.filter(j=>['assigned','editing','revision'].includes(j.status)&&j.due&&j.due*1000<Date.now());
  const stats=[['Open videos',active.length,'Moving toward delivery'],['In editing',active.filter(j=>['editing','revision'].includes(j.status)).length,'Work in motion'],['Needs review',active.filter(j=>j.status==='submitted').length,'Ready for a decision'],['Overdue',overdue.length,'May need a little help']];
  $('stats').replaceChildren(...stats.map(([label,num,note])=>{const n=el('div','stat');n.append(el('div','stat-label',label),el('div','stat-number',String(num)),el('div','stat-note',note));return n;}));
  for(const b of $('filters').querySelectorAll('button'))b.classList.toggle('selected',b.dataset.filter===filter);
  $('list-title').textContent=titles[filter];
  const query=$('search').value.trim().toLowerCase();
  let cards=[];
  if(filter==='team'){
    cards=model.editors.filter(e=>e.name.toLowerCase().includes(query)).map(e=>{
      const card=el('article','card');card.append(el('span','badge',e.available?'Available':'Paused'),el('h2','',e.name));
      card.append(el('p','',`${active.filter(j=>j.editor_id===e.id).length} open videos`));
      card.append(button(e.available?'Pause new assignments':'Resume assignments',()=>perform({action:'availability',editor_id:e.id,available:!e.available}),!e.available));return card;
    });
  }else{
    const jobs=model.jobs.filter(j=>{
      const closed=['approved','cancelled'].includes(j.status);
      return (filter==='closed'?closed:filter==='review'?j.status==='submitted':filter==='mine'?!closed&&j.editor_id===model.user.id:!closed)&&
        (`VID-${String(j.id).padStart(4,'0')} ${j.brief} ${j.editor_name||''}`).toLowerCase().includes(query);
    });
    cards=jobs.map(j=>{
      const card=el('article','card'),top=el('div','card-top');top.append(el('span','video-id',`VID-${String(j.id).padStart(4,'0')}`),el('span',`badge ${j.status}`,labels[j.status]));card.append(top,el('h3','brief',j.brief));
      if(j.feedback)card.append(el('p','feedback',`Changes requested: ${j.feedback}`));
      const meta=el('div','card-meta');meta.append(el('span','',j.editor_name||'Awaiting assignment'),el('span','',`Effort ${j.effort} / 3`));card.append(meta);
      const late=overdue.some(x=>x.id===j.id);card.append(el('div',`due${late?' late':''}`,j.due?`${late?'Overdue · ':''}Due ${new Date(j.due*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}`:'Deadline starts when delivered'));
      const actions=el('div','actions'),own=j.editor_id===model.user.id;
      if(own&&['assigned','revision'].includes(j.status))actions.append(button('Start editing',()=>perform({action:'start_job',job_id:j.id,version:j.version}),true));
      if(admin&&j.status==='submitted'){
        actions.append(button('Approve edit',()=>openDialog(j,'approve'),true));
        if(j.review_url)actions.append(link('Review in Telegram ↗',j.review_url));
      }else if(j.file_url)actions.append(link('Open video ↗',j.file_url));
      if(own&&['assigned','editing','revision'].includes(j.status)){
        actions.append(button('Submit in Telegram ↗',()=>{toast('Reply to the assignment video in Telegram with your finished file. It will appear here for review.');if(j.file_url)window.open(j.file_url,'_blank','noopener,noreferrer');}));
        actions.append(button('Need help',()=>openDialog(j,'block')));
      }
      card.append(actions);
      if(admin&&!['approved','cancelled','dispatching'].includes(j.status)){
        const more=el('div','admin-actions');
        const options=j.status==='submitted'?[['revise','Request changes']]:['assigned','editing','revision'].includes(j.status)?[['extend','More time']]:[];
        if(j.editor_id)options.push(['unassign','Change editor']);options.push(['cancel','Cancel video']);
        for(const [action,title]of options){const b=button(title,()=>openDialog(j,action));b.className='quiet';more.append(b);}card.append(more);
      }
      return card;
    });
  }
  $('cards').replaceChildren(...cards);$('empty').hidden=cards.length!==0;
  $('limit-note').textContent=model.jobs.length>=model.limit?'Showing the first 300 videos, with open work first.':'Updates automatically · Times shown in your timezone';
}
async function refresh(silent=false){
  if(busy)return;
  const n=++requestNumber;
  try{const data=await api('/api/state');if(n!==requestNumber)return;
    const signature=value=>JSON.stringify({...value,now:0});
    const changed=!model||signature(model)!==signature(data);model=data;if(changed)render();$('sync').textContent='Up to date';}
  catch(e){$('sync').textContent='Connection interrupted';if(!silent||model)toast(e.message);}
}
async function perform(data){
  if(busy)return;busy=true;++requestNumber;render();$('dialog-confirm').disabled=true;$('dialog-confirm').textContent='Saving…';
  try{model=await api('/api/action',data);$('action-dialog').close();toast('Saved. Telegram will sync in the background.');$('sync').textContent='Saved just now';}
  catch(e){toast(e.message);}
  finally{busy=false;$('dialog-confirm').disabled=false;$('dialog-confirm').textContent='Confirm';render();}
}
function openDialog(job,action){
  pending={job_id:job.id,version:job.version,action};
  const copy={approve:['Approve this edit?','The finished video will be sent to the Uploaders group.'],revise:['Request changes','Describe what the editor should change. A new revision deadline will start.'],unassign:['Change editor','This returns the video to the queue for a different editor. If no one else is available, it stays queued.'],extend:['Give a little more time','Add hours to the current deadline.'],cancel:['Cancel this video?','The job will close. Files already in Telegram will remain there.'],block:['Ask for help','Tell your admin what is blocking the edit. Your deadline stays the same until they extend it.']};
  $('dialog-job').textContent=`VID-${String(job.id).padStart(4,'0')}`;$('dialog-title').textContent=copy[action][0];$('dialog-copy').textContent=copy[action][1];
  $('hours-label').hidden=action!=='extend';$('hours').required=action==='extend';$('reason-label').hidden=action==='approve';$('reason').required=action!=='approve';$('reason').value='';$('hours').value='24';$('action-dialog').showModal();
}
$('filters').onclick=e=>{const b=e.target.closest('[data-filter]');if(b){filter=b.dataset.filter;render();}};
$('search').oninput=render;$('refresh').onclick=()=>refresh();$('dialog-cancel').onclick=()=>$('action-dialog').close();
$('action-form').onsubmit=e=>{e.preventDefault();perform({...pending,reason:$('reason').value,hours:Number($('hours').value)});};
async function init(){
  const token=new URLSearchParams(location.hash.slice(1)).get('login');
  if(token){history.replaceState(null,'',location.pathname);try{await api('/api/login',{token});}catch(e){showLogin();toast(e.message);}}
  try{const info=await api('/api/info');if(/^[A-Za-z0-9_]+$/.test(info.bot)){$('bot-login').href=`https://t.me/${info.bot}?start=dashboard`;$('bot-login').hidden=false;$('login-fallback').hidden=true;}}catch(e){/* Manual sign-in instructions remain available. */}
  await refresh(true);$('loading').hidden=true;if(!model)showLogin();
  setInterval(()=>{if(model&&!document.hidden&&!$('action-dialog').open)refresh(true);},12000);
  document.addEventListener('visibilitychange',()=>{if(model&&!document.hidden)refresh(true);});
}
init();
