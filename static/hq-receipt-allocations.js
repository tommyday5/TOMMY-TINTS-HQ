// Keep the same allocation ID and operation token after an uncertain response.
// This state is intentionally in memory, not browser storage.
function receiptSubmission(){
 let pending=null;
 return {
  get pending(){return pending!==null},
  prepare(makePayload){if(!pending)pending=Object.freeze(makePayload());return pending},
  async send(post){
   if(!pending)throw Error('No allocation prepared');
   try{
    const response=await post(pending);
    if(response.redirected||response.status===403||response.status>=500)
     return {kind:'uncertain'};
    const result=await response.json();
    if(response.ok&&result.saved===true&&Number.isInteger(result.revision)){
     pending=null;return {kind:'saved'};
    }
    if([400,409].includes(response.status)&&typeof result.error==='string'){
     pending=null;return {kind:'rejected',error:result.error};
    }
    return {kind:'uncertain'};
   }catch(error){return {kind:'uncertain'}}
  }
 };
}
if(typeof module!=='undefined'&&module.exports)module.exports={receiptSubmission};
if(typeof document!=='undefined')
(()=>{'use strict';
const $=id=>document.getElementById(id), form=$('allocation'), notice=$('notice');
let state=null, saving=false, searching=false, dirty=false, refreshRequired=false, activeQuery='';
const submission=receiptSubmission(), submit=form.querySelector('button[type=submit]');
let lockedControls=[];
function freeze(){
 if(lockedControls.length)return;
 lockedControls=[...form.querySelectorAll('input,select,textarea,button')].filter(el=>el!==submit).map(el=>[el,el.disabled]);
 for(const [el] of lockedControls)el.disabled=true;
}
function thaw(){for(const [el,disabled] of lockedControls)el.disabled=disabled;lockedControls=[];submit.textContent='Save reported allocation in HQ';}
const gbp=p=>new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP'}).format(p/100);
const node=(tag,content)=>{const el=document.createElement(tag);el.textContent=content;return el};
const id=()=>crypto.randomUUID().replaceAll('-','');
function amount(value){
 if(!/^(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?$/.test(value))throw Error('Enter a positive amount in pounds to two decimal places');
 const [p,d='']=value.split('.');
 const result=Number(p)*100+Number(d.padEnd(2,'0'));
 if(result<1||result>1000000000)throw Error('Amount out of range');
 return result;
}
function invoiceOptions(){
 const select=$('invoice');select.replaceChildren();
 const empty=node('option','Choose an issued HQ invoice');empty.value='';select.append(empty);
 for(const r of state.invoices.filter(v=>!v.archived&&['issued','disputed'].includes(v.status))){
  const o=node('option',r.invoice_ref+' · '+r.customer_name+' · '+gbp(r.amount_pence));
  o.value=r.id;select.append(o);
 }
}
function limit(){
 const r=state?.invoices.find(r=>r.id===$('invoice').value);
 $('allocation-limit').textContent=r?'Owner-reported unallocated portion: '+gbp(r.unallocated_owner_reported_pence)+' (not bank verified)':'Choose an issued invoice.';
}
function clear(){
 if(submission.pending||saving||searching||refreshRequired)return;
 form.reset();form.elements.id.value='';form.elements.revision.value='0';
 form.elements.invoice_id.disabled=false;form.elements.amount_pounds.readOnly=false;
 form.querySelector('button[type=submit]').disabled=false;
 invoiceOptions();limit();dirty=false;
}
function edit(r){
 if(submission.pending||saving||searching||refreshRequired)return;
 const invoice=$('invoice');if(![...invoice.options].some(o=>o.value===r.invoice_id)){
  const opt=node('option',r.invoice_ref);opt.value=r.invoice_id;invoice.append(opt);
 }
 for(const k of ['id','revision','invoice_id','received_on','method','evidence_ref','memo','status'])form.elements[k].value=r[k]??'';
 form.elements.amount_pounds.value=(r.amount_pence/100).toFixed(2);
 form.elements.amount_pounds.readOnly=true;invoice.disabled=true;
 form.querySelector('button[type=submit]').disabled=r.status==='reversed';
 limit();dirty=false;form.scrollIntoView({block:'start'});
}
async function load(query=activeQuery){
 const base='/api/hq/finance/receipt-allocations';
 const options={credentials:'same-origin',cache:'no-store'};
 const response=await fetch(query?base+'/search':base,query?{...options,method:'POST',
  headers:{'Content-Type':'application/json','X-HQ-CSRF':document.querySelector('meta[name=hq-csrf]').content},
  body:JSON.stringify({query})}:options);
 if(response.redirected||!response.ok)throw Error('Sign into HQ to access allocation records');
 state=await response.json();activeQuery=query;
 $('allocation-query').value=query;
 $('allocation-search-status').textContent=(query?'Search results: ':'Recent records: ')+
  state.invoices.length+' invoices · '+state.allocations.length+' allocations'+
  (state.display_limited?' · Display limit reached; refine by exact reference or record ID to find older entries.':'');
 // Only clear the editor after a successful response.
 const wasSaving=saving,wasSearching=searching;saving=false;searching=false;thaw();clear();saving=wasSaving;searching=wasSearching;
 const records=$('records');records.replaceChildren();
 for(const r of state.allocations){
  const card=node('article','');
  card.append(node('h2',r.invoice_ref+' · '+gbp(r.amount_pence)),
    node('p',r.status+' · '+r.received_on+' · '+r.method),
    node('p','Reference: '+(r.evidence_ref||'None supplied')+' · revision '+r.revision),
    node('small','Owner reported, not independently verified or reconciled'));
  const btn=node('button',r.status==='reversed'?'View reversed entry':'Edit / reverse');
  btn.type='button';btn.onclick=()=>edit(r);card.append(btn);
  if(r.method==='card_reported'){
   const link=node('a','Check / view exact Square payment evidence');
   link.href='/hq/finance/square-invoice-evidence?allocation='+encodeURIComponent(r.id);
   const line=node('p','');line.append(link);card.append(line);
   const recognition=node('a','Review provider-confirmed receipt and fees');
   recognition.href='/hq/finance/provider-confirmed-receipts?allocation='+encodeURIComponent(r.id);
   const reviewLine=node('p','');reviewLine.append(recognition);card.append(reviewLine);
   const settlement=node('a','Review single-payment settlement proposal');
   settlement.href='/hq/finance/settlement-review?allocation='+encodeURIComponent(r.id);
   const settlementLine=node('p','');settlementLine.append(settlement);card.append(settlementLine);
  }
  records.append(card);
 }
 if(!state.allocations.length)records.append(node('p',query?'No allocations match this search.':'No allocation reports yet.'));
 const history=$('history');history.replaceChildren();
 for(const event of state.history){
  const r=JSON.parse(event.snapshot),card=node('article','');
  card.append(node('p',gbp(r.amount_pence)+' · '+r.status+' · revision '+event.revision),
    node('small',event.occurred_at+' · allocation '+event.allocation_id.slice(0,8)));
  history.append(card);
 }
 if(!state.history.length)history.append(node('p',query?'No matching allocation revisions.':'No allocation revisions recorded.'));
 notice.textContent='HQ allocation records loaded. No bank or payment-provider reconciliation performed.';
 dirty=false;
}
async function runSearch(query){
 if(saving||searching||submission.pending||refreshRequired||dirty){
  $('allocation-search-status').textContent='Resolve or discard the current allocation edit before changing the search.';
  return;
 }
 searching=true;const previousDisabled=submit.disabled;freeze();submit.disabled=true;
 try{await load(query.trim())}
 catch(error){thaw();submit.disabled=previousDisabled;$('allocation-search-status').textContent='Search not completed. Existing records are unchanged.'}
 finally{searching=false}
}
$('allocation-search').onsubmit=event=>{event.preventDefault();runSearch($('allocation-query').value)};
$('allocation-search-reset').onclick=()=>runSearch('');
$('invoice').onchange=()=>{limit();dirty=true};
form.addEventListener('input',()=>dirty=true);
$('newRecord').onclick=clear;
form.onsubmit=async event=>{
 event.preventDefault();if(saving||searching||refreshRequired||!state)return;
 const fields=form.elements;
 try{
  submission.prepare(()=>({
   id:fields.id.value||id(),operation_id:id(),revision:Number(fields.revision.value),
   invoice_id:fields.invoice_id.value,amount_pence:amount(fields.amount_pounds.value),
   received_on:fields.received_on.value,method:fields.method.value,
   evidence_ref:fields.evidence_ref.value,memo:fields.memo.value,status:fields.status.value
  }));
 }catch(error){notice.textContent=error.message;return}
 saving=true;dirty=true;freeze();submit.disabled=true;
 try{
  const outcome=await submission.send(payload=>fetch('/api/hq/finance/receipt-allocations',{method:'POST',
   credentials:'same-origin',headers:{'Content-Type':'application/json',
   'X-HQ-CSRF':document.querySelector('meta[name=hq-csrf]').content},
   body:JSON.stringify(payload)}));
  if(outcome.kind==='uncertain'){
   submit.textContent='Retry same allocation safely';
   notice.textContent='Save result unknown. Retry this same allocation without changing its details. If you leave this page, check saved allocations before recording it again.';
   submit.disabled=false;return;
  }
  if(outcome.kind==='rejected'){
   thaw();submit.disabled=false;notice.textContent=outcome.error;return;
  }
  // A successful save and an unsuccessful refresh are different outcomes.
  // Do not allow another submission from the stale form.
  dirty=false;submit.disabled=true;
  try{
   await load();
   notice.textContent='Allocation saved and reloaded from HQ. No external payment, message or booking occurred.';
  }catch(error){
   refreshRequired=true;freeze();submit.disabled=true;
   notice.textContent='Allocation saved in HQ, but the refresh failed. Reload this page to review it; do not record it again.';
  }
 }finally{saving=false}
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
runSearch('').catch(error=>{notice.textContent=error.message});
})();