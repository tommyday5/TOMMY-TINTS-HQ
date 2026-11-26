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

function persistentClock({seconds=0,recorded=0,running='yes',allowance=100}={}) {
  let now=1000000,tick,foreground;
  const nodes={serverTimer:{dataset:{seconds:String(seconds),recorded:String(recorded),running,allowance:String(allowance)}},serverClock:{},serverAllowanceNotice:{}};
  const script='(()=>{const root=document.getElementById(\'serverTimer\');'+source.split("(()=>{const root=document.getElementById('serverTimer');")[1];
  vm.runInNewContext(script,{document:{getElementById:id=>nodes[id],hidden:false,addEventListener:(_,fn)=>{foreground=fn}},Date:{now:()=>now},setInterval:fn=>{tick=fn}});
  return {nodes,wake(seconds){now+=seconds*1000;foreground()},tick(){tick()}};
}
test('persistent timer catches up after device sleep without counting paused breaks',()=>{
  const running=persistentClock({seconds:60});running.wake(3600);
  assert.equal(running.nodes.serverClock.textContent,'01:01:00');
  const paused=persistentClock({seconds:60,running:'no'});paused.wake(3600);
  assert.equal(paused.nodes.serverClock.textContent,'00:01:00');
});
test('persistent allowance distinguishes 75, 90, and 100 percent including saved labour',()=>{
  const t=persistentClock({recorded:75,running:'yes'});
  assert.match(t.nodes.serverAllowanceNotice.textContent,/75%/);
  t.wake(15*60);assert.match(t.nodes.serverAllowanceNotice.textContent,/90%/);
  t.wake(10*60);assert.match(t.nodes.serverAllowanceNotice.textContent,/Allowance reached/);
  t.wake(60);assert.match(t.nodes.serverAllowanceNotice.textContent,/00:01:00 over/);
});
