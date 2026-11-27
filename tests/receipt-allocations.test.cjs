const {test}=require('node:test');
const assert=require('node:assert/strict');
const {receiptSubmission}=require('../static/hq-receipt-allocations.js');
const payload=()=>({id:'allocation-1',operation_id:'operation-1',revision:0,amount_pence:2000,evidence_ref:''});
const response=(status,body)=>({status,ok:status>=200&&status<300,redirected:false,json:async()=>body});

test('lost response after commit retries identical ID and token, including no-reference payments',async()=>{
 const submission=receiptSubmission();
 const initial=submission.prepare(payload), ledger=new Map();
 const post=async p=>{
  if(!ledger.has(p.operation_id)){ledger.set(p.operation_id,p);throw Error('connection lost after commit')}
  assert.deepEqual(p,ledger.get(p.operation_id));
  return response(200,{saved:true,revision:1});
 };
 assert.equal((await submission.send(post)).kind,'uncertain');
 assert.equal(submission.pending,true);
 assert.equal(submission.prepare(()=>({...payload(),id:'another',operation_id:'another'})),initial);
 assert.equal((await submission.send(post)).kind,'saved');
 assert.equal(ledger.size,1);assert.equal(submission.pending,false);
});
test('redirect, expired CSRF, malformed success and server errors retain pending token',async()=>{
 const submission=receiptSubmission(), initial=submission.prepare(payload);
 for(const r of [{...response(200,{}),redirected:true},response(403,{error:'expired'}),response(503,{}),response(200,{}),{...response(200,{}),json:async()=>{throw Error('invalid JSON')}}]){
  assert.equal((await submission.send(async p=>{assert.equal(p,initial);return r})).kind,'uncertain');
  assert.equal(submission.pending,true);
 }
 assert.equal((await submission.send(async()=>response(200,{saved:true,revision:1}))).kind,'saved');
});
test('definitive validation and stale rejection permit a corrected new operation',async()=>{
 const submission=receiptSubmission();
 for(const status of [400,409]){
  submission.prepare(payload);
  assert.deepEqual(await submission.send(async()=>response(status,{error:'Rejected'})),{kind:'rejected',error:'Rejected'});
  assert.equal(submission.pending,false);
 }
 const corrected=submission.prepare(()=>({...payload(),operation_id:'corrected'}));
 assert.equal(corrected.operation_id,'corrected');
});
