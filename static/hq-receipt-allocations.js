(()=>{'use strict';
const $=id=>document.getElementById(id), form=$('allocation'), notice=$('notice');
let state=null, saving=false, dirty=false;
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
 form.reset();form.elements.id.value='';form.elements.revision.value='0';
 form.elements.invoice_id.disabled=false;form.elements.amount_pounds.readOnly=false;
 form.querySelector('button[type=submit]').disabled=false;
 invoiceOptions();limit();dirty=false;
}
function edit(r){
 const invoice=$('invoice');if(![...invoice.options].some(o=>o.value===r.invoice_id)){
  const opt=node('option',r.invoice_ref);opt.value=r.invoice_id;invoice.append(opt);
 }
 for(const k of ['id','revision','invoice_id','received_on','method','evidence_ref','memo','status'])form.elements[k].value=r[k]??'';
 form.elements.amount_pounds.value=(r.amount_pence/100).toFixed(2);
 form.elements.amount_pounds.readOnly=true;invoice.disabled=true;
 form.querySelector('button[type=submit]').disabled=r.status==='reversed';
 limit();dirty=false;form.scrollIntoView({block:'start'});
}
async function load(){
 const response=await fetch('/api/hq/finance/receipt-allocations',{credentials:'same-origin',cache:'no-store'});
 if(response.redirected||!response.ok)throw Error('Sign into HQ to access allocation records');
 state=await response.json();clear();
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
  }
  records.append(card);
 }
 if(!state.allocations.length)records.append(node('p','No allocation reports yet.'));
 const history=$('history');history.replaceChildren();
 for(const event of state.history){
  const r=JSON.parse(event.snapshot),card=node('article','');
  card.append(node('p',gbp(r.amount_pence)+' · '+r.status+' · revision '+event.revision),
    node('small',event.occurred_at+' · allocation '+event.allocation_id.slice(0,8)));
  history.append(card);
 }
 if(!state.history.length)history.append(node('p','No allocation revisions recorded.'));
 notice.textContent='HQ allocation records loaded. No bank or payment-provider reconciliation performed.';
 dirty=false;
}
$('invoice').onchange=()=>{limit();dirty=true};
form.addEventListener('input',()=>dirty=true);
$('newRecord').onclick=clear;
form.onsubmit=async event=>{
 event.preventDefault();if(saving)return;saving=true;
 const fields=form.elements;
 try{
  const payload={
   id:fields.id.value||id(),operation_id:id(),revision:Number(fields.revision.value),
   invoice_id:fields.invoice_id.value,amount_pence:amount(fields.amount_pounds.value),
   received_on:fields.received_on.value,method:fields.method.value,
   evidence_ref:fields.evidence_ref.value,memo:fields.memo.value,status:fields.status.value
  };
  const res=await fetch('/api/hq/finance/receipt-allocations',{method:'POST',
   credentials:'same-origin',headers:{'Content-Type':'application/json',
   'X-HQ-CSRF':document.querySelector('meta[name=hq-csrf]').content},
   body:JSON.stringify(payload)});
  if(res.redirected)throw Error('Owner session expired');
  const result=await res.json();
  if(!res.ok)throw Error(result.error||'No save confirmed');
  await load();
  notice.textContent='Allocation saved and reloaded from HQ. No external payment, message or booking occurred.';
 }catch(error){notice.textContent=error.message}finally{saving=false}
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
load().catch(error=>{notice.textContent=error.message});
})();