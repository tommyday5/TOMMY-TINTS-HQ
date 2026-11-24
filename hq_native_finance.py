"""Owner-entered finance planning and transaction review; never bank verification."""
import json, re, sqlite3, uuid
from datetime import date, datetime, timezone
from flask import Blueprint, jsonify, render_template, request, session
from hq_auth import required
from customer_request_store import connect

finance=Blueprint('native_finance',__name__)
CATEGORIES=('unclassified','customer_income','materials','fuel','insurance','rent_utilities','equipment','subscriptions','vehicle','director_drawings','director_repayment','internal_transfer','loan','creditor_payment','other')

def schema(db):
 db.execute('CREATE TABLE IF NOT EXISTS finance_plan (id INTEGER PRIMARY KEY CHECK(id=1), balance INTEGER, balance_date TEXT, target INTEGER, revision INTEGER NOT NULL DEFAULT 0)')
 db.execute('CREATE TABLE IF NOT EXISTS finance_bills (id TEXT PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL, due TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')
 db.execute('CREATE TABLE IF NOT EXISTS finance_transactions (id TEXT PRIMARY KEY, day TEXT NOT NULL, description TEXT NOT NULL, amount INTEGER NOT NULL, category TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0)')
 db.execute('CREATE TABLE IF NOT EXISTS finance_edit_audit (id TEXT PRIMARY KEY, entity TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL)')
 # Additive migration: preserve every existing bill, identity, value and revision.
 columns={r['name'] for r in db.execute('PRAGMA table_info(finance_bills)')}
 for name,definition in [('status',"TEXT NOT NULL DEFAULT 'planned'"),('status_date','TEXT'),('archived','INTEGER NOT NULL DEFAULT 0')]:
  if name not in columns:db.execute('ALTER TABLE finance_bills ADD COLUMN '+name+' '+definition)
 db.execute('CREATE TABLE IF NOT EXISTS finance_bill_history (id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(bill_id,revision))')

def money(v,nullable=False,signed=False):
 if nullable and v is None:return None
 if type(v) is not int or abs(v)>1000000000 or (not signed and v<0):raise ValueError('Amounts must be integer pence within limits')
 return v

def day(v):
 if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v):raise ValueError('Use a valid date')
 date.fromisoformat(v);return v

def text(v):
 if not isinstance(v,str) or not 1<=len(v.strip())<=160:raise ValueError('Enter text of 1–160 characters')
 return v.strip()

def audit(db,entity,ref,revision):
 db.execute('INSERT INTO finance_edit_audit VALUES(?,?,?,?,?)',(uuid.uuid4().hex,entity,ref,revision,datetime.now(timezone.utc).isoformat()))

def snapshot():
 with connect() as db:
  schema(db)
  plan=db.execute('SELECT * FROM finance_plan WHERE id=1').fetchone()
  bills=[dict(r) for r in db.execute('SELECT * FROM finance_bills ORDER BY archived,due,id')]
  bill_history=[dict(id=r['id'],bill_id=r['bill_id'],revision=r['revision'],snapshot=json.loads(r['snapshot']),created=r['created']) for r in db.execute('SELECT * FROM finance_bill_history ORDER BY created DESC,id DESC LIMIT 250')]
  transactions=[dict(r) for r in db.execute('SELECT * FROM finance_transactions ORDER BY day DESC,id LIMIT 200')]
  count=db.execute('SELECT COUNT(*) FROM finance_transactions').fetchone()[0]
 return dict(plan=dict(plan) if plan else dict(balance=None,balance_date=None,target=None,revision=0),bills=bills,bill_history=bill_history,transactions=transactions,transaction_count=count,categories=CATEGORIES,bank_verified=False,source='Owner-entered HQ records',received_income=None)

@finance.get('/hq/finance/command')
@required
def page():return render_template('finance_command.html',csrf=session.get('csrf','')),200,{'Cache-Control':'no-store'}

@finance.route('/api/hq/finance/native',methods=['GET','POST'])
@required
def api():
 if request.method=='GET':return jsonify(snapshot()),200,{'Cache-Control':'no-store'}
 import secrets
 if not secrets.compare_digest(request.headers.get('X-HQ-CSRF',''),session.get('csrf','not-set')):return jsonify(error='Invalid security token'),403
 if not request.is_json or not request.content_length or request.content_length>4096:return jsonify(error='Invalid request'),400
 try:
  d=request.get_json(silent=True)
  if not isinstance(d,dict):raise ValueError('Invalid request')
  kind=d.get('kind');rev=d.get('revision')
  if type(rev) is not int or rev<0:raise ValueError('Revision required')
  with connect() as db:
   schema(db);split_tables(db);db.execute('BEGIN IMMEDIATE')
   if kind=='plan':
    balance=money(d.get('balance'),True,True);target=money(d.get('target'),True)
    stamp=day(d.get('balance_date')) if balance is not None else None
    row=db.execute('SELECT * FROM finance_plan WHERE id=1').fetchone()
    if row and row['revision']!=rev:return jsonify(error='Changed elsewhere. Reload before editing.'),409
    if not row and rev!=0:return jsonify(error='Changed elsewhere. Reload.'),409
    db.execute('INSERT OR REPLACE INTO finance_plan VALUES(1,?,?,?,?)',(balance,stamp,target,rev+1));ref='1'
   elif kind in ('bill','transaction'):
    ref=d.get('id')
    if not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{32}',ref):raise ValueError('Invalid record ID')
    table='finance_bills' if kind=='bill' else 'finance_transactions'
    row=db.execute('SELECT * FROM '+table+' WHERE id=?',(ref,)).fetchone()
    if row and row['revision']!=rev:return jsonify(error='Changed elsewhere. Reload before editing.'),409
    if not row and rev!=0:return jsonify(error='Changed elsewhere. Reload.'),409
    if kind=='bill':
     name=text(d.get('name'));amount=money(d.get('amount'));due=day(d.get('due'))
     status=d.get('status',row['status'] if row else 'planned')
     if status not in ('planned','reported_paid','cancelled'):raise ValueError('Unknown bill status')
     archived=d.get('archived',bool(row['archived']) if row else False)
     if type(archived) is not bool:raise ValueError('Archive flag must be boolean')
     # Owner-reported paid is NOT a verified bank settlement.
     status_date=day(d.get('status_date')) if status=='reported_paid' else None
     db.execute('INSERT OR REPLACE INTO finance_bills (id,name,amount,due,revision,status,status_date,archived) VALUES(?,?,?,?,?,?,?,?)',
      (ref,name,amount,due,rev+1,status,status_date,int(archived)))
     bill_state=dict(name=name,amount=amount,due=due,status=status,status_date=status_date,archived=archived)
     db.execute('INSERT INTO finance_bill_history (id,bill_id,revision,snapshot,created) VALUES(?,?,?,?,?)',
      (uuid.uuid4().hex,ref,rev+1,json.dumps(bill_state,sort_keys=True),datetime.now(timezone.utc).isoformat()))
    else:
     if row and split_active(db,ref):return jsonify(error='Withdraw the active split before changing this transaction.'),409
     category=d.get('category');reviewed=d.get('reviewed',False)
     if category not in CATEGORIES or type(reviewed) is not bool:raise ValueError('Choose a valid classification')
     if reviewed and category=='unclassified':raise ValueError('Classify before marking reviewed')
     values=(day(d.get('day')),text(d.get('description')),money(d.get('amount'),signed=True),category,int(reviewed))
     db.execute('INSERT OR REPLACE INTO finance_transactions VALUES(?,?,?,?,?,?,?)',(ref,*values,rev+1))
   else:raise ValueError('Invalid record type')
   audit(db,kind,ref,rev+1)
  return jsonify(saved=True,bank_verified=False)
 except (ValueError,TypeError):return jsonify(error='Check amounts, dates and required fields. Review does not verify a bank receipt.'),400
 except sqlite3.Error:return jsonify(error='Finance storage unavailable. Nothing confirmed saved.'),503


# Additive owner-only receivables routes. Do not merge invoice values with receipts.
from hq_receivables import register as register_receivable_routes
register_receivable_routes(finance)

# Additive owner-only reported-payment allocation interface; no bank verification.
from hq_receivable_payments import register as register_allocation_routes
register_allocation_routes(finance)

# Additive owner-only native transaction split classifications.
from hq_finance_splits import register as register_splits, tables as split_tables, active as split_active
register_splits(finance)

# Owner-approved classifications of existing read-only Monzo worksheet evidence.
from hq_monzo_sheet_classifications import register as register_monzo_classifications
register_monzo_classifications(finance)
