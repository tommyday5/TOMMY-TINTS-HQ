const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/job-timer.js', 'utf8');
function timer(saved = null, justSaved = false) {
  let now = 1000000, tick;
  const ids = ['jobCockpit','allowance','activeClock','clockPercent','allowanceRing','clockRemaining','clockWarning','clockState','pauseClock','clockStart','clockPause','clockFinish','clockNotice','minutes','timeEntryForm'];
  const nodes = Object.fromEntries(ids.map(id => [id, {dataset:{},style:{setProperty(){}},setAttribute(){},addEventListener(event, fn){this[event]=fn},scrollIntoView(){},focus(){}}]));
  nodes.jobCockpit.dataset = {work:'job-1', recorded:'30', editable:'yes', saved:justSaved?'yes':'no'};
  let storage = saved;
  vm.runInNewContext(source, {document:{getElementById:id=>nodes[id],addEventListener(){}},localStorage:{getItem:()=>storage,setItem:(_,value)=>{storage=value},removeItem:()=>{storage=null}},Date:{now:()=>now},setInterval:fn=>{tick=fn}});
  return {nodes, advance(seconds){now+=seconds*1000;tick()}, stored:()=>JSON.parse(storage)};
}
test('counts saved work and active time, excludes breaks, reviews only current session',()=>{
  const t=timer(), n=t.nodes;
  n.allowance.value='1';n.allowance.onchange();
  assert.equal(n.clockPercent.textContent,'50%');
  n.clockStart.onclick();t.advance(120);n.clockPause.onclick();
  assert.equal(n.activeClock.textContent,'00:32:00');
  t.advance(300);assert.equal(n.activeClock.textContent,'00:32:00');
  n.clockStart.onclick();t.advance(60);n.clockPause.onclick();n.clockFinish.onclick();
  assert.equal(n.minutes.value,3);assert.match(n.pauseClock.textContent,/00:05:00/);
  assert.equal(n.clockFinish.disabled,false);
});
test('restores running sessions and only clears after confirmed reviewed save',()=>{
  const t=timer();t.nodes.clockStart.onclick();t.advance(60);
  const pending=t.stored();assert.equal(pending.start,1000000);
  const restored=timer(JSON.stringify(pending));assert.equal(restored.nodes.clockState.textContent,'IN PROGRESS');
  restored.nodes.timeEntryForm.submit();
  const confirmed=timer(JSON.stringify({...restored.stored(),seconds:60}),true);
  assert.equal(confirmed.nodes.activeClock.textContent,'00:30:00');
  assert.equal(confirmed.nodes.clockState.textContent,'STANDBY');
});
test('invalid stored data cannot inflate time; unknown allowance stays unknown',()=>{
  const t=timer(JSON.stringify({seconds:-100,start:null,paused:0,pauseAt:null}));
  assert.equal(t.nodes.activeClock.textContent,'00:30:00');
  assert.equal(t.nodes.clockPercent.textContent,'—');
  assert.equal(t.nodes.clockFinish.disabled,true);
});

function persistentClock({seconds=0,recorded=0,running='yes',allowance=100,paused='no',breakSeconds=0,live=false,reply=null}={}) {
  let now=1000000,foreground,fetches=0;
  const ticks={},button={disabled:false};
  const nodes={serverTimer:{dataset:{seconds:String(seconds),recorded:String(recorded),running,allowance:String(allowance),paused,break:String(breakSeconds),statusUrl:live?'/hq/test/status':'',workRevision:'2',timerRevision:'0',sessionId:'synthetic'},querySelectorAll:()=>[button]},serverClock:{},serverAllowanceNotice:{},serverSyncNotice:{},serverReload:{hidden:true},serverTotalClock:{},serverBreakClock:{},serverPercent:{},serverAllowanceRing:{style:{setProperty(){}},setAttribute(){},removeAttribute(){}}};
  vm.runInNewContext(source,{document:{getElementById:id=>nodes[id],hidden:false,addEventListener:(_,fn)=>{foreground=fn}},performance:{now:()=>now},window:{addEventListener(){}},setInterval:(fn,delay)=>{ticks[delay]=fn},fetch:async()=>{fetches++;if(reply instanceof Error)throw reply;return {ok:true,json:async()=>reply}}});
  return {nodes,button,wake(seconds){now+=seconds*1000;foreground()},tick(){ticks[1000]()},poll(){ticks[15000]()},setReply(value){reply=value},fetchCount:()=>fetches};
}
test('persistent timer catches up after device sleep without counting paused breaks',()=>{
  const running=persistentClock({seconds:60});running.wake(3600);
  assert.equal(running.nodes.serverClock.textContent,'01:01:00');
  const paused=persistentClock({seconds:60,running:'no'});paused.wake(3600);
  assert.equal(paused.nodes.serverClock.textContent,'00:01:00');
});
test('persistent allowance matches recovery queue 80 percent and exact limit',()=>{
  const t=persistentClock({recorded:79,running:'yes'});
  assert.doesNotMatch(t.nodes.serverAllowanceNotice.textContent,/Approaching/);
  t.wake(60);assert.match(t.nodes.serverAllowanceNotice.textContent,/Approaching labour allowance/);
  t.wake(20*60);assert.match(t.nodes.serverAllowanceNotice.textContent,/Allowance reached/);
  t.wake(60);assert.match(t.nodes.serverAllowanceNotice.textContent,/Allowance exceeded by 00:01:00/);
});

test('persistent cumulative clock includes saved labour once and pause clock excludes breaks',()=>{
  const t=persistentClock({seconds:120,recorded:30,running:'no',paused:'yes',breakSeconds:20,allowance:40});
  assert.equal(t.nodes.serverTotalClock.textContent,'00:32:00');
  assert.equal(t.nodes.serverPercent.textContent,'80%');
  t.wake(3600);
  assert.equal(t.nodes.serverClock.textContent,'00:02:00');
  assert.equal(t.nodes.serverTotalClock.textContent,'00:32:00');
  assert.equal(t.nodes.serverBreakClock.textContent,'01:00:20');
  const running=persistentClock({seconds:120,recorded:30,breakSeconds:20,allowance:40});
  running.wake(480);
  assert.equal(running.nodes.serverTotalClock.textContent,'00:40:00');
  assert.equal(running.nodes.serverBreakClock.textContent,'00:00:20');
  assert.equal(running.nodes.serverPercent.textContent,'100%');
  const unknown=persistentClock({allowance:0});
  assert.equal(unknown.nodes.serverPercent.textContent,'—');
});


function costPreview({minutes='30',rate='18.25',saved=0,unknown=0,max=1440}={}) {
  const input=value=>({value,addEventListener(event,fn){this[event]=fn}});
  const nodes={labourCostPreview:{dataset:{savedCost:String(saved),unknownRates:String(unknown),maxMinutes:String(max)},hidden:true},
    minutes:input(minutes),rate:input(rate),entryCostPreview:{},totalCostPreview:{}};
  vm.runInNewContext(source,{document:{getElementById:id=>nodes[id]}});
  return nodes;
}
test('labour preview rounds each entry exactly like SQLite, and adds saved costs',()=>{
  const n=costPreview({saved:1500});
  assert.equal(n.labourCostPreview.hidden,false);
  assert.equal(n.entryCostPreview.textContent,'£9.13');
  assert.equal(n.totalCostPreview.textContent,'£24.13');
  n.minutes.value='60';n.minutes.input();
  assert.equal(n.totalCostPreview.textContent,'£33.25');
  n.rate.value='0';n.rate.input();
  assert.equal(n.entryCostPreview.textContent,'£0.00');
  assert.equal(n.totalCostPreview.textContent,'£15.00');
});
test('labour preview keeps missing rates unknown and rejects invalid or excessive minutes/rates',()=>{
  const n=costPreview({unknown:1,max:30});
  assert.equal(n.entryCostPreview.textContent,'£9.13');
  assert.match(n.totalCostPreview.textContent,/Unknown/);
  for(const value of ['','18.255','-1','1e2','10000.01']){
    n.rate.value=value;n.rate.input();
    assert.doesNotMatch(n.entryCostPreview.textContent,/^£/);
    assert.match(n.totalCostPreview.textContent,/Unknown/);
  }
  n.rate.value='18.25';n.minutes.value='31';n.minutes.input();
  assert.match(n.entryCostPreview.textContent,/valid reviewed minutes/);
  assert.match(n.totalCostPreview.textContent,/Unknown/);
});


test('foreground verification refreshes clock and stops stale display after another device changes timer',async()=>{
  const reply={work_revision:2,timer_revision:0,session_id:'synthetic',saved_minutes:30,active_seconds:90,break_seconds:0};
  const t=persistentClock({recorded:30,seconds:60,live:true,reply});
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();
  assert.equal(t.nodes.serverClock.textContent,'00:01:30');
  assert.match(t.nodes.serverSyncNotice.textContent,/checked against server/);
  t.setReply({...reply,timer_revision:1});
  t.wake(60);await flush();
  assert.match(t.nodes.serverSyncNotice.textContent,/changed on another screen/);
  assert.equal(t.nodes.serverReload.hidden,false);
  assert.equal(t.button.disabled,true);
  const frozen=t.nodes.serverClock.textContent;
  t.wake(600);t.tick();assert.equal(t.nodes.serverClock.textContent,frozen);
  assert.equal(t.fetchCount(),2);
});
test('failed live verification identifies estimate and retries without changing records',async()=>{
  const t=persistentClock({live:true,reply:new Error('offline')});
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();
  assert.match(t.nodes.serverSyncNotice.textContent,/estimated from the last known/);
  assert.equal(t.button.disabled,false);
  t.setReply({work_revision:2,timer_revision:0,session_id:'synthetic',saved_minutes:0,active_seconds:42,break_seconds:0});
  t.poll();await flush();
  assert.equal(t.nodes.serverClock.textContent,'00:00:42');
  assert.match(t.nodes.serverSyncNotice.textContent,/checked against server/);
});
