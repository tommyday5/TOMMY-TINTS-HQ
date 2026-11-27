(()=>{'use strict';const root=document.getElementById('jobCockpit');if(!root)return;const $=id=>document.getElementById(id),key='hq-job-session-v1-'+root.dataset.work,recorded=Number(root.dataset.recorded)*60;let state={seconds:0,start:null,paused:0,pauseAt:null,estimate:'',pendingSave:false};try{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved&&Number.isFinite(saved.seconds)&&saved.seconds>=0&&saved.seconds<=86400&&[saved.start,saved.pauseAt].every(x=>x===null||(Number.isFinite(x)&&x>0&&x<=Date.now()))&&Number.isFinite(saved.paused)&&saved.paused>=0)state={...state,...saved}}catch{}const server=$('serverTimer'),legacy=$('legacyTimer');if(server&&legacy){if(server.dataset.hasSession==='yes'||(!state.start&&!state.seconds&&!state.pendingSave)){legacy.hidden=true;return}legacy.open=true;const serverForm=server.querySelector('form');if(serverForm)serverForm.hidden=true;$('clockNotice').textContent='Recover the previous device session before starting a persistent timer.'}if(root.dataset.saved==='yes'&&state.pendingSave){try{localStorage.removeItem(key)}catch{}state={seconds:0,start:null,paused:0,pauseAt:null,estimate:state.estimate,pendingSave:false}}$('allowance').value=state.estimate;const clock=()=>Math.max(0,Math.floor((Date.now()-(state.start||Date.now()))/1000))+state.seconds;const format=s=>[Math.floor(s/3600),Math.floor(s%3600/60),Math.floor(s%60)].map(v=>String(v).padStart(2,'0')).join(':');function persist(){try{localStorage.setItem(key,JSON.stringify(state))}catch{$('clockNotice').textContent='Device storage unavailable. Review and save your time before leaving.'}}function render(){const total=recorded+clock(),estimate=Number(state.estimate)*3600,valid=Number.isFinite(estimate)&&estimate>0&&estimate<=1800000,ratio=valid?total/estimate:0;$('activeClock').textContent=format(total);$('clockPercent').textContent=valid?Math.floor(ratio*100)+'%':'—';$('allowanceRing').style.setProperty('--used',Math.min(100,ratio*100)+'%');$('allowanceRing').setAttribute('aria-valuenow',String(Math.min(100,Math.floor(ratio*100))));$('clockRemaining').textContent=!valid?'Estimate not recorded':ratio>=1?format(total-estimate)+' over allowance':format(estimate-total)+' remaining';$('clockWarning').textContent=!valid?'Enter the job’s estimated active hours; no allowance has been assumed.':ratio>=1?'Time allowance reached. Review remaining scope before continuing.':ratio>=.75?'Time allowance approaching. Review remaining work before continuing.':'Within the entered allowance. No price or job status is changed by timing.';$('clockState').textContent=root.dataset.editable!=='yes'?'READ ONLY':state.start?'IN PROGRESS':clock()>0?'PAUSED':'STANDBY';$('pauseClock').textContent='Current session paused time '+format(state.paused+(state.pauseAt?Math.max(0,(Date.now()-state.pauseAt)/1000):0))+' · excluded';if($('clockStart')){$('clockStart').hidden=!!state.start;$('clockPause').hidden=!state.start;$('clockFinish').disabled=clock()<60||!!state.start}$('allowance').disabled=!!state.start}
function pause(){if(state.start){state.seconds=clock();state.start=null;state.pauseAt=Date.now();persist();render()}}
if($('clockStart')){$('clockStart').onclick=()=>{if(!state.start){if(state.pauseAt)state.paused+=Math.max(0,(Date.now()-state.pauseAt)/1000);state.pauseAt=null;state.start=Date.now();state.pendingSave=false;persist();render()}};$('clockPause').onclick=pause;$('clockFinish').onclick=()=>{const minutes=Math.floor(clock()/60);if(minutes<1||minutes>1440){$('clockNotice').textContent='Review sessions in entries of 1–1440 whole minutes.';return}$('minutes').value=minutes;$('clockNotice').textContent='Review '+minutes+' whole active minutes below, select the activity, then Save actual time. Remaining seconds are not included. Job is not marked complete.';$('timeEntryForm').scrollIntoView({behavior:'auto',block:'start'});$('minutes').focus()};$('timeEntryForm').addEventListener('submit',()=>{pause();state.pendingSave=true;persist()})}$('allowance').onchange=()=>{state.estimate=$('allowance').value;persist();render()};document.addEventListener('visibilitychange',()=>{persist();if(!document.hidden)render()});setInterval(render,1000);render()})();

(()=>{'use strict';
const root=document.getElementById('serverTimer');if(!root)return;
const get=id=>document.getElementById(id),clock=get('serverClock'),totalClock=get('serverTotalClock'),breakClock=get('serverBreakClock'),ring=get('serverAllowanceRing'),percent=get('serverPercent'),notice=get('serverAllowanceNotice'),syncNotice=get('serverSyncNotice'),reload=get('serverReload');
let initial=Number(root.dataset.seconds),initialBreak=Number(root.dataset.break||0),started=performance.now(),stale=false,pending=false;
const recorded=Number(root.dataset.recorded)*60,allowance=Number(root.dataset.allowance)*60;
const format=s=>[Math.floor(s/3600),Math.floor(s%3600/60),Math.floor(s%60)].map(v=>String(v).padStart(2,'0')).join(':');
function render(){
  if(stale)return;
  const elapsed=Math.max(0,Math.floor((performance.now()-started)/1000)),seconds=initial+(root.dataset.running==='yes'?elapsed:0),breaks=initialBreak+(root.dataset.paused==='yes'?elapsed:0),total=seconds+recorded,ratio=allowance>0?total/allowance:0;
  clock.textContent=format(seconds);if(totalClock)totalClock.textContent=format(total);if(breakClock)breakClock.textContent=format(breaks);
  if(percent)percent.textContent=allowance>0?Math.floor(ratio*100)+'%':'—';
  if(ring){ring.style.setProperty('--used',Math.min(100,ratio*100)+'%');if(allowance>0){ring.setAttribute('aria-valuenow',String(Math.min(100,Math.floor(ratio*100))));ring.setAttribute('aria-valuetext',Math.floor(ratio*100)+' percent used')}else{ring.removeAttribute('aria-valuenow');ring.setAttribute('aria-valuetext','Allowance unknown')}}
  const message=allowance<=0?'No allowance recorded.':total>allowance?'Allowance exceeded by '+format(total-allowance)+'. Review scope before continuing.':total===allowance?'Allowance reached. Review scope before continuing.':(total*5>=allowance*4?'Approaching labour allowance · ':'')+format(allowance-total)+' remaining';
  if(notice.textContent!==message)notice.textContent=message;
}
function setNotice(message){if(syncNotice&&syncNotice.textContent!==message)syncNotice.textContent=message}
function markStale(){
  stale=true;notice.textContent='Clock paused on this screen: server records changed.';
  setNotice('This job or timer changed on another screen. Reload before making timer changes. The server timer itself has not been paused.');
  if(reload)reload.hidden=false;
  for(const button of root.querySelectorAll('form button'))button.disabled=true;
}
async function refresh(){
  if(stale||pending||document.hidden||!root.dataset.statusUrl)return;
  pending=true;
  try{
    const response=await fetch(root.dataset.statusUrl,{credentials:'same-origin',cache:'no-store',redirect:'error',headers:{Accept:'application/json'}});
    if(!response.ok)throw Error('status unavailable');
    const data=await response.json();
    if(!Number.isSafeInteger(data.work_revision)||!Number.isSafeInteger(data.timer_revision)||!Number.isSafeInteger(data.saved_minutes)||!Number.isSafeInteger(data.active_seconds)||!Number.isSafeInteger(data.break_seconds)||data.active_seconds<0||data.break_seconds<0||data.saved_minutes<0)throw Error('invalid status');
    if(data.work_revision!==Number(root.dataset.workRevision)||data.timer_revision!==Number(root.dataset.timerRevision)||(data.session_id||'')!==root.dataset.sessionId||data.saved_minutes*60!==recorded){markStale();return}
    initial=data.active_seconds;initialBreak=data.break_seconds;started=performance.now();
    setNotice('Clock checked against server. No background push is sent.');render();
  }catch{
    setNotice('Live verification unavailable. Display is estimated from the last known server state; reconnect or sign in and reload to verify.');
  }finally{pending=false}
}
render();refresh();setInterval(render,1000);setInterval(refresh,15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){render();refresh()}});
window.addEventListener('pageshow',event=>{if(event.persisted)refresh()});
})();


;(()=>{'use strict';
  const panel=document.getElementById('labourCostPreview');
  if(!panel)return;
  const minutes=document.getElementById('minutes'),rate=document.getElementById('rate');
  const entry=document.getElementById('entryCostPreview'),total=document.getElementById('totalCostPreview');
  const saved=Number(panel.dataset.savedCost),unknown=Number(panel.dataset.unknownRates),max=Number(panel.dataset.maxMinutes);
  const currency=pence=>'£'+(Math.floor(pence/100))+'.'+String(pence%100).padStart(2,'0');
  function render(){
    const rawMinutes=String(minutes.value),rawRate=String(rate.value);
    const m=Number(rawMinutes);
    if(!/^[0-9]{1,4}$/.test(rawMinutes)||m<1||m>max){
      entry.textContent='Enter valid reviewed minutes';total.textContent='Unknown';return;
    }
    if(!rawRate){entry.textContent='Unknown — no internal rate entered';total.textContent='Unknown';return;}
    if(!/^[0-9]{1,5}(?:\.[0-9]{1,2})?$/.test(rawRate)){
      entry.textContent='Enter a valid pounds-and-pence rate';total.textContent='Unknown';return;
    }
    const [whole,fraction='']=rawRate.split('.');
    const pence=Number(whole)*100+Number(fraction.padEnd(2,'0'));
    if(pence>1000000){entry.textContent='Enter a rate no higher than £10000';total.textContent='Unknown';return;}
    // Same half-up per-entry rounding as the authoritative SQLite ledger.
    const cost=Math.floor((m*pence+30)/60);
    entry.textContent=currency(cost);
    total.textContent=unknown===0&&Number.isSafeInteger(saved)&&saved>=0&&Number.isSafeInteger(saved+cost)?currency(saved+cost):'Unknown — previous labour rates missing';
  }
  for(const input of [minutes,rate])for(const event of ['input','change','focus'])input.addEventListener(event,render);
  panel.hidden=false;render();
})();
