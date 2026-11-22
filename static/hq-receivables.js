(()=>{'use strict';
const $=id=>document.getElementById(id), form=$('receivable'), notice=$('notice');
let state=null, saving=false, dirty=false;
const pounds=p=>new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP'}).format(p/100);
const node=(tag,value)=>{const el=document.createElement(tag);el.textContent=value;return el};
const options=(select,rows,placeholder,label)=>{select.replaceChildren();const blank=node('option',placeholder);blank.value='';select.append(blank);for(const r of rows){const option=node('option',label(r));option.value=r.id;select.append(option)}};
const worksFor=(customerId)=>state.works.filter(w=>w.customer_id===customerId);
function workOptions(selected=''){
 options($('work'),worksFor($('customer').value),'No linked HQ work',w=>w.service+' · '+w.status+' · '+w.id.slice(0,8));
 if(selected&&[...$('work').options].some(o=>o.value===selected))$('work').value=selected;
}
function clear(){form.reset();form.elements.id.value='';form.elements.revision.value='0';workOptions();dirty=false}
function edit(r){
 for(const field of ['id','revision','invoice_ref','issue_date','due_date','status','memo','customer_id'])form.elements[field].value=r[field]??'';
 form.elements.amount_pounds.value=(r.amount_pence/100).toFixed(2);
 form.elements.archived.checked=!!r.archived;
 workOptions(r.work_id||'');
 dirty=false;form.scrollIntoView({block:'start',behavior:'auto'});
}
async function load(){
 const response=await fetch('/api/hq/finance/receivables',{cache:'no-store',credentials:'same-origin'});
 if(response.redirected||!response.ok)throw Error('Sign into HQ to load your finance records');
 state=await response.json();
 options($('customer'),state.customers,'Choose a canonical HQ customer',c=>c.name+' · '+c.id.slice(0,8));
 clear();const records=$('records');records.replaceChildren();
 for(const r of state.receivables){
  const card=node('article','');
  card.append(node('h2',r.invoice_ref+' · '+r.customer_name),
    node('p',pounds(r.amount_pence)+' face amount · '+r.issue_date+' issued date · due '+r.due_date),
    node('p',r.status+' · '+(r.archived?'Archived':'Active')+
      (r.work_id?' · HQ work '+r.work_id.slice(0,8):' · no linked job')),
    node('small','Owner-entered · receipts unverified · outstanding balance unknown'));
  const button=node('button','Edit / archive');button.type='button';button.onclick=()=>edit(r);card.append(button);
  records.append(card);
 }
 if(!state.receivables.length)records.append(node('p','No receivables recorded in HQ yet.'));
 $('count').textContent='Showing '+state.receivables.length+' entered records. These are not verified bank receivables or received income.';
 const history=$('history');history.replaceChildren();
 for(const item of state.history){
  const r=JSON.parse(item.snapshot),card=node('article','');
  card.append(node('h2',r.invoice_ref+' · revision '+item.revision),
   node('p',pounds(r.amount_pence)+' · '+r.status+' · due '+r.due_date+(r.archived?' · archived':'')),
   node('small',item.occurred_at+' · '+item.record_id.slice(0,8)));
  history.append(card);
 }
 if(!state.history.length)history.append(node('p','No revision history yet.'));
 notice.textContent='HQ records loaded. Payment and bank reconciliation not connected.';
 dirty=false;
}
function toPence(value){
 if(!/^(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?$/.test(value))throw Error('Enter a valid positive amount with up to two decimal places');
 const [p,decimal='']=value.split('.');
 const amount=Number(p)*100+Number(decimal.padEnd(2,'0'));
 if(amount<1||amount>1000000000)throw Error('Invoice amount outside permitted range');
 return amount;
}
$('customer').onchange=()=>{workOptions();dirty=true};
form.addEventListener('input',()=>dirty=true);
$('newRecord').onclick=clear;
form.onsubmit=async event=>{
 event.preventDefault();if(saving)return;saving=true;
 const data=Object.fromEntries(new FormData(form));const id=data.id||crypto.randomUUID().replaceAll('-','');
 const payload={kind:'receivable',id,operation_id:crypto.randomUUID().replaceAll('-',''),
    revision:Number(data.revision),customer_id:data.customer_id,work_id:data.work_id||null,
    invoice_ref:data.invoice_ref,issue_date:data.issue_date,due_date:data.due_date,
    amount_pence:0,status:data.status,memo:data.memo||'',archived:form.elements.archived.checked};
 try{
  payload.amount_pence=toPence(data.amount_pounds);
  const response=await fetch('/api/hq/finance/receivables',{
   method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',
   'X-HQ-CSRF':document.querySelector('meta[name=hq-csrf]').content},body:JSON.stringify(payload)});
  if(response.redirected)throw Error('Owner session expired. Sign back into HQ.');
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'Could not save. No change confirmed.');
  await load();notice.textContent='Saved and reloaded from persistent HQ storage. No payment or external message sent.';
 }catch(error){notice.textContent=error.message}finally{saving=false}
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
load().catch(error=>{notice.textContent=error.message});
})();
