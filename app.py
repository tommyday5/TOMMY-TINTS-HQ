"""Tommy Tints HQ interactive preview. No customer data is served until auth is configured."""
import os, secrets, time, hmac, hashlib, sqlite3
import requests
from threading import Lock
from flask import Flask, jsonify, render_template, render_template_string, make_response, session, request, redirect, url_for
from quote_engine import calculate_quote
from enquiry_agent import analyse_enquiry
from hq_assistant import plan_request
from assistant_task_store import create_task, list_tasks, review_task
from assistant_memory import history as assistant_history, record_chat, clear_history, audit, audit_events
from hq_core_records import create_internal_work_record, list_work_records, search_work_records, set_draft_financials, change_work_status, read_work_record, StaleWorkRecordError, work_summary, create_test_job, delete_test_job
from customer_request_store import create_request as save_customer_request, list_requests as list_customer_requests, update_request_status, storage_ready, create_review_draft, list_review_drafts
from hq_openai import chat as openai_chat, configured as openai_configured, AssistantConfigurationError, AssistantUpstreamError
from hq_assistant_local import answer as local_hq_answer, LocalRecordsUnavailable
from materials_costing import calculate_materials
from job_material_store import read_log, write_log, StaleMaterialLogError
from job_profitability import profitability
from hq_job_costing import costing_snapshot
from hq_work_history import work_record_history
from job_negotiation import evaluate as evaluate_negotiation
from maintenance_planner import maintenance_data
from maintenance_economics import travel_charge, route_economics
from maintenance_maps import driving_matrix
from hq_maps_usage import reserve_route_request
from maintenance_suggestions import booking_suggestions
from maintenance_calendar import check_booking_window
from maintenance_calendar_google import live_conflicts
from hq_sheets_live import configured as sheets_configured, read_workbook, read_workbook_with_access_token
import hq_gmail_enquiries as gmail
import hq_square_bookings as square
import hq_square_payments as square_payments
from hq_payment_review import payment_review
from hq_transaction_preview import transaction_preview
from hq_integration_health import (PersistedSyncError, integration_status, record_check,
                                   record_committed_sync)
from hq_connection_readiness import build_readiness
from hq_workflow_readiness import build_workflow_readiness
from hq_version import ICON_VERSION, UPDATE_SCRIPT_VERSION, build_version
from hq_commerce import commerce
from hq_shop_enquiries import shop_enquiries
from hq_combined_baskets import combined_baskets
from hq_combined_quote_lines import combined_quote_lines
from hq_product_quote_lines import product_quote_lines
from hq_service_quotes import service_quotes
from hq_quote_pdf import service_quote_pdfs
from customer_edits import customer_edits
from hq_customer_directory import customer_directory
from hq_cost_library import cost_library
from hq_work_cost_estimates import estimate_workspace
from hq_customer_documents import customer_documents
from hq_customer_portal import customer_portal
from hq_customer_profile_requests import profile_requests
from hq_appointment_requests import availability_requests
from hq_native_work import native_work
from hq_native_maintenance import native_maintenance
from hq_maintenance_packages import maintenance_plans
from maintenance_visual import visual_routes
from hq_auth import auth, oauth, google, required, configured, active_session
from hq_passkeys import passkeys, registered_passkeys
from hq_google_connections import connection, save_connection, stored_grant_present
from hq_sheets_link_inspection import sheet_link_inspection
from hq_historical_preview import migration_preview
app = Flask(__name__)
app.secret_key = os.environ.get('HQ_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.environ.get('HQ_DEV_HTTP') != '1', SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID', '')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET', '')
oauth.init_app(app)
from hq_native_finance import finance as native_finance
app.register_blueprint(native_finance)
app.register_blueprint(auth)
app.register_blueprint(passkeys)
app.register_blueprint(visual_routes)
app.register_blueprint(customer_edits)
app.register_blueprint(customer_directory)
app.register_blueprint(cost_library)
app.register_blueprint(estimate_workspace)
app.register_blueprint(customer_documents)
app.register_blueprint(customer_portal)
app.register_blueprint(profile_requests)
app.register_blueprint(availability_requests)
app.register_blueprint(native_work)
app.register_blueprint(native_maintenance)
app.register_blueprint(maintenance_plans)
app.register_blueprint(commerce)
app.register_blueprint(shop_enquiries)
app.register_blueprint(combined_baskets)
app.register_blueprint(combined_quote_lines)
app.register_blueprint(product_quote_lines)
app.register_blueprint(service_quotes)
app.register_blueprint(service_quote_pdfs)
app.register_blueprint(square_payments.payment_evidence)
app.register_blueprint(payment_review)
app.register_blueprint(transaction_preview)
app.register_blueprint(sheet_link_inspection)
app.register_blueprint(migration_preview)
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tommy Tints HQ</title><meta name="theme-color" content="#0b0f16"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Tommy Tints HQ"><link rel="icon" type="image/png" sizes="180x180" href="/favicon.ico?v=20261117d"><link rel="apple-touch-icon" sizes="180x180" href="/hq-icon-180.png?v=20261117d"><link rel="manifest" href="/static/manifest.webmanifest?v=20261117d"><style>
:root{font-family:system-ui,Arial;color:#eef2f8;background:#0b0f16}*{box-sizing:border-box}body{margin:0}.shell{display:flex;min-height:100vh}aside{width:235px;background:#111824;padding:24px;border-right:1px solid #283344}main{padding:30px;flex:1;min-width:0;max-width:1250px}h1{font-size:32px;margin:0 0 8px}h2{font-size:21px}p,small{color:#aab6c8}.brand{font-size:23px;font-weight:900;margin-bottom:28px}.brand b{color:#f0444d}button,.button{background:#263447;color:white;border:1px solid #43536a;padding:11px 14px;border-radius:9px;cursor:pointer;font-weight:650}button:hover{background:#364c67}button.active,.primary{background:#c82737;border-color:#e03a49}.nav{display:block;width:100%;text-align:left;margin:7px 0}.card{background:#151e2b;border:1px solid #2c394b;padding:20px;border-radius:14px;margin:15px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:13px}.grid .card{margin:0}.muted{color:#9baac0}.value{font-size:27px;font-weight:800}.hidden{display:none}label{display:block;margin:12px 0 5px;color:#c6d0df}input,select{background:#0d1420;color:white;border:1px solid #45536a;padding:11px;border-radius:8px;width:100%;max-width:420px}table{border-collapse:collapse;width:100%;margin-top:12px}td,th{padding:11px;border-bottom:1px solid #344055;text-align:left}a{color:#ff7c84}.notice{border-left:3px solid #eab44c;padding:12px 15px;background:#302718;margin:15px 0;color:#f0d99d}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}#timer{font-size:40px;font-variant-numeric:tabular-nums} @media(max-width:750px){.shell{display:block}aside{width:100%;padding:14px}.brand{margin-bottom:8px}.nav{display:inline-block;width:auto;margin:4px;font-size:12px}main{padding:17px}}

/* Tommy Tints HQ branded interface — responsive design layer */
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;color:#f7f8fa;background:#07080b;--accent:#ef1426;--yellow:#f4cf22;--panel:#14171d;--line:#34363d}body{background:radial-gradient(ellipse at 96% 0%,#3a0b13 0%,transparent 30%),#08090c;color:#f7f8fa}.shell{min-height:100vh}aside{width:252px;flex-shrink:0;background:linear-gradient(170deg,#0a0c10,#15171b 62%,#0a0a0e);border-right:1px solid #3d2024;padding:25px 14px;position:sticky;top:0;height:100vh;overflow:auto}main{max-width:none;width:100%;padding:28px clamp(16px,3vw,46px)}.brand{display:flex;flex-direction:column;align-items:center;gap:5px;text-align:center;padding:8px 3px 25px;margin-bottom:15px;border-bottom:1px solid #3c2427;letter-spacing:-.4px}.brand-emblem{display:flex;flex-direction:column;align-items:center;justify-content:center;width:100px;height:62px;border:3px solid #e9e9eb;border-radius:44% 44% 20% 20%;clip-path:polygon(0 0,100% 0,92% 72%,50% 100%,8% 72%);box-shadow:0 0 16px #e7193022}.brand-stars{font-size:11px;color:var(--yellow);letter-spacing:2px;white-space:nowrap}.brand-car{font-size:20px;color:var(--accent);letter-spacing:-2px}.brand-name{font-size:19px;white-space:nowrap;font-weight:950;font-style:italic}.brand-name b{color:#fa2737}.brand-name em{background:#f4ce21;color:#090909;border-radius:3px;padding:2px 5px;font-size:14px;font-style:normal}.brand small{color:#b4b4b8;font-size:9px;letter-spacing:2px}.nav{border:1px solid transparent;background:transparent;color:#d1d3d8;border-radius:9px;font-size:14px;margin:3px 0;padding:14px 13px;transition:background .2s,border-color .2s}.nav:hover{background:#27232a}.nav.active{background:linear-gradient(100deg,#c60a1b,#e41d2b);border-color:#ef3949;box-shadow:0 4px 20px #d70c292a;color:white}h1{font-size:clamp(24px,3vw,35px);letter-spacing:-1px}h2{font-size:20px;letter-spacing:-.4px}p,small,.muted{color:#aeb0b9}.card{background:linear-gradient(155deg,#191b21,#101217);border:1px solid #37383e;border-radius:14px;box-shadow:0 8px 24px #0003}.grid{gap:15px}.metric-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.metric{min-height:158px;position:relative;overflow:hidden}.metric:before{content:'';position:absolute;left:18px;right:18px;top:0;height:2px;background:linear-gradient(90deg,#f01628,#efcf27,transparent)}.metric-icon{display:inline-grid;place-items:center;width:37px;height:37px;background:#2b2024;color:#ffd622;border-radius:9px;font-size:22px;margin-bottom:11px}.metric small{display:block;color:#a8aab1}.metric .value{font-size:24px;margin:5px 0 8px}.metric-note{font-size:12px;color:#989ca6}.dashboard-intro{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin:6px 0 22px}.dashboard-intro h2{font-size:26px;margin:6px 0}.dashboard-intro p{margin:4px 0}.eyebrow{color:#f3ce25;font-size:11px;letter-spacing:1.7px;font-weight:800}.dashboard-panels{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(290px,1fr);gap:16px;margin-top:18px}.dashboard-panels .card{margin:0}.panel-title{display:flex;justify-content:space-between;align-items:center;gap:10px}.action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:18px}.action-grid button{padding:16px 10px;text-align:left}.status-line{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #33363c;padding:13px 0;font-size:13px}.status-pill{color:#f3ce25;font-size:11px;text-align:right}.muted-pill{color:#aeb0b9}.status-footnote{font-size:12px;line-height:1.6}button,.button{background:#242830;border-color:#484a52;color:#f9f9fa;border-radius:9px}button:hover{background:#3c3037}button.active,.primary{background:linear-gradient(110deg,#bc0619,#ee1a2e);border-color:#f33b4e;color:white}input,select,textarea{background:#0b0e13!important;border:1px solid #555861!important;color:#f8f8f8!important;border-radius:9px!important;min-height:42px}input:focus,select:focus,textarea:focus,button:focus-visible{outline:2px solid #f4d128;outline-offset:2px}a{color:#ff5b67}table{display:block;overflow-x:auto;white-space:nowrap}th{color:#f5d029}#heading{border-bottom:1px solid #33343a;padding-bottom:14px;margin-bottom:16px} @media(max-width:1150px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-panels{grid-template-columns:1fr}}@media(max-width:750px){aside{position:relative;height:auto;width:100%;padding:12px;overflow:visible}.brand{flex-direction:row;justify-content:center;gap:12px;padding:3px 0 12px;margin-bottom:9px}.brand-emblem{width:55px;height:43px}.brand-stars{font-size:6px}.brand-car{font-size:11px}.brand-name{font-size:17px}.brand small{display:none}.nav{font-size:12px;padding:11px 10px;margin:3px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{min-height:146px;padding:14px}.metric .value{font-size:20px}.dashboard-panels{grid-template-columns:1fr}.action-grid{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:18px 13px}.dashboard-intro h2{font-size:23px}}@media(max-width:370px){.metric-grid,.action-grid{grid-template-columns:1fr}}
.brand-logo{display:block;width:110px;height:110px;object-fit:contain;flex-shrink:0;border:0}.brand{gap:8px}@media(max-width:750px){.brand-logo{width:70px;height:70px}.brand{align-items:center}}</style><meta name="hq-csrf" content="__HQ_CSRF_PLACEHOLDER__"></head><body><div class="shell"><aside><div class="brand" aria-label="Tommy Tints HQ"><img class="brand-logo" src="/static/company-logo-header.webp" width="100" height="100" alt="Tommy Tints Vehicle Customisation shield logo"><span class="brand-name">TOMMY <b>TINTS</b> <em>HQ</em></span><small>VEHICLE CUSTOMISATION</small></div><a class="nav" href="/hq/finance">£ Finance</a><a class="nav" href="/hq/customers">◉ HQ Customers</a><a class="nav" href="/hq/services">✎ Service Tariffs</a><a class="nav" href="/hq/cost-library">⚒ Cost Library</a><a class="nav" href="/hq/service-enquiries">✉ Service Quote Inbox</a><button class="nav active" data-page="overview">⌂  Dashboard</button><button class="nav" data-page="calculator">▣  Quick Quotes</button><button class="nav" data-page="timer">◷  Job Timer</button><button class="nav" data-page="materials">⚒  Materials & Costs</button><button class="nav" data-page="jobs">▤  Jobs & Job Cards</button><button class="nav" data-page="customer-requests">✉  Customer Requests</button><button class="nav" data-page="maintenance">⌖  Maintenance Routes</button><button class="nav" data-page="assistant">✦  HQ Assistant</button><button class="nav" data-page="integrations">⚙  Integrations</button><button class="nav" data-page="settings">Settings</button></aside><main><h1 id="heading">Command centre</h1><p>Interactive development preview · real business records are not loaded</p><div class="notice">Preview only: login and Google service credentials are required before displaying private business data. No information entered here is sent to Google Sheets.</div>
<section id="overview" class="page"><div class="dashboard-intro"><div><span class="eyebrow">BUSINESS COMMAND CENTRE</span><h2>Welcome to Tommy Tints HQ</h2><p><a href="/hq/integrations">Integration health</a></p><p>Everything you need to run the workshop, in one place.</p></div><button class="primary" data-go="calculator">＋ New Quote</button></div><div class="grid metric-grid"><div class="card metric"><span class="metric-icon">▤</span><small>Jobs &amp; records</small><div class="value">Google Sheets</div><span class="metric-note">Open live jobs and work records</span></div><div class="card metric"><span class="metric-icon">▦</span><small>Bookings &amp; availability</small><div class="value">Calendar</div><span class="metric-note">Check appointment conflicts</span></div><div class="card metric"><span class="metric-icon">£</span><small>Job profitability</small><div class="value">Costing</div><span class="metric-note">Calculate before accepting work</span></div><div class="card metric"><span class="metric-icon">⌖</span><small>Maintenance routes</small><div class="value">Planner</div><span class="metric-note">Find customers due together</span></div></div><div class="dashboard-panels"><div class="card action-panel"><div class="panel-title"><h2>Quick actions</h2><span class="eyebrow">WORKSHOP TOOLS</span></div><div class="action-grid"><button class="primary" data-go="calculator">＋ Create Quote</button><button data-go="jobs">▤ View Jobs</button><button data-go="maintenance">▦ Calendar &amp; Routes</button><button data-go="materials">⚒ Job Costing</button><button data-go="timer">◷ Job Timer</button><button data-go="integrations">⚙ Connections</button></div></div><div class="card status-panel"><h2>HQ connection status</h2><div class="status-line"><span>Google Sheets</span><span class="status-pill">Connect to view live data</span></div><div class="status-line"><span>Google Calendar</span><span class="status-pill">Check in Maintenance</span></div><div class="status-line"><span>Square appointments</span><span class="status-pill muted-pill">Not yet integrated</span></div><p class="status-footnote">This dashboard deliberately does not show invented bookings, income or customer balances. Live KPI cards will be enabled after data sources are verified.</p></div></div></section>
<section id="calculator" class="page hidden"><div class="card"><h2>Quick Quote · Internal estimator</h2><p>Select a live Cost Library service to prefill labour and materials. Estimates require your approval; prices are not automatically sent to customers.</p><label>Cost Library service</label><select id="service"><option value="">Manual estimate — no service selected</option></select><p id="serviceInfo" class="muted">Load HQ records to select a live service.</p><label>Customer type</label><select id="tier"><option value="retail">Retail — £85/hour</option><option value="trade">Trade — £70/hour</option></select><label>Work description (for your notes only)</label><input id="workDescription" maxlength="300" placeholder="e.g. Hyundai bonnet repair and repaint"><label>Labour hours</label><input id="hours" type="number" min="0" step=".1" value="2.1"><p id="rateDisplay" class="muted">Confirmed retail labour rate: £85/hour</p><label>Materials (£)</label><input id="materials" type="number" min="0" step=".01" value="8.27"><label>Consumables (£)</label><input id="consumables" type="number" min="0" step=".01" value="2.97"><label>Additional overhead allowance (£)</label><input id="overhead" type="number" min="0" step=".01" value="0"><label>Quoted price (£, optional)</label><input id="quoted" type="number" min="0" step=".01" value="150"><p><button class="primary" id="calculate">Calculate</button></p><div id="quoteResult" aria-live="polite"></div></div></section>
<section id="materials" class="page hidden"><div class="card"><h2>Materials & consumables · job costing</h2><p>Calculate actual material cost. Nothing is written to Sheets. Add each clearcoat component separately; basecoat can use the mixing system's actual price.</p><label>Job reference</label><select id="matJob"><option value="">Unassigned</option></select><p id="matJobHelp">Choose a linked job from an HQ job card, select a Sheets job or enter a manual reference.</p><label>Manual job reference (when not listed)</label><input id="matManualJob" placeholder="e.g. J0057"><button id="matUseManual">Use reference</button><p id="matHqPriceHint" role="status" class="muted">The internal HQ job card controls the recorded selling price; editing costing cannot change it.</p><label>Selling price (£)</label><input id="matSale" type="number" min="0" step=".01"><label>Active labour hours</label><input id="matLabour" type="number" min="0" step=".1"><label>Actual labour cost (£ per hour, not customer rate)</label><input id="matActualRate" type="number" min="0" step=".01" placeholder="Required for meaningful profit"><label>Allocated overhead for this job (£)</label><input id="matOverhead" type="number" min="0" step=".01" placeholder="Enter job overhead"><label>Other job costs (£)</label><input id="matOther" type="number" min="0" step=".01" placeholder="Transport, subcontractors, etc."><label>Material name</label><input id="matName" value="Clearcoat"><label>Cost method</label><select id="matMethod"><option value="clearcoat_kit">Clearcoat kit (clear + hardener)</option><option value="quantity">Quantity from purchased pack</option><option value="direct">Actual cost from mixing system</option><option value="filler_preset">Filler size estimate</option></select><label>Clearcoat volume in kit (ml)</label><input id="matClear" type="number" min="0" value="5000"><label>Hardener volume in kit (ml)</label><input id="matHardener" type="number" min="0" value="2500"><label>Purchase price (£)</label><input id="matPrice" type="number" min="0" step=".01" value="100"><label>Pack quantity (ml, g, discs, etc.)</label><input id="matPack" type="number" min=".01" step=".01" value="5000"><label>Quantity used (same units)</label><input id="matUsed" type="number" min="0" step=".01" value="300"><label>Actual direct cost (£)</label><input id="matDirect" type="number" min="0" step=".01" value="8.61"><label>Filler repair size</label><select id="matPreset"><option value="small">Small skim (30 g)</option><option value="medium">Medium (90 g)</option><option value="heavy">Heavy (200 g)</option></select><label>Waste allowance (%)</label><input id="matWaste" type="number" min="0" max="100" value="0"><p><button id="addMaterial">Add material to job</button></p><p id="matStatus" role="status"></p><div id="matLines"></div><h2 id="matTotal">Total material cost: £0.00</h2><p id="matProfit"></p><div id="matBreakdown"></div><hr><h3>Price negotiation</h3><p>Set a minimum sustainable margin. The slider never changes the agreed job price automatically.</p><label>Minimum sustainable margin (%)</label><input id="matMinMargin" type="number" min="0" max="99" step="1" placeholder="Set your policy"><label>Proposed customer price (£)</label><input id="matOffer" type="range" min="0" max="2000" step="5" value="0"><p id="matOfferLabel">Proposed price: £0.00</p><p id="matNegotiation" role="status"></p><h3>Quiet-period cashflow check</h3><p id="matQuiet">Cash-only acceptance is not a sustainable pricing recommendation. Costs and capacity must be verified first.</p><hr><h3>Customer-facing job breakdown</h3><p>Only the work description, work stages, job reference and agreed selling price are printed. Internal costs and margins are excluded. Save job to HQ after editing the description or stages, then print.</p><label>Customer-facing job description</label><input id="matDescription" maxlength="200" placeholder="e.g. Hyundai bonnet preparation and repaint"><label>Work completed (one stage per line)</label><textarea id="matStages" rows="5" placeholder="Preparation and masking&#10;Surface repair&#10;Primer and paint&#10;Clearcoat and finishing"></textarea><button id="matPrint">Open printable customer breakdown</button><button id="clearMaterials">Clear list</button> <button id="syncMaterials" class="primary">Save job to HQ</button><p id="syncStatus" role="status">Material lines are local until saved to HQ.</p></div></section>
<section id="timer" class="page hidden"><div class="card"><h2>Job timer</h2><p>Use this to measure actual active working time. Timer data is saved on this device only and is not synced across devices.</p><label>Existing HQ job</label><select id="jobSelect"><option value="">Manual timer — no job selected</option></select><label>Job reference (no customer details)</label><input id="jobRef" maxlength="40" placeholder="e.g. J0082"><label>Estimated active labour (hours)</label><input id="estimatedHours" type="number" min="0.1" max="500" step="0.25" value="4"><p class="muted">Alerts at 75%, 90% and 100% of estimated active time. Save your timer locally on this device; browser alerts require permission and may not fire if the page is closed.</p><div id="timeProgress" role="progressbar" aria-label="Time used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="height:12px;background:#263447;border-radius:8px;overflow:hidden"><div id="timeFill" style="height:100%;width:0%;background:#eab44c"></div></div><p id="timeRemaining" role="status"></p><div id="timer">00:00:00</div><div class="row"><button class="primary" id="start">Start / Resume</button><button id="pause">Pause</button><button id="reset">Reset</button><button id="copyTime">Copy time</button></div><p id="timerMessage" role="status"></p></div></section>
<section id="jobs" class="page hidden"><div class="card"><h2>HQ job cards</h2><p><a class="button primary" href="/hq/work/new">＋ Create HQ customer, quote or job</a> · <a href="/hq/work/archived">View archived HQ records</a> · Save new native records without entering Google Sheets.</p><p>Search internal HQ jobs across all records, not just the latest 100. Prices and deposits are owner-entered; payments are unverified.</p>
<label for="jobCardSearch">Customer, registration, vehicle or service</label><input id="jobCardSearch" maxlength="80" placeholder="Search internal jobs">
<label for="jobCardFilter">Job status</label><select id="jobCardFilter"><option value="">All statuses</option><option value="draft">Draft</option><option value="ready_for_review">Ready for review</option><option value="approved">Approved</option><option value="in_progress">In progress</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select>
<div class="row"><button id="loadJobCards">Search / refresh jobs</button><button id="jobCardPrev" disabled>Previous 25</button><button id="jobCardNext" disabled>Next 25</button></div>
<p id="jobCardPaymentSummary" role="status">Approved job amounts are not verified cash receipts.</p><p id="jobCardStatus" role="status"></p><div id="hqJobCards" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,245px),1fr))"></div></div><div class="card"><h2>Existing HQ jobs</h2><p>Unmigrated historical Google Sheets records remain read-only. New native HQ records can be created and edited above; no historical import or bidirectional sync has been authorised.</p><p><a href="/auth/sheets/connect">Connect Google Sheets (read-only)</a> · <a href="/hq/migration/preview">Historical Jobs/Customers migration dry run</a> · <button id="loadSheets">Load HQ records</button></p><p id="sheetsStatus" role="status"></p><div id="sheetsSummary"></div><a href="https://docs.google.com/spreadsheets/d/1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M/edit#gid=330634230" target="_blank" rel="noopener noreferrer">Open the live Jobs sheet</a></div></section>
<section id="maintenance" class="page hidden"><div class="card"><h2>Maintenance Routes</h2><p><a class="button primary" href="/hq/maintenance/visits">＋ Create / edit HQ maintenance visits</a> · <a class="button" href="/hq/maintenance/plans">Manage native maintenance plans</a> · <a href="/hq/maintenance/routes">Open visual route planner</a></p><details><summary>Advanced maintenance tools</summary><p>Read-only view of existing Maintenance Clients, Maintenance Visits and Route Rules. Customer data stays within your authenticated HQ session.</p><button id="loadMaintenance" class="primary">Load maintenance customers</button><p id="maintenanceStatus" role="status"></p><div id="maintenanceSummary"></div><div id="maintenanceClusters"></div><hr><h3>Customers due together</h3><p>Suggests customers due within the next 7 days who share a recorded route area. Does not send reminders or create bookings.</p><button id="maintenanceSuggest">Find booking opportunities</button><div id="maintenanceSuggestions" role="status"></div><hr><h3>Live Google Calendar conflict check</h3><p>Checks your primary Google Calendar, read-only. Square bookings and other calendars are not included. Permission expires and may require reconnection.</p><p><a href="/auth/calendar/connect">Connect Google Calendar (read-only)</a></p><label for="liveCalendarDate">Appointment date</label><input id="liveCalendarDate" type="date"><label for="liveCalendarStartTime">Start time</label><input id="liveCalendarStartTime" type="time" value="09:00"><label for="liveCalendarEndTime">End time</label><input id="liveCalendarEndTime" type="time" value="10:00"><p>Times use UK local time (GMT/BST). No technical date format needed.</p><button id="liveCalendarCheck">Check live Google Calendar</button><p id="liveCalendarResult" role="status"></p><hr><h3>Booking conflict preview</h3><p>Paste proposed appointment and existing calendar event times in ISO 8601 format, including UK timezone offset. This checks overlaps only; it does not read your live calendar or create appointments.</p><label>Proposed start</label><input id="calendarStart" placeholder="2026-11-18T10:00:00+00:00"><label>Proposed end</label><input id="calendarEnd" placeholder="2026-11-18T11:00:00+00:00"><label>Existing busy events (one per line: start,end)</label><textarea id="calendarEvents" rows="3" placeholder="2026-11-18T10:30:00+00:00,2026-11-18T12:00:00+00:00"></textarea><button id="calendarCheck">Check overlapping appointments</button><p id="calendarResult" role="status"></p><hr><h3>Driving-time grouping (Google Routes)</h3><p>Optional integration: requires a configured server-side Google Maps Routes API key and verified full addresses. No automatic booking changes.</p><label>Full addresses (one per line, include street, postcode and UK)</label><textarea id="maintenanceAddresses" rows="5" placeholder="Unit 5 &amp; 6 Portland Lodge, RM14 3TJ, UK&#10;Customer full street address, SS11 7LL, UK"></textarea><button id="maintenanceMapCalculate">Compare driving times</button><div id="maintenanceMapResult" role="status"></div><hr><h3>Travel charge calculator</h3><p>Uses the existing 10 free one-way miles and £0.56 per mile setting. Enter verified one-way miles; no postcode-only distance guesses.</p><label>Verified one-way miles from HQ</label><input id="maintenanceMiles" type="number" min="0" step=".1" placeholder="Enter map distance"><button id="maintenanceTravelCalculate">Calculate travel surcharge</button><p id="maintenanceTravelResult" role="status"></p><hr><h3>Route profitability</h3><p>Enter total actual route distance and driving time, including journeys between customers and the return to HQ. Estimate costs across all stops.</p><label>Verified total route miles</label><input id="routeMiles" type="number" min="0" step=".1"><label>Total driving minutes</label><input id="routeMinutes" type="number" min="0" step="1"><label>Actual labour cost per hour (£)</label><input id="routeLabour" type="number" min="0" step=".01"><label>Allocated overhead per hour (£)</label><input id="routeOverhead" type="number" min="0" step=".01"><label>Stops (one per line: price, cleaning minutes, materials cost)</label><textarea id="routeStops" rows="5" placeholder="80,60,5.50&#10;80,60,5.50"></textarea><button id="routeCalculate">Calculate route contribution</button><p id="routeResult" role="status"></p><p>Suggested groups are based on your existing route areas, not verified driving times. No bookings are created or changed.</p><p><a href="https://docs.google.com/spreadsheets/d/1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M/edit" target="_blank" rel="noopener noreferrer">Open maintenance source workbook</a></p></div></details></section><section id="customer-requests" class="page hidden"><div class="card"><h2>Customer Requests</h2><p><a class="button primary" href="/hq/work/new">＋ Create native HQ work directly</a></p><p>Incoming customer booking and quote enquiries. Requests are unconfirmed until you review them. Changing a status does not book appointments, send messages or take payment.</p><button id="customerRequestsRefresh">Refresh requests</button><button id="customerDraftsRefresh">Refresh internal drafts</button><button id="customerWorkRefresh">Refresh work records</button><button id="customerCreateTestJob">Create TEST job</button><p id="customerRequestsStatus" role="status"></p><div id="customerRequestsList" style="display:grid;gap:12px;margin-top:16px"></div><h3>Internal quote and job drafts</h3><p>These are preparation records only, not jobs added to the live job board or quotes sent to customers.</p><div id="customerDraftsList" style="display:grid;gap:12px"></div><h3>Internal work records</h3><p>Search all internal jobs and quotes, including older records. These are not verified invoices or bookings.</p><label for="workSearch">Find customer, email, phone, registration or service</label><input id="workSearch" maxlength="80" placeholder="Customer, registration or service"><label for="workKind">Record type</label><select id="workKind"><option value="">All types</option><option value="job">Jobs</option><option value="quote">Quotes</option></select><label for="workStatus">Status</label><select id="workStatus"><option value="">All statuses</option><option value="draft">Draft</option><option value="ready_for_review">Ready for review</option><option value="approved">Approved</option><option value="in_progress">In progress</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select><div class="row"><button id="workSearchApply">Find records</button><button id="workPrev" disabled>Previous 25</button><button id="workNext" disabled>Next 25</button></div><p id="workPageStatus" role="status" aria-live="polite"></p><div id="customerWorkList" style="display:grid;gap:12px"></div></div></section><link rel="stylesheet" href="/static/hq-reactor.css?v=1"><script defer src="/static/hq-reactor.js?v=1"></script><section id="assistant" class="page hidden"><div class="card"><h2><img src="/static/bot/tommy-avatar-64.webp" width="64" height="64" alt="Tommy Tints Bot" style="vertical-align:middle;border-radius:50%"> Tommy Tints Bot · Private HQ Assistant</h2><p>Private operations assistant — early access. Describe what needs doing in everyday language. Ask “HQ work summary” for live local internal record totals, even without API credentials. OpenAI chat requires configuration. Chat is read-only: no emails, bookings, payments or records are changed.</p><div id="assistantChatLog" aria-live="polite" style="display:grid;gap:10px;max-height:420px;overflow:auto;margin:12px 0"></div><label for="assistantInput">What would you like me to handle?</label><textarea id="assistantInput" maxlength="2000" rows="5" style="width:100%;max-width:760px;background:#0d1420;color:#fff;border:1px solid #45536a;padding:12px;border-radius:9px" placeholder="e.g. Check overdue invoices, prepare follow-up emails and review tomorrow's bookings"></textarea><div class="row" style="margin-top:12px"><button class="primary" id="assistantChat">Ask HQ assistant</button><button id="assistantPlan">Prepare task plan (offline)</button><button id="assistantClear">Clear saved chat</button><button id="assistantAuditRefresh">View activity log</button></div><p id="assistantStatus" role="status" aria-live="polite"></p><div id="assistantResults" class="grid"></div><div class="card"><h3>Assistant task inbox</h3><p>Save administrative tasks for later review. Approving a task marks it as approved; it does not execute it.</p><label for="assistantTaskTitle">Task title</label><input id="assistantTaskTitle" maxlength="160" placeholder="e.g. Follow up outstanding quote"><label for="assistantTaskDetails">Details</label><textarea id="assistantTaskDetails" maxlength="3000" rows="3" style="width:100%;background:#0d1420;color:white" placeholder="What needs doing?"></textarea><button id="assistantSaveTask">Save draft task</button><button id="assistantRefreshTasks">Refresh tasks</button><p id="assistantTaskStatus" role="status"></p><div id="assistantTaskList"></div><h3>Activity log</h3><div id="assistantAuditLog"></div></div><div class="notice">Actions requiring external access or approval are not executed in this preview. OpenAI chat requires Railway credentials. Local HQ work summary works without OpenAI. No external action tools or background worker are connected yet.</div></div></section><section id="settings" class="page hidden"><div class="card"><p><a href="/hq/shop">Manage products</a> · <a href="/hq/maintenance/packages">Maintenance package ledger</a></p><h2>App version</h2>
<p>Current version <code style="overflow-wrap:anywhere">{{ hq_build }}</code></p>
<p>Latest available version <code id="hq-latest-version" style="overflow-wrap:anywhere">Checking…</code></p>
<h2>Passkeys</h2><p>Sign in again before adding or removing a passkey. Your iPhone may use Face ID or its device passcode.</p>
<button type="button" data-passkey="register">Add passkey</button>
<ul>{% for key in owner_passkeys %}<li>Passkey …{{ key.id[-6:] }} <button type="button" data-passkey="remove" data-credential="{{ key.id }}">Remove</button></li>{% endfor %}</ul>
<p id="hq-passkey-status" role="status"></p><script defer src="/static/hq-passkeys.js"></script>
</div></section><section id="integrations" class="page hidden"><div class="card"><h2>Integration readiness</h2><table><tr><th>Integration</th><th>Status</th></tr><tr><td>Google Sheets adapter</td><td>Google Sheets read-only OAuth consent available; server-side token persistence pending</td></tr><tr><td>Square / Calendar</td><td>Not connected to this app</td></tr><tr><td>Monzo / DNA Payments</td><td>Not connected to this app</td></tr><tr><td>Login / roles</td><td>Required before private data sync</td></tr></table></div></section>
</main></div><script>
"use strict";
const $=id=>document.getElementById(id);

const assistantBtn=document.getElementById("assistantPlan");if(assistantBtn){assistantBtn.addEventListener("click",async()=>{const message=document.getElementById("assistantInput").value;const status=document.getElementById("assistantStatus"),out=document.getElementById("assistantResults");status.textContent="Preparing task plan…";out.replaceChildren();assistantBtn.disabled=true;try{const r=await fetch("/api/hq/assistant/plan",{method:"POST",headers:{"Content-Type":"application/json","X-HQ-CSRF":document.querySelector('meta[name="hq-csrf"]').content},body:JSON.stringify({message})});const data=await r.json();if(!r.ok)throw Error(data.error||"Unable to prepare plan");status.textContent=data.message;for(const task of data.tasks){const box=document.createElement("div");box.className="card";const heading=document.createElement("h3");heading.textContent=task.title;const note=document.createElement("p");note.textContent=task.next_step;const state=document.createElement("small");state.textContent=task.approval_required?"Approval required before action":"Not executed — integration pending";const save=document.createElement("button");save.textContent="Save to task inbox";save.addEventListener("click",async()=>{save.disabled=true;try{await assistantTaskApi("/api/hq/assistant/tasks","POST",{title:task.title,details:"Original request: "+message.slice(0,1400)+"\\nNext step: "+task.next_step,category:task.category});save.textContent="Saved as draft";status.textContent="Task saved for review; no action executed.";await refreshAssistantTasks();await loadAssistantAudit()}catch(e){save.disabled=false;status.textContent=e.message}});box.append(heading,note,state,save);out.append(box)}}catch(e){status.textContent=e.message}finally{assistantBtn.disabled=false}})}

function showAssistantSaveChatTask(message,reply){const log=document.getElementById("assistantChatLog");const box=document.createElement("div");box.className="card";const button=document.createElement("button");button.textContent="Save this discussion as a draft task";button.addEventListener("click",async()=>{button.disabled=true;const title=message.trim().slice(0,160);try{await assistantTaskApi("/api/hq/assistant/tasks","POST",{title:title.length>=3?title:"Follow up assistant conversation",details:"Owner request: "+message.slice(0,1200)+"\\nAssistant response: "+reply.slice(0,1700),category:"general"});button.textContent="Saved to task inbox";await refreshAssistantTasks();await loadAssistantAudit()}catch(e){button.disabled=false;document.getElementById("assistantStatus").textContent=e.message}});box.append(button);log.append(box);log.scrollTop=log.scrollHeight;}
let assistantHistory=[];
const assistantChatBtn=document.getElementById("assistantChat");
function appendAssistantMessage(role,content){const log=document.getElementById("assistantChatLog");const p=document.createElement("div");p.style.cssText="padding:12px;border:1px solid #45536a;border-radius:10px;white-space:pre-wrap;max-width:95%";p.textContent=(role==="user"?"You: ":"HQ Assistant: ")+content;log.appendChild(p);log.scrollTop=log.scrollHeight;}
if(assistantChatBtn){assistantChatBtn.addEventListener("click",async()=>{const input=document.getElementById("assistantInput"),message=input.value.trim(),status=document.getElementById("assistantStatus");if(!message)return;assistantChatBtn.disabled=true;status.textContent="Thinking…";window.dispatchEvent(new Event("hq-assistant-thinking"));try{const r=await fetch("/api/hq/assistant/chat",{method:"POST",headers:{"Content-Type":"application/json","X-HQ-CSRF":document.querySelector('meta[name="hq-csrf"]').content},body:JSON.stringify({message,history:assistantHistory.slice(-12)})});const data=await r.json();if(!r.ok)throw Error(data.error||"AI request failed");appendAssistantMessage("user",message);appendAssistantMessage("assistant",data.reply);window.dispatchEvent(new CustomEvent("hq-assistant-reply",{detail:data.reply}));showAssistantSaveChatTask(message,data.reply);assistantHistory.push({role:"user",content:message},{role:"assistant",content:data.reply});assistantHistory=assistantHistory.slice(-12);input.value="";status.textContent=data.mode==="hq_records_local"?"HQ work summary loaded locally. No external action executed.":"AI response received. No business actions executed.";}catch(e){status.textContent=e.message}finally{assistantChatBtn.disabled=false;window.dispatchEvent(new Event("hq-assistant-finished"))}});document.getElementById("assistantClear").addEventListener("click",async()=>{if(!confirm("Permanently clear saved HQ Assistant chat messages?"))return;try{await assistantTaskApi("/api/hq/assistant/history/clear","POST");assistantHistory=[];document.getElementById("assistantChatLog").replaceChildren();document.getElementById("assistantStatus").textContent="Saved chat cleared."}catch(e){document.getElementById("assistantStatus").textContent=e.message}})}


async function refreshCustomerRequests(){const host=document.getElementById("customerRequestsList");const notice=document.getElementById("customerRequestsStatus");host.replaceChildren();notice.textContent="Loading customer requests…";try{const data=await assistantTaskApi("/api/hq/customer-requests");notice.textContent=data.requests.length+" request(s)";if(!data.requests.length){host.textContent="No customer requests received yet. Public intake is currently disabled unless explicitly enabled.";return}for(const req of data.requests){const card=document.createElement("div");card.className="card";const heading=document.createElement("h3");heading.textContent=req.name+" — "+req.service;const info=document.createElement("p");info.textContent=(req.kind==="booking"?"Booking request":"Quote request")+" · "+req.vehicle_make+" "+req.vehicle_model+" "+(req.registration||"")+" · "+(req.preferred_date||"No date")+" "+(req.preferred_time||"");const contact=document.createElement("p");contact.textContent="Email: "+req.email+" | Phone: "+req.phone;const notes=document.createElement("p");notes.textContent=req.notes||"No additional notes";const label=document.createElement("label");label.textContent="Review status";const select=document.createElement("select");for(const value of ["new","reviewing","quoted","declined","closed"]){const option=document.createElement("option");option.value=value;option.textContent=value;select.append(option)}select.value=req.status;select.setAttribute("aria-label","Status for "+req.name);select.addEventListener("change",async()=>{select.disabled=true;try{await assistantTaskApi("/api/hq/customer-requests/"+encodeURIComponent(req.id)+"/status","POST",{status:select.value});notice.textContent="Status updated. No booking or message sent."}catch(e){select.value=req.status;notice.textContent=e.message}finally{select.disabled=false}});label.append(select);const draftActions=document.createElement("div");draftActions.style.cssText="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px";for(const type of ["quote","job"]){const btn=document.createElement("button");btn.textContent="Prepare "+type+" draft";btn.disabled=["closed","declined"].includes(req.status);btn.addEventListener("click",async()=>{btn.disabled=true;try{const result=await assistantTaskApi("/api/hq/customer-requests/"+encodeURIComponent(req.id)+"/draft","POST",{draft_type:type});notice.textContent="Internal "+result.draft.draft_type+" draft saved. Nothing sent or booked.";await refreshCustomerDrafts()}catch(e){notice.textContent=e.message}finally{btn.disabled=false}});draftActions.append(btn)}card.append(heading,info,contact,notes,label,draftActions);host.append(card)}}catch(e){notice.textContent=e.message}}
async function refreshCustomerDrafts(){const host=document.getElementById("customerDraftsList");host.replaceChildren();try{const result=await assistantTaskApi("/api/hq/customer-request-drafts");if(!result.drafts.length){host.textContent="No internal drafts yet.";return}for(const draft of result.drafts){const row=document.createElement("div");row.className="card";const title=document.createElement("strong");title.textContent=draft.draft_type.toUpperCase()+" draft · "+draft.name+" · "+draft.service;const detail=document.createElement("p");detail.textContent=draft.vehicle_make+" "+draft.vehicle_model+" · "+draft.registration+" · Source enquiry "+draft.request_id.slice(0,10);const convert=document.createElement("button");convert.textContent="Create internal work record";convert.addEventListener("click",async()=>{convert.disabled=true;try{await assistantTaskApi("/api/hq/customer-requests/"+encodeURIComponent(draft.request_id)+"/work-record","POST",{});await refreshCustomerWorkRecords();document.getElementById("customerRequestsStatus").textContent="Internal record created; no appointment booked."}catch(e){document.getElementById("customerRequestsStatus").textContent=e.message}finally{convert.disabled=false}});row.append(title,detail,convert);host.append(row)}}catch(e){host.textContent=e.message}}
document.getElementById("customerRequestsRefresh").addEventListener("click",refreshCustomerRequests);
document.getElementById("customerDraftsRefresh").addEventListener("click",refreshCustomerDrafts);
let workPageOffset=0;const workPageLimit=25;
async function refreshCustomerWorkRecords(){const host=document.getElementById("customerWorkList");host.replaceChildren();try{const filters=new URLSearchParams({q:$("workSearch").value.trim(),kind:$("workKind").value,status:$("workStatus").value,limit:String(workPageLimit),offset:String(workPageOffset)});const result=await assistantTaskApi("/api/hq/work-records/search?"+filters);$("workPageStatus").textContent=result.total+" matching internal records · showing "+(result.records.length?workPageOffset+1:0)+"–"+(workPageOffset+result.records.length);$("workPrev").disabled=workPageOffset===0;$("workNext").disabled=!result.has_more;if(!result.records.length){host.textContent="No matching internal records. Adjust filters or go to the previous page.";return}for(const item of result.records){const card=document.createElement("div");card.className="card";const title=document.createElement("strong");title.textContent=item.name+" · "+item.make+" "+item.model+" · "+item.service;const meta=document.createElement("p");meta.textContent=item.kind+" · "+item.status+" · "+item.registration;if(item.original_draft_kind==="quote")meta.textContent+=" · original reviewed quotation";const price=document.createElement("input");price.type="number";price.min="0";price.step="0.01";price.placeholder="Total price £";if(item.price_pence!==null)price.value=(item.price_pence/100).toFixed(2);const deposit=document.createElement("input");deposit.type="number";deposit.min="0";deposit.step="0.01";deposit.placeholder="Planned deposit £ (NOT received)";deposit.setAttribute("aria-label","Planned deposit amount, not a received payment");deposit.value=(item.deposit_pence/100).toFixed(2);const save=document.createElement("button");save.textContent="Save draft amounts";save.addEventListener("click",async()=>{const toPence=v=>{if(!/^\d{1,7}(?:\.\d{1,2})?$/.test(v))throw Error("Enter an amount in pounds with up to two decimals");const [whole,part=""]=v.split(".");return Number(whole)*100+Number(part.padEnd(2,"0"))};try{const result=await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(item.id)+"/financials","POST",{price_pence:toPence(price.value),deposit_pence:toPence(deposit.value),expected_revision:item.revision});item.revision=result.revision;item.price_pence=result.price_pence;item.deposit_pence=result.deposit_pence;document.getElementById("customerRequestsStatus").textContent="Draft saved. Conditional remainder after planned deposit £"+(result.balance_pence/100).toFixed(2)+". This is not a verified amount owed or received."}catch(e){document.getElementById("customerRequestsStatus").textContent=e.message}});const state=document.createElement("select");for(const optionValue of ["draft","ready_for_review","approved","in_progress","completed","cancelled"]){const option=document.createElement("option");option.value=optionValue;option.textContent=optionValue.replaceAll("_"," ");state.append(option)}state.value=item.status;const apply=document.createElement("button");const labelStatusAction=()=>{apply.textContent=item.kind==="quote"&&state.value==="approved"?"Approve quote and create job":"Update internal status"};state.addEventListener("change",labelStatusAction);labelStatusAction();apply.addEventListener("click",async()=>{try{await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(item.id)+"/status","POST",{status:state.value,expected_revision:item.revision});document.getElementById("customerRequestsStatus").textContent="Internal status updated. No customer contacted or booking created.";await refreshCustomerWorkRecords()}catch(e){document.getElementById("customerRequestsStatus").textContent=e.message}});const edit=document.createElement("a");edit.textContent="Edit customer";edit.href="/hq/customers/"+encodeURIComponent(item.customer_id)+"/edit";card.append(title,meta,price,deposit,save,state,apply,edit);if(item.source_request_id.startsWith("HQ-")){const scopeEdit=document.createElement("a");scopeEdit.href="/hq/work/"+encodeURIComponent(item.id)+"/edit";scopeEdit.textContent="Edit work scope & tier";card.append(scopeEdit);}if(item.source_request_id.startsWith("TEST-")){const remove=document.createElement("button");remove.textContent="Delete TEST job";remove.addEventListener("click",async()=>{try{await assistantTaskApi("/api/hq/test-work-records/"+encodeURIComponent(item.id),"DELETE");await refreshCustomerWorkRecords();document.getElementById("customerRequestsStatus").textContent="Synthetic test job deleted."}catch(e){document.getElementById("customerRequestsStatus").textContent=e.message}});card.append(remove)}host.append(card)}}catch(e){host.textContent=e.message}}
document.getElementById("customerWorkRefresh").addEventListener("click",refreshCustomerWorkRecords);
document.getElementById("workSearchApply").addEventListener("click",()=>{workPageOffset=0;refreshCustomerWorkRecords()});
document.getElementById("workSearch").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();workPageOffset=0;refreshCustomerWorkRecords()}});
document.getElementById("workPrev").addEventListener("click",()=>{workPageOffset=Math.max(0,workPageOffset-workPageLimit);refreshCustomerWorkRecords()});
document.getElementById("workNext").addEventListener("click",()=>{workPageOffset+=workPageLimit;refreshCustomerWorkRecords()});document.getElementById("customerCreateTestJob").addEventListener("click",async()=>{const notice=document.getElementById("customerRequestsStatus");try{await assistantTaskApi("/api/hq/test-work-records","POST",{});notice.textContent="Synthetic TEST job created. No real customer, payment or appointment.";await refreshCustomerWorkRecords()}catch(e){notice.textContent=e.message}});
async function assistantTaskApi(path,method="GET",data){const r=await fetch(path,{method,headers:{"Content-Type":"application/json","X-HQ-CSRF":document.querySelector('meta[name="hq-csrf"]').content},...(data?{body:JSON.stringify(data)}:{})});const result=await r.json();if(!r.ok)throw Error(result.error||"Task request failed");return result}
async function refreshAssistantTasks(){const container=document.getElementById("assistantTaskList");container.replaceChildren();try{const result=await assistantTaskApi("/api/hq/assistant/tasks");if(!result.tasks.length){container.textContent="No tasks saved yet.";return}for(const task of result.tasks){const box=document.createElement("div");box.className="card";const title=document.createElement("h4");title.textContent=task.title;const details=document.createElement("p");details.textContent=task.details;const status=document.createElement("small");status.textContent=task.status+" • "+task.category;box.append(title,details,status);if(task.status==="draft"){for(const decision of ["approved","rejected"]){const btn=document.createElement("button");btn.textContent=decision==="approved"?"Approve (no execution)":"Reject";btn.addEventListener("click",async()=>{btn.disabled=true;try{await assistantTaskApi("/api/hq/assistant/tasks/"+encodeURIComponent(task.id)+"/review","POST",{decision});await refreshAssistantTasks()}catch(e){document.getElementById("assistantTaskStatus").textContent=e.message;btn.disabled=false}});box.append(btn)}}container.append(box)}}catch(e){document.getElementById("assistantTaskStatus").textContent=e.message}}
document.getElementById("assistantSaveTask").addEventListener("click",async()=>{const status=document.getElementById("assistantTaskStatus");try{await assistantTaskApi("/api/hq/assistant/tasks","POST",{title:document.getElementById("assistantTaskTitle").value,details:document.getElementById("assistantTaskDetails").value,category:"general"});status.textContent="Draft task saved. No action executed.";document.getElementById("assistantTaskTitle").value="";document.getElementById("assistantTaskDetails").value="";await refreshAssistantTasks()}catch(e){status.textContent=e.message}});
document.getElementById("assistantRefreshTasks").addEventListener("click",refreshAssistantTasks);

async function loadAssistantHistory(){try{const r=await fetch("/api/hq/assistant/history");if(!r.ok)return;const data=await r.json();assistantHistory=data.messages||[];document.getElementById("assistantChatLog").replaceChildren();for(const m of assistantHistory)appendAssistantMessage(m.role,m.content)}catch(e){}}
async function loadAssistantAudit(){const el=document.getElementById("assistantAuditLog");el.replaceChildren();try{const data=await assistantTaskApi("/api/hq/assistant/audit");if(!data.events.length){el.textContent="No activity yet.";return}for(const event of data.events){const p=document.createElement("p");p.textContent=event.created_at+" — "+event.event+": "+event.details;el.append(p)}}catch(e){el.textContent=e.message}}
document.getElementById("assistantAuditRefresh").addEventListener("click",loadAssistantAudit);
loadAssistantHistory();
const TIMER_STORAGE_KEY="tommy-tints-hq-timer-v2";
let hqData=null;
const titles={overview:"Command centre",calculator:"Trade price calculator",materials:"Materials costing",timer:"Job timer",jobs:"Jobs",maintenance:"Maintenance & Routes","customer-requests":"Customer Requests",assistant:"HQ Assistant",settings:"Settings",integrations:"Integrations"};
function navigate(page){if(!titles[page])return;document.querySelectorAll(".page").forEach(el=>el.classList.toggle("hidden",el.id!==page));document.querySelectorAll("[data-page]").forEach(el=>el.classList.toggle("active",el.dataset.page===page));$("heading").textContent=titles[page];}
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.page)));
const jobCards=document.getElementById("hqJobCards");
const jobCardStatus=document.getElementById("jobCardStatus");
const jobCardPrev=document.getElementById("jobCardPrev");
const jobCardNext=document.getElementById("jobCardNext");
let jobCardOffset=0;
const jobCardLimit=25;
let jobCardRequestId=0;
let jobPaymentSummaryRequestId=0;
async function loadJobPaymentSummary(){
 const requestId=++jobPaymentSummaryRequestId;
 const display=document.getElementById("jobCardPaymentSummary");
 display.textContent="Loading approved job planning totals…";
 try{
  const summary=await assistantTaskApi("/api/hq/work-summary");
  if(requestId!==jobPaymentSummaryRequestId)return;
  const pounds=value=>new Intl.NumberFormat("en-GB",{style:"currency",currency:"GBP"}).format(value/100);
  const notes=[
   "Approved job prices (not collected): "+pounds(summary.approved_price_total_pence),
   "Planned deposits (not confirmed received): "+pounds(summary.planned_deposit_total_pence),
   "Remainder IF planned deposits are paid: "+pounds(summary.conditional_remainder_pence),
   "Verified receipts and actual outstanding amounts: unavailable until payment reconciliation"
  ];
  if(summary.approved_amounts_incomplete_count)
   notes.push(summary.approved_amounts_incomplete_count+" approved record(s) with missing or invalid prices excluded");
  if(summary.excluded_synthetic_approved_record_count)
   notes.push(summary.excluded_synthetic_approved_record_count+" synthetic TEST job(s) excluded from financial planning totals");
  display.textContent=notes.join(" · ");
 }catch(error){
  if(requestId===jobPaymentSummaryRequestId)
   display.textContent="Internal financial summary unavailable: "+error.message;
 }
}
async function loadJobCards(){
 const refresh=document.getElementById("loadJobCards");
 const requestId=++jobCardRequestId;
 refresh.disabled=true;jobCardPrev.disabled=true;jobCardNext.disabled=true;
 jobCardStatus.textContent="Loading HQ jobs…";
 void loadJobPaymentSummary();
 const currency=value=>new Intl.NumberFormat("en-GB",{style:"currency",currency:"GBP"}).format(value/100);
 function line(box,tag,label){const node=document.createElement(tag);node.textContent=label;box.append(node);}
 try{
  const filters=new URLSearchParams({
   q:document.getElementById("jobCardSearch").value.trim(),
   kind:"job",status:document.getElementById("jobCardFilter").value,
   limit:String(jobCardLimit),offset:String(jobCardOffset)
  });
  const result=await assistantTaskApi("/api/hq/work-records/search?"+filters);
  if(requestId!==jobCardRequestId)return;
  jobCards.replaceChildren();
  jobCardPrev.disabled=jobCardOffset===0;
  jobCardNext.disabled=!result.has_more;
  for(const job of result.records){
   const box=document.createElement("article");box.className="card";
   line(box,"h3",[job.make,job.model,job.registration].filter(Boolean).join(" ")||"Vehicle not recorded");
   line(box,"p",job.name+" · "+job.service);
   line(box,"p",job.status.replaceAll("_"," "));
    if(job.original_draft_kind==="quote")line(box,"p","Origin: reviewed quotation · original HQ record "+job.id.slice(0,8));
   const progress={draft:0,ready_for_review:1,approved:2,in_progress:3,completed:4}[job.status];
   if(progress!==undefined){
    const bar=document.createElement("progress");bar.max=4;bar.value=progress;
    bar.setAttribute("aria-label",job.status.replaceAll("_"," "));box.append(bar);
   }
   line(box,"p","Requested date "+(job.preferred_date||"not recorded"));
   const approved=["approved","in_progress","completed"].includes(job.status);
   line(box,"p",(approved?"Approved price ":"Recorded price ")+(job.price_pence===null?"not recorded":currency(job.price_pence)));
   line(box,"p","Planned deposit (not verified as received) "+currency(job.deposit_pence));
   if(job.price_pence!==null)line(box,"p","Price less planned deposit "+currency(job.price_pence-job.deposit_pence)+" · illustrative only, not verified amount owed");
   const syncDetail=document.createElement("p");syncDetail.textContent="Sheets: not yet verified";
   const syncButton=document.createElement("button");syncButton.type="button";syncButton.textContent="Check Sheets sync";
   syncButton.addEventListener("click",async()=>{try{const info=await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(job.id)+"/sheet-sync");syncDetail.textContent="Sheets: "+info.state.replaceAll("_"," ")+" · write access and verified mapping required";}catch(error){syncDetail.textContent=error.message;}});
   box.append(syncDetail,syncButton);
   const transition={approved:["Start job","in_progress"],in_progress:["Complete job","completed"]}[job.status];
   if(transition){
    const action=document.createElement("button");action.type="button";action.textContent=transition[0];
    action.addEventListener("click",async()=>{
     if(!confirm(transition[0]+" for "+job.name+"?"))return;
     action.disabled=true;
     try{await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(job.id)+"/status","POST",{status:transition[1],expected_revision:job.revision});await loadJobCards();}
     catch(error){jobCardStatus.textContent=error.message;action.disabled=false;}
    });
    box.append(action);
   }
   const historyDetail=document.createElement("div");historyDetail.setAttribute("role","status");
    const historyButton=document.createElement("button");historyButton.type="button";historyButton.textContent="Review quote & job history";
    historyButton.addEventListener("click",async()=>{
     historyButton.disabled=true;historyDetail.textContent="Loading internal work history…";
     try{
      const history=await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(job.id)+"/history");
      historyDetail.replaceChildren();
      line(historyDetail,"p","Source enquiry "+history.source_request_id.slice(0,12)+" · "+(history.original_draft_kind==="quote"?"Originally a quotation":history.original_draft_kind==="job"?"Originally a job draft":"Original draft type unverified"));
      if(history.approved_from_quote)line(historyDetail,"p","Reviewed quote approved into the same job record; no duplicate record created.");
      if(!history.events.length)line(historyDetail,"p","No recorded internal changes for this job.");
      for(const event of history.events){
       let detail="";
       if(event.action==="financials"){
        const amount=value=>Number.isInteger(value)?currency(value):"not recorded";
        detail="Draft price "+amount(event.before.price_pence)+" → "+amount(event.after.price_pence)+
         "; planned deposit "+amount(event.before.deposit_pence)+" → "+amount(event.after.deposit_pence);
       }else{
        detail="Status "+(event.before.status||"unknown")+" → "+(event.after.status||"unknown");
        if(event.before.kind!==event.after.kind)detail+=" · type "+(event.before.kind||"unknown")+" → "+(event.after.kind||"unknown");
       }
       line(historyDetail,"p",event.created_at+" · "+detail);
      }
      if(history.has_earlier_events)line(historyDetail,"small","Only the most recent 100 changes shown.");
      line(historyDetail,"small","Entered financial amounts are not payment verification.");
     }catch(error){historyDetail.textContent="History unavailable: "+error.message;}
     finally{historyButton.disabled=false;}
    });
    box.append(historyButton,historyDetail);
    const costDetail=document.createElement("div");costDetail.setAttribute("role","status");
   const costReview=document.createElement("button");costReview.type="button";costReview.textContent="Review costs & contribution";
   costReview.addEventListener("click",async()=>{
    costReview.disabled=true;costDetail.textContent="Checking saved internal job costs…";
    try{
     const snapshot=await assistantTaskApi("/api/hq/work-records/"+encodeURIComponent(job.id)+"/costing");
     costDetail.replaceChildren();
     if(!snapshot.costing_saved){line(costDetail,"p","No saved cost log yet for this internal job.");return;}
     if(snapshot.price_pence===null)line(costDetail,"p","No HQ selling price recorded; margin cannot be calculated.");
     else line(costDetail,"p",(snapshot.price_approved?"Approved ":"Unapproved ")+"HQ price: "+currency(snapshot.price_pence));
     if(snapshot.saved_sale_matches_record===false)line(costDetail,"p","Warning: saved costing uses a different selling price. The HQ work-record price takes precedence.");
     if(snapshot.materials!==null)line(costDetail,"p","Materials: £"+snapshot.materials+" across "+snapshot.material_line_count+" saved line(s)");
     if(snapshot.labour_hours!==null)
      line(costDetail,"p","Recorded labour: "+snapshot.labour_hours+" hours"+(snapshot.actual_labour_rate!==null?" at £"+snapshot.actual_labour_rate+"/hour":" (cost rate missing)"));
     if(snapshot.actual_labour_cost!==null)line(costDetail,"p","Actual labour cost: £"+snapshot.actual_labour_cost);
     if(snapshot.allocated_overhead!==null)line(costDetail,"p","Allocated overhead: £"+snapshot.allocated_overhead);
     if(snapshot.other_costs!==null)line(costDetail,"p","Other recorded costs: £"+snapshot.other_costs);
     if(snapshot.missing_cost_fields&&snapshot.missing_cost_fields.length){
      const fieldLabels={lines:"materials list",labour:"labour hours",actual_labour_rate:"actual labour rate",overhead:"allocated overhead",other_costs:"other costs"};
      line(costDetail,"p","Incomplete cost inputs: "+snapshot.missing_cost_fields.map(field=>fieldLabels[field]||field).join(", "));
     }
     if(snapshot.total_recorded_cost!==null)line(costDetail,"p","Total recorded costs: £"+snapshot.total_recorded_cost);
     if(snapshot.estimated_contribution!==null)
      line(costDetail,"p","Estimated contribution: £"+snapshot.estimated_contribution+" · margin "+snapshot.margin_percent+"%");
     else line(costDetail,"p","Contribution withheld until price is approved and all costing inputs are complete.");
     if(!snapshot.complete_costs&&(!snapshot.missing_cost_fields||!snapshot.missing_cost_fields.length))
      line(costDetail,"p",snapshot.note);
     if(snapshot.customer_summary_ready){
      const preview=document.createElement("a");
      preview.href="/customer-breakdown/"+encodeURIComponent(job.id);
      preview.target="_blank";preview.rel="noopener noreferrer";
      preview.textContent="Preview approved customer work summary";
      costDetail.append(preview);
      line(costDetail,"small","Private preview only; no customer message or booking is created.");
     }else if(snapshot.customer_summary_missing){
      const labels={approved_price:"an approved HQ selling price",saved_work_stages:"saved customer-facing work stages"};
      line(costDetail,"p","Customer work summary unavailable: "+snapshot.customer_summary_missing.map(field=>labels[field]||field).join(" and ")+".");
     }
     line(costDetail,"small","Payments unverified. Estimated contribution is not accounting profit.");
    }catch(error){costDetail.textContent="Costing unavailable: "+error.message;}
    finally{costReview.disabled=false;}
   });
   box.append(costReview);
   const openCost=document.createElement("button");openCost.type="button";openCost.textContent="Open linked costing";
   openCost.addEventListener("click",async()=>{
    const selector=$("matJob");
    if(!Array.from(selector.options).some(option=>option.value===job.id))
     selector.add(new Option("Internal HQ job "+job.id.slice(0,8),job.id));
    selector.value=job.id;currentMaterialJob=job.id;currentMaterialRevision=0;
    loadMat();navigate("materials");
    $("matHqPriceHint").textContent=(job.price_pence===null?"Internal HQ selling price not recorded.":(approved?"Approved HQ price: ":"Unapproved HQ price: ")+currency(job.price_pence))+" Costing edits cannot change the HQ job price.";
    await loadServerMat();
   });
   box.append(openCost,costDetail);
   const edit=document.createElement("a");edit.className="button";edit.textContent="Edit customer";
   edit.href="/hq/customers/"+encodeURIComponent(job.customer_id)+"/edit";box.append(edit);if(job.source_request_id.startsWith("HQ-")){const scopeEdit=document.createElement("a");scopeEdit.className="button";scopeEdit.textContent="Edit work scope & tier";scopeEdit.href="/hq/work/"+encodeURIComponent(job.id)+"/edit";box.append(scopeEdit);}
   const progressLink=document.createElement("a");progressLink.className="button";progressLink.textContent="Record / view work stages";progressLink.href="/hq/work/"+encodeURIComponent(job.id)+"/progress";box.append(progressLink);
   jobCards.append(box);
  }
  const first=result.records.length?jobCardOffset+1:0;
  jobCardStatus.textContent=result.total+" matching internal jobs · showing "+first+"–"+(jobCardOffset+result.records.length)+(result.total===0?" · No matching jobs":"");
 }catch(error){
  if(requestId===jobCardRequestId){jobCards.replaceChildren();jobCardStatus.textContent=error.message;}
 }finally{
  if(requestId===jobCardRequestId)refresh.disabled=false;
 }
}
document.getElementById("loadJobCards").addEventListener("click",()=>{jobCardOffset=0;loadJobCards();});
document.getElementById("jobCardSearch").addEventListener("keydown",event=>{
 if(event.key==="Enter"){event.preventDefault();jobCardOffset=0;loadJobCards();}
});
document.getElementById("jobCardFilter").addEventListener("change",()=>{jobCardOffset=0;loadJobCards();});
jobCardPrev.addEventListener("click",()=>{if(jobCardPrev.disabled)return;jobCardOffset=Math.max(0,jobCardOffset-jobCardLimit);loadJobCards();});
jobCardNext.addEventListener("click",()=>{if(jobCardNext.disabled)return;jobCardOffset+=jobCardLimit;loadJobCards();});
document.querySelector('[data-page="jobs"]').addEventListener("click",loadJobCards);

const startPage=new URLSearchParams(location.search).get("page");if(startPage==="maintenance")navigate("maintenance");if(startPage==="jobs"){navigate("jobs");loadJobCards();}
document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.go)));
function ukCalendarTime(date,time){
 if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!/^\d{2}:\d{2}$/.test(time))throw Error("Select a date and time.");
 const [year,month,day]=date.split("-").map(Number);
 const [hour,minute]=time.split(":").map(Number);
 if(hour>23||minute>59||new Date(Date.UTC(year,month-1,day)).toISOString().slice(0,10)!==date)throw Error("Invalid date or time.");
 // UK DST: starts last Sunday of March at 01:00 UTC, ends last Sunday of October at 01:00 UTC.
 const lastSunday=(m)=>{const d=new Date(Date.UTC(year,m,0));return d.getUTCDate()-d.getUTCDay();};
 const dstStart=Date.UTC(year,2,lastSunday(3),1);
 const dstEnd=Date.UTC(year,9,lastSunday(10),1);
 const local=Date.UTC(year,month-1,day,hour,minute);
 // For the repeated 01:00 hour in October, choose GMT; for the skipped March hour, reject.
 const summer=local>=dstStart+3600000&&local<dstEnd;
 if(month===3&&day===lastSunday(3)&&hour===1)throw Error("This hour is skipped when UK clocks go forward. Choose another time.");
 const offset=summer?"+01:00":"+00:00";
 return date+"T"+time+":00"+offset;
}
(function(){
 const dateInput=$("liveCalendarDate");
 if(dateInput&&!dateInput.value){
  const parts=new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/London",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date());
  const part=k=>parts.find(p=>p.type===k).value;
  dateInput.value=part("year")+"-"+part("month")+"-"+part("day");
 }
})();
$("liveCalendarCheck").addEventListener("click",async()=>{
 const host=$("liveCalendarResult");host.textContent="Checking live primary calendar…";
 try{
  const response=await fetch("/api/hq/maintenance/calendar-live-check",{method:"POST",credentials:"same-origin",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({start:ukCalendarTime($("liveCalendarDate").value,$("liveCalendarStartTime").value),end:ukCalendarTime($("liveCalendarDate").value,$("liveCalendarEndTime").value)})});
  const data=await response.json();if(!response.ok)throw Error(data.error||"Calendar check failed");
  host.textContent=(data.available?"No conflict in primary Google Calendar.":"Calendar conflict: "+data.conflicts.length+" overlapping event(s).")+" "+data.warning;
 }catch(e){host.textContent=e.message;}
});
$("calendarCheck").addEventListener("click",async()=>{
 const host=$("calendarResult");
 try{
  const lines=$("calendarEvents").value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  const events=lines.map(line=>{
   const parts=line.split(",").map(x=>x.trim());
   if(parts.length!==2)throw Error("Each busy event needs a start and end separated by a comma.");
   return {start:parts[0],end:parts[1]};
  });
  const response=await fetch("/api/hq/maintenance/check-window",{method:"POST",credentials:"same-origin",
   headers:{"Content-Type":"application/json"},body:JSON.stringify({start:$("calendarStart").value,end:$("calendarEnd").value,events})});
  const data=await response.json();if(!response.ok)throw Error(data.error||"Unable to check");
  host.textContent=(data.available?"No overlaps in supplied events.":"Conflicts found: "+data.conflicts.length+".")+" "+data.warning;
 }catch(e){host.textContent=e.message;}
});
$("maintenanceSuggest").addEventListener("click",async()=>{
 const host=$("maintenanceSuggestions");host.textContent="Checking due customers…";
 try{
  const response=await fetch("/api/hq/maintenance/suggestions",{credentials:"same-origin"});
  const data=await response.json();if(!response.ok)throw Error(data.error||"Unable to load suggestions");
  host.replaceChildren();
  if(!data.groups.length){host.textContent="No multi-customer groups with recorded due dates in the next 7 days.";return;}
  data.groups.forEach(group=>{
   const card=document.createElement("div");card.className="card";
   const title=document.createElement("h3");title.textContent=group.area+" — "+group.customer_count+" customers";card.append(title);
   group.customers.forEach(c=>{
    const p=document.createElement("p");p.textContent=c.customer+" ("+c.postcode+") · due "+c.due+(c.days_until_due<0?" · overdue":"");card.append(p);
   });
   const note=document.createElement("p");note.textContent=group.verification+" "+group.suggested_action;card.append(note);host.append(card);
  });
 }catch(e){host.textContent=e.message;}
});
$("maintenanceMapCalculate").addEventListener("click",async()=>{
 const host=$("maintenanceMapResult");host.replaceChildren();
 try{
  const addresses=$("maintenanceAddresses").value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  if(addresses.length<2||addresses.length>10)throw Error("Enter 2–10 full addresses.");
  const response=await fetch("/api/hq/maintenance/driving-matrix",{method:"POST",credentials:"same-origin",
   headers:{"Content-Type":"application/json","X-CSRF-Token":document.querySelector('meta[name="csrf-token"]')?.content||""},
   body:JSON.stringify({addresses})});
  const data=await response.json();if(!response.ok)throw Error(data.error||"Unable to calculate routes");
  const intro=document.createElement("p");intro.textContent=data.note;host.append(intro);
  data.legs.filter(x=>x.origin!==x.destination).sort((a,b)=>a.minutes-b.minutes).forEach(x=>{
   const p=document.createElement("p");p.textContent=(x.origin+1)+" → "+(x.destination+1)+": "+x.minutes+" min, "+x.miles+" miles"+(x.minutes<=15?" · Nearby candidate":"");host.append(p);
  });
 }catch(e){host.textContent=e.message;}
});
$("maintenanceTravelCalculate").addEventListener("click",async()=>{
 const raw=$("maintenanceMiles").value;
 if(raw===""){$("maintenanceTravelResult").textContent="Enter verified one-way miles.";return;}
 try{
  const response=await fetch("/api/hq/maintenance/travel?one_way_miles="+encodeURIComponent(raw),{credentials:"same-origin"});
  const data=await response.json();if(!response.ok)throw Error(data.error);
  $("maintenanceTravelResult").textContent="Chargeable round-trip miles: "+data.chargeable_round_trip_miles+" | Additional travel: £"+data.travel_surcharge;
 }catch(e){$("maintenanceTravelResult").textContent=e.message;}
});
$("routeCalculate").addEventListener("click",async()=>{
 try{
  const lines=$("routeStops").value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  if(!lines.length)throw Error("Add at least one stop.");
  const stops=lines.map(line=>{
   const fields=line.split(",").map(x=>x.trim());
   if(fields.length!==3||fields.some(x=>x===""))throw Error("Each stop needs price, cleaning minutes, materials cost.");
   return {price:fields[0],clean_minutes:fields[1],materials_cost:fields[2]};
  });
  const keys=["routeMiles","routeMinutes","routeLabour","routeOverhead"];
  if(keys.some(k=>$(k).value===""))throw Error("Enter verified route miles, driving minutes, labour and overhead rates.");
  const payload={stops,total_route_miles:$("routeMiles").value,total_drive_minutes:$("routeMinutes").value,
   actual_labour_cost_per_hour:$("routeLabour").value,overhead_per_hour:$("routeOverhead").value};
  const response=await fetch("/api/hq/maintenance/economics",{method:"POST",credentials:"same-origin",
   headers:{"Content-Type":"application/json","X-CSRF-Token":document.querySelector('meta[name="csrf-token"]')?.content||""},body:JSON.stringify(payload)});
  const data=await response.json();if(!response.ok)throw Error(data.error||"Unable to calculate");
  $("routeResult").textContent="Route revenue £"+data.revenue+" | Miles £"+data.mileage_cost+" | Materials £"+data.materials+" | Labour £"+data.labour_cost+" | Overhead £"+data.allocated_overhead+" | Contribution £"+data.estimated_contribution+" | Contribution/hour £"+data.contribution_per_hour+" | Margin "+data.margin_percent+"%.";
 }catch(e){$("routeResult").textContent=e.message;}
});
$("loadMaintenance").addEventListener("click",async()=>{
 $("maintenanceStatus").textContent="Loading maintenance records…";
 try{
  const response=await fetch("/api/hq/maintenance",{credentials:"same-origin"});
  const data=await response.json();
  if(!response.ok)throw Error(data.error||"Unable to read maintenance data");
  $("maintenanceStatus").textContent="Loaded from existing HQ Sheets. Routes are suggestions, not confirmed travel times.";
  $("maintenanceSummary").textContent="Active maintenance customers: "+data.active_clients+" | Area clusters: "+data.clusters.length;
  const host=$("maintenanceClusters");host.replaceChildren();
  data.clusters.forEach(cluster=>{
   const card=document.createElement("div");card.className="card";
   const heading=document.createElement("h3");heading.textContent=cluster.name+" — "+cluster.count+" active customer(s)";card.append(heading);
   cluster.clients.forEach(c=>{
    const p=document.createElement("p");
    p.textContent=(c.customer||c.id)+" · "+(c.postcode||"Postcode needed")+" · "+(c.vehicle_slots||"?")+" vehicle(s) · "+(c.frequency||"Frequency needed")+" · next due "+(c.next_due||"Not recorded")+" · "+(c.price||"Price needed");
    card.append(p);
   });
   const note=document.createElement("p");note.textContent=cluster.suggestion;card.append(note);host.append(card);
  });
 }catch(e){$("maintenanceStatus").textContent="Unable to load maintenance records: "+e.message;}
});
function num(id){const raw=$(id).value.trim();if(!raw)return 0;const n=Number(raw);if(!Number.isFinite(n)||n<0)throw Error("Enter valid non-negative numbers.");return n}
function money(n){return new Intl.NumberFormat("en-GB",{style:"currency",currency:"GBP"}).format(n)}
function updateRate(){const tier=$("tier").value;const rate=hqData?Number(hqData.rates[tier]):(tier==="trade"?70:85);$("rateDisplay").textContent=(hqData?"Live HQ":"Fallback")+" "+tier+" labour rate: £"+rate+"/hour";}
$("tier").addEventListener("change",updateRate);
$("service").addEventListener("change",()=>{
 const item=hqData?.services?.[$("service").value];if(!item)return;
 $("hours").value=Number(item.hours);$("materials").value=Number(item.materials);
 $("consumables").value=Number(item.consumables);
 $("serviceInfo").textContent="Prefilled from live Cost Library. Pricing method: "+(item.pricing_method||"not specified")+". Multiplier (retail/trade): "+item.retail_multiplier+"/"+item.trade_multiplier+". Multipliers are NOT automatically applied by this basic cost estimator; review before approving.";
});
$("jobSelect").addEventListener("change",()=>{
 const key=$("jobSelect").value, job=hqData?.jobs?.[key];if(!job)return;
 if(startedAt!==null||seconds>0){$("timerMessage").textContent="Pause and reset the current timer before selecting another job.";return;}
 $("jobRef").value=key.slice(0,40);
 if(Number(job.estimated_hours)>0)$("estimatedHours").value=Number(job.estimated_hours);
 $("timerMessage").textContent="Loaded "+key+" · "+job.status+" · "+job.services.join(", ")+(Number(job.estimated_hours)>0?"":" · No quoted hours available; enter manually.");
 saveTimer();render();
});
$("calculate").addEventListener("click",async()=>{
 try{
  const payload={tier:$("tier").value,hours:num("hours"),materials:num("materials"),consumables:num("consumables"),overhead:num("overhead"),quoted_price:num("quoted"),service:$("service").value};
  const response=await fetch("/api/quote/estimate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),credentials:"same-origin"});
  const data=await response.json();
  if(!response.ok)throw Error(data.error||"Unable to calculate quote");
  const gap=Number(data.difference_to_target);
  $("quoteResult").innerHTML="<div class='grid'><div class='card'><small>Cost-based target (not a final quote)</small><div class='value'>"+money(Number(data.cost_based_target))+"</div></div><div class='card'><small>Labour allowance</small><div class='value'>"+money(Number(data.labour_allowance))+"</div></div><div class='card'><small>Difference to target</small><div class='value'>"+money(gap)+"</div></div></div><div class='notice'>Internal estimate only. Confirm work scope, material quantities, margin and customer approval before quoting.</div>";
 }catch(e){$("quoteResult").textContent=e.message;}
});
let seconds=0,startedAt=null,interval=null,alerted={};
try{const saved=JSON.parse(localStorage.getItem(TIMER_STORAGE_KEY)||"null");if(saved&&Number.isFinite(saved.seconds)&&saved.seconds>=0&&saved.seconds<1e8){seconds=saved.seconds;if(typeof saved.ref==="string")$("jobRef").value=saved.ref.slice(0,40);if(Number.isFinite(saved.estimate)&&saved.estimate>0)$("estimatedHours").value=saved.estimate;if(saved.alerted&&typeof saved.alerted==="object")alerted=saved.alerted;}}catch(e){}
function saveTimer(){try{localStorage.setItem(TIMER_STORAGE_KEY,JSON.stringify({seconds:elapsed(),ref:$("jobRef").value,estimate:Number($("estimatedHours").value),alerted}));}catch(e){}}
function notifyTimer(message){$("timerMessage").textContent=message;if("Notification" in window&&Notification.permission==="granted"&&document.hidden){try{new Notification("Tommy Tints HQ",{body:message});}catch(e){}}}
$("jobRef").addEventListener("change",saveTimer);
$("estimatedHours").addEventListener("change",()=>{alerted={};saveTimer();render();});

function elapsed(){return seconds+(startedAt===null?0:Math.floor((Date.now()-startedAt)/1000))}
function format(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;return [h,m,sec].map(v=>String(v).padStart(2,"0")).join(":")}
function render(){
 const used=elapsed();$("timer").textContent=format(used);
 const est=Number($("estimatedHours").value)*3600;
 if(!Number.isFinite(est)||est<=0){$("timeRemaining").textContent="Enter a valid estimated time.";return;}
 const percent=used/est*100;
 $("timeProgress").setAttribute("aria-valuenow",String(Math.min(100,Math.round(percent))));
 $("timeFill").style.width=Math.min(100,percent)+"%";
 $("timeFill").style.background=percent>=100?"#d43b48":percent>=75?"#eab44c":"#319e76";
 $("timeRemaining").textContent=percent>=100?"Over estimate by "+format(used-est):format(Math.ceil(est-used))+" remaining · "+Math.floor(percent)+"% of estimate used";
 if(startedAt!==null){for(const threshold of [75,90,100]){if(percent>=threshold&&!alerted[threshold]){alerted[threshold]=true;notifyTimer(threshold===100?"Estimated labour time reached. Review scope before discussing any extra charges.":threshold+"% of estimated labour used. Check progress and remaining work.");saveTimer();}}}
}
$("start").addEventListener("click",()=>{if(startedAt===null){startedAt=Date.now();interval=setInterval(()=>{render();saveTimer();},1000);$("timerMessage").textContent="Running";}});
$("pause").addEventListener("click",()=>{if(startedAt!==null){seconds=elapsed();startedAt=null;clearInterval(interval);render();saveTimer();$("timerMessage").textContent="Paused";}});
$("reset").addEventListener("click",()=>{if(!confirm("Reset this timer?"))return;seconds=0;startedAt=null;alerted={};clearInterval(interval);render();saveTimer();$("timerMessage").textContent="Reset";});
$("copyTime").addEventListener("click",async()=>{const text=($("jobRef").value||"Job")+" — "+format(elapsed())+" ("+(elapsed()/3600).toFixed(2)+" hours)";try{await navigator.clipboard.writeText(text);$("timerMessage").textContent="Time copied";}catch(e){$("timerMessage").textContent="Copy unavailable: "+text;}});
let materialLines=[];
let currentMaterialJob="";
let currentMaterialRevision=0, materialLoadRequestId=0;
function matKey(){return "hq-materials-job:"+currentMaterialJob;}
function saveMat(){try{localStorage.setItem(matKey(),JSON.stringify({lines:materialLines,sale:$("matSale").value,labour:$("matLabour").value,actual_labour_rate:$("matActualRate").value,overhead:$("matOverhead").value,other_costs:$("matOther").value,minimum_margin_percent:$("matMinMargin").value,description:$("matDescription").value,stages:$("matStages").value}));}catch(e){}}
function loadMat(){try{const d=JSON.parse(localStorage.getItem(matKey())||"{}");materialLines=Array.isArray(d.lines)?d.lines:[];$("matSale").value=d.sale||"";$("matLabour").value=d.labour||"";$("matActualRate").value=d.actual_labour_rate||"";$("matOverhead").value=d.overhead||"";$("matOther").value=d.other_costs||"";$("matMinMargin").value=d.minimum_margin_percent||"";$("matDescription").value=d.description||"";$("matStages").value=d.stages||"";}catch(e){materialLines=[];}showMaterials();}
$("matUseManual").addEventListener("click",async()=>{
 const key=$("matManualJob").value.trim().toUpperCase();
 if(!/^[A-Z0-9][A-Z0-9 _-]{0,79}$/.test(key)){$("syncStatus").textContent="Enter a valid job reference (letters, numbers, spaces, hyphens or underscores).";return;}
 if(!Array.from($("matJob").options).some(o=>o.value===key))$("matJob").add(new Option(key+" (manual — not verified against Sheets)",key));
 $("matJob").value=key;currentMaterialJob=key;currentMaterialRevision=0;$("matHqPriceHint").textContent="Manual job reference: HQ selling price not verified.";loadMat();await loadServerMat();
});
$("matJob").addEventListener("change",async()=>{currentMaterialJob=$("matJob").value;currentMaterialRevision=0;$("matHqPriceHint").textContent="Price not verified for this selection. Open an internal HQ job card to show its selling price.";loadMat();await loadServerMat();});
async function loadServerMat(){
 const key=currentMaterialJob,requestId=++materialLoadRequestId;
 if(!key)return;
 const saveButton=$("syncMaterials");saveButton.disabled=true;
 let loaded=false;
 try{
  const res=await fetch("/api/job-materials/"+encodeURIComponent(key));
  const d=await res.json();if(!res.ok)throw Error(d.error||"Unable to load saved job");
  if(key!==currentMaterialJob||requestId!==materialLoadRequestId)return;
  currentMaterialRevision=d.revision;loaded=true;
  if(d.data){materialLines=d.data.lines||[];$("matSale").value=d.data.sale||"";$("matLabour").value=d.data.labour||"";$("matActualRate").value=d.data.actual_labour_rate||"";$("matOverhead").value=d.data.overhead||"";$("matOther").value=d.data.other_costs||"";$("matMinMargin").value=d.data.minimum_margin_percent||"";$("matDescription").value=d.data.description||"";$("matStages").value=d.data.stages||"";showMaterials();$("syncStatus").textContent="Loaded saved HQ job record ("+d.updated_at+", revision "+d.revision+").";}
  else $("syncStatus").textContent="No saved HQ record for this job. Local values are shown; saving will create version 1.";
 }catch(e){if(key===currentMaterialJob&&requestId===materialLoadRequestId)$("syncStatus").textContent=e.message+"; local values retained. Saving is disabled until the record can be checked.";}
 finally{if(key===currentMaterialJob&&requestId===materialLoadRequestId)saveButton.disabled=!loaded;}
}
$("syncMaterials").addEventListener("click",async()=>{
 const key=currentMaterialJob;
 if(!key){$("syncStatus").textContent="Select an existing job before saving to HQ.";return;}
 const saveButton=$("syncMaterials");saveButton.disabled=true;
 try{
  const csrf=document.querySelector('input[name="csrf"]').value;
  const payload={lines:materialLines,sale:$("matSale").value,labour:$("matLabour").value,actual_labour_rate:$("matActualRate").value,overhead:$("matOverhead").value,other_costs:$("matOther").value,minimum_margin_percent:$("matMinMargin").value,description:$("matDescription").value,stages:$("matStages").value,expected_revision:currentMaterialRevision};
  const res=await fetch("/api/job-materials/"+encodeURIComponent(key),{method:"PUT",headers:{"Content-Type":"application/json","X-HQ-CSRF":csrf},body:JSON.stringify(payload)});
  const d=await res.json();if(!res.ok)throw Error(d.error||"Save failed");
  if(key===currentMaterialJob){currentMaterialRevision=d.revision;$("syncStatus").textContent="Saved securely to HQ at "+d.updated_at+" (revision "+d.revision+"). Customer breakdown fields saved.";}
 }catch(e){if(key===currentMaterialJob)$("syncStatus").textContent="Not saved: "+e.message;}
 finally{if(key===currentMaterialJob)saveButton.disabled=false;}
});
["matSale","matLabour","matActualRate","matOverhead","matOther","matMinMargin","matDescription","matStages"].forEach(id=>$(id).addEventListener("change",showMaterials));
try{const saved=JSON.parse(localStorage.getItem("hq-material-lines-v1")||"[]");if(Array.isArray(saved)&&saved.length<=100)materialLines=saved.filter(x=>x&&typeof x.name==="string"&&Number.isFinite(Number(x.cost)));}catch(e){}
function showMaterials(){
 saveMat();
 $("matLines").replaceChildren();
 materialLines.forEach((line,i)=>{
  const p=document.createElement("p");p.textContent=line.name+" — "+money(Number(line.cost))+" · "+line.detail;
  const b=document.createElement("button");b.textContent="Remove";b.addEventListener("click",()=>{materialLines.splice(i,1);showMaterials();});
  p.append(" ",b);$("matLines").append(p);
 });
 const cost=materialLines.reduce((sum,x)=>sum+Number(x.cost),0);
 $("matTotal").textContent="Total material cost: "+money(cost);
 if($("matSale").value!=="" && Number($("matOffer").max)<Number($("matSale").value)*1.5){$("matOffer").max=String(Math.ceil(Number($("matSale").value)*1.5/5)*5);}
 refreshNegotiation();
 const sale=$("matSale").value, hours=$("matLabour").value;
 const actual=$("matActualRate").value,overhead=$("matOverhead").value,other=$("matOther").value;
 if(sale===""||hours===""){
  $("matProfit").textContent="Enter selling price and actual hours to calculate job contribution.";
  $("matBreakdown").textContent="";
 }else if([sale,hours,actual,overhead,other].some(v=>v!==""&&(!Number.isFinite(Number(v))||Number(v)<0))){
  $("matProfit").textContent="Check cost inputs: use nonnegative numbers.";
  $("matBreakdown").textContent="";
 }else{
  const labourCost=Number(hours)*Number(actual||0), overheadCost=Number(overhead||0), otherCost=Number(other||0);
  const contribution=Number(sale)-cost-labourCost-overheadCost-otherCost;
  const incomplete=[actual,overhead,other].some(v=>v==="");
  $("matProfit").textContent=(incomplete?"INCOMPLETE ESTIMATE — missing costs count as £0. ":"")+"Estimated contribution before tax: "+money(contribution)+"; margin "+(Number(sale)>0?(100*contribution/Number(sale)).toFixed(1)+"%":"N/A")+". Not accounting net profit.";
  $("matBreakdown").textContent="Revenue "+money(Number(sale))+" | Materials "+money(cost)+" | Actual labour "+money(labourCost)+" | Allocated overhead "+money(overheadCost)+" | Other costs "+money(otherCost)+". Owner pay, depreciation and tax may still need accounting treatment.";
 }
}
function refreshNegotiation(){
 const price=Number($("matOffer").value);
 $("matOfferLabel").textContent="Proposed price: "+money(price);
 const target=$("matMinMargin").value;
 const fields=[$("matSale").value,$("matLabour").value,$("matActualRate").value,$("matOverhead").value,$("matOther").value,target];
 if(fields.some(v=>v==="")){
  $("matNegotiation").textContent="HOLD — enter selling price, labour hours, actual labour rate, overhead, other costs and minimum margin.";
  $("matQuiet").textContent="Quiet-period decision unavailable until all costs are entered.";
  return;
 }
 const materials=materialLines.reduce((a,x)=>a+Number(x.cost),0);
 const labour=Number($("matLabour").value)*Number($("matActualRate").value);
 const overhead=Number($("matOverhead").value),other=Number($("matOther").value),min=Number(target);
 if([materials,labour,overhead,other,min].some(x=>!Number.isFinite(x)||x<0)||min>=100){$("matNegotiation").textContent="HOLD — invalid inputs.";return;}
 const floor=(materials+labour+overhead+other)/(1-min/100);
 const cash=materials+labour+other;
 $("matNegotiation").textContent=price>=floor?"GREEN — meets your sustainable margin target. Minimum "+money(floor)+".":"RED — below sustainable floor "+money(floor)+". Hold firm.";
 $("matQuiet").textContent=price>=floor?"Covers recorded costs and target margin.":price>=cash?"Cash-positive on recorded incremental costs, but DOES NOT cover allocated overhead or target margin. Exception review only — not a sustainable price.":"Below recorded incremental cost "+money(cash)+". Reject.";
}
$("matOffer").addEventListener("input",refreshNegotiation);
$("matSale").addEventListener("change",()=>{
 const sale=Number($("matSale").value);
 if(Number.isFinite(sale)&&sale>=0){
  $("matOffer").max=String(Math.max(2000,Math.ceil(sale*1.5/5)*5));
  $("matOffer").value=String(Math.min(Number($("matOffer").max),Math.round(sale/5)*5));
  refreshNegotiation();
 }
});
$("matPrint").addEventListener("click",()=>{
 if(!currentMaterialJob){$("syncStatus").textContent="Select a job and save it to HQ before printing.";return;}
 window.open("/customer-breakdown/"+encodeURIComponent(currentMaterialJob),"_blank","noopener");
});
$("addMaterial").addEventListener("click",async()=>{
 try{
  const method=$("matMethod").value;
  const line={name:$("matName").value,method};
  if(method==="direct")line.cost=num("matDirect");
  else if(method==="clearcoat_kit"){line.purchase_price=num("matPrice");line.clear_ml=num("matClear");line.hardener_ml=num("matHardener");line.used_quantity=num("matUsed");line.waste_percent=num("matWaste");}
  else{line.purchase_price=num("matPrice");line.package_quantity=num("matPack");
   if(method==="quantity"){line.used_quantity=num("matUsed");line.waste_percent=num("matWaste");}
   else line.preset=$("matPreset").value;}
  const res=await fetch("/api/materials/calculate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({lines:[line]})});
  const data=await res.json();if(!res.ok)throw Error(data.error||"Calculation failed");
  materialLines.push(data.lines[0]);$("matStatus").textContent="Added "+line.name;showMaterials();
 }catch(e){$("matStatus").textContent=e.message;}
});
$("clearMaterials").addEventListener("click",()=>{materialLines=[];showMaterials();$("matStatus").textContent="Cleared";});
$("loadSheets").addEventListener("click",async()=>{
 $("sheetsStatus").textContent="Loading current HQ records…";
 try{
  const res=await fetch("/api/hq/pricing",{credentials:"same-origin"});
  const data=await res.json();
  if(!res.ok)throw Error(data.error||"Unable to read Google Sheets");
  const services=Object.keys(data.services||{}), jobs=Object.keys(data.jobs||{});
  hqData=data;
  $("service").replaceChildren(new Option("Manual estimate — no service selected",""));
  services.forEach(name=>$("service").add(new Option(name,name)));
  const timerJobSelect=$("jobSelect");
  if(timerJobSelect){
   timerJobSelect.replaceChildren(new Option("Manual timer — no job selected",""));
   jobs.forEach(key=>timerJobSelect.add(new Option(key+" · "+(data.jobs[key].status||"No status"),key)));
  }
  const materialJobSelect=$("matJob");
  if(materialJobSelect){
   const selected=materialJobSelect.value;
   materialJobSelect.replaceChildren(new Option("Unassigned",""));
   jobs.forEach(key=>materialJobSelect.add(new Option(key+" · "+(data.jobs[key].status||"No status"),key)));
   if(selected && jobs.includes(selected))materialJobSelect.value=selected;
   else if(selected){materialJobSelect.add(new Option(selected+" (manual)",selected));materialJobSelect.value=selected;}
  }
  $("matJobHelp").textContent=jobs.length+" existing job references loaded from Google Sheets.";
  updateRate();
  $("serviceInfo").textContent="Choose a live service to prefill hours and materials. Review any multipliers and pricing rules manually.";
  $("sheetsStatus").textContent="Connected (read-only).";
  $("sheetsSummary").textContent=services.length+" Cost Library services and "+jobs.length+" job references loaded. Retail £"+data.rates.retail+"/hr; trade £"+data.rates.trade+"/hr.";
 }catch(err){$("sheetsStatus").textContent=err.message+" — connect Google Sheets from the Jobs page, then press Load HQ records.";$("matJobHelp").textContent="Job list unavailable: "+err.message+". Reconnect Google Sheets on the Jobs page, or enter the exact job reference manually."; }
});
$("calculate").click();render();
loadMat();$("loadSheets").click();
</script></body></html>"""
@app.after_request
def hq_cache_policy(response):
    path = request.path
    is_versioned_icon = response.mimetype == "image/png" and path in {
        "/hq-icon-180.png", "/hq-icon-512.png", "/hq-icon-1024.png", "/favicon.ico",
    } and request.args.get("v") == ICON_VERSION
    is_versioned_script = (path == "/static/hq-updates.js"
                           and request.args.get("v") == UPDATE_SCRIPT_VERSION)
    if response.status_code in (200, 206, 304) and (is_versioned_icon or is_versioned_script):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif (response.mimetype == "text/html" or path.startswith(("/api/", "/auth/"))
          or path in {"/", "/install-hq", "/login", "/logout", "/static/manifest.webmanifest"}
          or path.endswith((".js", ".mjs"))):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/hq/version")
def hq_version():
    return jsonify(version=build_version())


def _with_updates(html, version, automatic=False):
    notice = render_template_string("""<div id="hq-update-banner" hidden data-build="{{ version }}"
data-automatic="{{ 'true' if automatic else 'false' }}" style="position:fixed;bottom:calc(12px + env(safe-area-inset-bottom,0px));right:12px;left:auto;max-width:calc(100% - 24px);z-index:1000;background:#151e2b;color:#fff;border:1px solid #53637a;border-radius:10px;padding:12px">
<span role="status">Update available</span>
<button type="button" id="hq-update-refresh">Refresh</button></div>
<script defer src="/static/hq-updates.js?v={{ script_version }}"></script>""",
        version=version, automatic=automatic, script_version=UPDATE_SCRIPT_VERSION)
    return html.replace("</body>", notice + "</body>", 1)


@app.get("/install-hq")
def install_hq():
    """Public, no-data installation landing with explicit Apple touch icon."""
    return _with_updates("""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tommy Tints HQ</title><meta name="theme-color" content="#0b0f16">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Tommy Tints HQ">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" sizes="180x180" href="/hq-icon-180.png?v=20261117d">
<link rel="apple-touch-icon-precomposed" href="/hq-icon-180.png?v=20261117d">
<link rel="icon" type="image/png" sizes="180x180" href="/favicon.ico?v=20261117d">
<link rel="manifest" href="/static/manifest.webmanifest?v=20261117d">
<style>body{background:#0b0f16;color:#fff;font:16px system-ui,sans-serif;
margin:0;min-height:100vh;display:grid;place-items:center;text-align:center}
main{padding:28px}img{width:180px;height:180px;border-radius:32px}
h1{font-size:28px}p{color:#b9c3d2;line-height:1.5}
a{display:inline-block;background:#d51a27;color:#fff;text-decoration:none;
padding:14px 22px;border-radius:12px;font-weight:700}</style>
</head><body><main><img src="/hq-icon-180.png?v=20261117d"
alt="Tommy Tints HQ icon"><h1>Tommy Tints HQ</h1>
<p>To install: Safari Share → Add to Home Screen.<br>
The icon preview should match the logo above.</p>
<a href="/">Open HQ</a></main></body></html>""", build_version(), automatic=True)

@app.get("/")
def home():
    if not configured():
        return "Authentication configuration required. Private HQ data remains disabled.", 503
    active = active_session()
    if active is None:
        # Keep the root URL on a branded HTML page so Safari can discover the
        # Apple touch icon before the owner signs in through Google OAuth.
        return _with_updates("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tommy Tints HQ</title>
<meta name="theme-color" content="#0b0f16">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Tommy Tints HQ">
<link rel="apple-touch-icon" sizes="180x180" href="/hq-icon-180.png?v=20261117d">
<link rel="icon" type="image/png" sizes="180x180" href="/favicon.ico?v=20261117d">
<link rel="manifest" href="/static/manifest.webmanifest?v=20261117d">
<style>body{background:#0b0f16;color:#fff;font-family:system-ui,sans-serif;
display:grid;place-content:center;min-height:90vh;text-align:center}
img{width:170px;max-width:50vw;margin:auto}
a{background:#d51a27;color:white;padding:14px 22px;border-radius:12px;
text-decoration:none;font-weight:700}</style></head><body>
<img src="/hq-icon-180.png?v=20261117d" alt="Tommy Tints HQ logo">
<h1>Tommy Tints HQ</h1><p>Private business dashboard</p>
<p><button type="button" data-passkey="login">Sign in with a passkey</button></p>
<p id="hq-passkey-status" role="status"></p>
<p><a href="/login">Sign in with Google</a></p><script defer src="/static/hq-passkeys.js"></script></body></html>""", build_version(), automatic=True)
    version = build_version()
    html = render_template_string(PAGE.replace("__HQ_CSRF_PLACEHOLDER__", session["csrf"]).replace("</main>", '<form action="/logout" method="post"><input type="hidden" name="csrf" value="' + session["csrf"] + '"><button type="submit">Lock HQ / Sign out</button></form></main>'), hq_build=version, owner_passkeys=registered_passkeys(active))
    return _with_updates(html, version)
@app.post("/api/quote/estimate")
@required
def quote_estimate():
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid request"), 400
    try:
        service = data.get("service")
        if service:
            entry = _google_connection("sheets")
            if not entry or entry[1] <= time.time():
                return jsonify(error="Reconnect Google Sheets to quote from the live Cost Library"), 401
            workbook = read_workbook_with_access_token(entry[0])
            item = workbook["services"].get(service)
            if item is None:
                return jsonify(error="Selected service is not in the current Cost Library"), 400
            data = {**data, "hours":item["hours"], "materials":item["materials"],
                    "consumables":item["consumables"]}
            result = calculate_quote(data, rates=workbook["rates"])
            result["source_service"] = service
            result["pricing_method"] = item["pricing_method"]
            result["multiplier_applied"] = False
            return jsonify(result)
        return jsonify(calculate_quote(data))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

# Short-lived, process-local tokens only: no Google credentials in browser cookies.
# A restart or multi-worker request can require reconnection. Do not enable as
# a production integration until a durable encrypted server-side store exists.
_google_tokens = {}
_google_tokens_lock = Lock()
SHEETS_SCOPE = "openid email profile https://www.googleapis.com/auth/spreadsheets.readonly"
CALENDAR_SCOPE = "openid email profile https://www.googleapis.com/auth/calendar.events.readonly"
_calendar_tokens = {}
_calendar_tokens_lock = Lock()

def _google_connection(provider):
    stored = connection(provider, session.get("google_sub"))
    if stored:
        return stored
    tokens, lock = (_google_tokens, _google_tokens_lock) if provider == "sheets" else (_calendar_tokens, _calendar_tokens_lock)
    with lock:
        return tokens.get(session.get(f"{provider}_token_key"))


app.extensions["hq_sheets_connection"] = lambda: _google_connection("sheets")
app.extensions["hq_calendar_connection"] = lambda: _google_connection("calendar")


@app.get("/auth/sheets/connect")
@required
def sheets_connect():
    return google.authorize_redirect(
        url_for("sheets_callback", _external=True, _scheme="https"),
        scope=SHEETS_SCOPE, prompt="consent", access_type="offline")

@app.get("/auth/sheets/callback")
@required
def sheets_callback():
    token = google.authorize_access_token()
    identity = token.get("userinfo") or google.userinfo(token=token).json()
    expected = os.environ["HQ_ALLOWED_GOOGLE_EMAIL"].strip().casefold()
    if not identity.get("email_verified") or not secrets.compare_digest(
            str(identity.get("email", "")).strip().casefold(), expected):
        return "Google account not authorised", 403
    scope = token.get("scope", "")
    if "https://www.googleapis.com/auth/spreadsheets.readonly" not in scope.split():
        return "Google Sheets read-only permission was not granted", 403
    save_connection("sheets", session.get("google_sub"), token)
    key = secrets.token_urlsafe(32)
    with _google_tokens_lock:
        _google_tokens[key] = (token["access_token"], time.time() + min(int(token.get("expires_in", 3600)), 3600))
    session["sheets_token_key"] = key
    return redirect("/")

@app.get("/auth/calendar/connect")
@required
def calendar_connect():
    return google.authorize_redirect(
        url_for("calendar_callback",_external=True,_scheme="https"),
        scope=CALENDAR_SCOPE,prompt="consent", access_type="offline")

@app.get("/auth/calendar/callback")
@required
def calendar_callback():
    token=google.authorize_access_token()
    identity=token.get("userinfo") or google.userinfo(token=token).json()
    expected=os.environ["HQ_ALLOWED_GOOGLE_EMAIL"].strip().casefold()
    if not identity.get("email_verified") or not secrets.compare_digest(str(identity.get("email","")).strip().casefold(),expected):
        return "Google account not authorised",403
    if "https://www.googleapis.com/auth/calendar.events.readonly" not in token.get("scope","").split():
        return "Google Calendar read-only permission was not granted",403
    save_connection("calendar", session.get("google_sub"), token)
    key=secrets.token_urlsafe(32)
    with _calendar_tokens_lock:
        _calendar_tokens[key]=(token["access_token"],time.time()+min(int(token.get("expires_in",3600)),3600))
    session["calendar_token_key"]=key
    return redirect("/")

@app.post("/api/hq/maintenance/calendar-live-check")
@required
def hq_calendar_live_check():
    if not request.is_json:return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error="Invalid request"),400
    entry = _google_connection("calendar")
    if not entry or entry[1]<=time.time():
        return jsonify(error="Connect Google Calendar read-only first",connect_url="/auth/calendar/connect"),401
    try:
        result = live_conflicts(entry[0], data.get("start"), data.get("end"))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        record_check("calendar", "Calendar availability could not be verified")
        return jsonify(error=str(exc)), 409
    except Exception:
        record_check("calendar", "Calendar read failed")
        app.logger.warning("Read-only Calendar check failed")
        return jsonify(error="Unable to verify Google Calendar availability"), 502
    if not record_check("calendar"):
        return jsonify(error=("Calendar was read, but HQ could not record the " 
                              "readiness evidence. Check Integration Health storage; " 
                              "do not treat this as a verified read.")), 503
    return jsonify(result)

@app.post("/api/materials/calculate")
@required
def materials_calculate():
    if not request.is_json:
        return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error="Invalid request"),400
    try:
        return jsonify(calculate_materials(data.get("lines")))
    except (ValueError,TypeError,AttributeError) as exc:
        return jsonify(error=str(exc)),400

@app.route("/api/job-materials/<path:job_key>",methods=["GET","PUT"])
@required
def job_materials(job_key):
    if not job_key or len(job_key)>100 or "/" in job_key:
        return jsonify(error="Invalid job reference"),400
    owner=str(session.get("google_sub") or "")
    if not owner:
        return jsonify(error="Owner identity missing"),403
    try:
        if request.method=="GET":
            return jsonify(read_log(owner,job_key))
        if not secrets.compare_digest(request.headers.get("X-HQ-CSRF",""),session.get("csrf","")):
            return jsonify(error="Invalid security token"),403
        data=request.get_json(silent=True) if request.is_json else None
        if not isinstance(data,dict) or not isinstance(data.get("lines"),list) or len(data["lines"])>100:
            return jsonify(error="Invalid material log"),400
        if type(data.get("expected_revision")) is not int or not 0 <= data["expected_revision"] < 2**63 - 1:
            return jsonify(error="Expected material log revision is required"),400
        for field, maximum in (("description",200),("stages",4000),("minimum_margin_percent",20)):
            if not isinstance(data.get(field),str) or len(data[field])>maximum:
                return jsonify(error="Invalid "+field),400
        for line in data["lines"]:
            if not isinstance(line,dict) or not isinstance(line.get("name"),str) or len(line["name"])>100:
                return jsonify(error="Invalid material line"),400
            try:
                value=float(line["cost"])
                if not (0<=value<1000000): raise ValueError()
            except (ValueError,TypeError,KeyError):
                return jsonify(error="Invalid material cost"),400
        # Revision is transport metadata, never part of the printable record.
        expected=data.pop("expected_revision")
        return jsonify(write_log(owner,job_key,data,expected_revision=expected))
    except (RuntimeError,OSError):
        app.logger.exception("Persistent materials storage unavailable")
        return jsonify(error="Persistent storage unavailable"),503
    except StaleMaterialLogError as exc:
        return jsonify(error=str(exc)),409
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),400

@app.post("/api/job-profitability/calculate")
@required
def job_profitability_calculate():
    if not request.is_json: return jsonify(error="JSON body required"),415
    try:
        return jsonify(profitability(request.get_json(silent=True)))
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),400

@app.post("/api/job-negotiation/calculate")
@required
def job_negotiation_calculate():
    if not request.is_json: return jsonify(error="JSON body required"),415
    try: return jsonify(evaluate_negotiation(request.get_json(silent=True)))
    except (ValueError,TypeError) as exc: return jsonify(error=str(exc)),400

@app.get("/customer-breakdown/<path:job_key>")
@required
def customer_breakdown(job_key):
    from markupsafe import escape
    if not job_key or len(job_key)>100 or "/" in job_key: return "Invalid job reference",400
    owner=str(session.get("google_sub") or "")
    if not owner: return "Owner identity missing",403
    try: record=read_log(owner,job_key).get("data")
    except (RuntimeError,OSError,sqlite3.Error): return "Job storage unavailable",503
    if not record: return "Save this job in HQ before printing.",404
    description=str(record.get("description") or "Vehicle work completed")[:200]
    stages=[str(x).strip()[:200] for x in str(record.get("stages") or "").splitlines() if str(x).strip()][:30]
    if not stages: return "Add customer-facing work stages and save the job before printing.",400
    # A 32-character internal HQ work ID must use its approved HQ selling
    # price, never the independently editable sale in the costing log.
    # Legacy manual/Sheets job keys retain their existing print behaviour.
    if len(job_key) == 32 and all(ch in "0123456789abcdef" for ch in job_key):
        try:
            work = read_work_record(job_key)
        except LookupError:
            return "Internal HQ job not found", 404
        except (RuntimeError, OSError, sqlite3.Error):
            return "Job storage unavailable", 503
        pence = work["price_pence"]
        if (work["status"] not in ("approved", "in_progress", "completed")
                or type(pence) is not int or not 0 <= pence <= 100000000):
            return "Approve the internal HQ price before preparing a customer breakdown.", 409
        price_text = f"£{pence // 100:,}.{pence % 100:02d}"
    else:
        price = record.get("sale")
        try:
            from decimal import Decimal
            amount = Decimal(str(price))
            price_text = ("£" + format(amount, ",.2f")
                          if amount.is_finite() and 0 <= amount <= 1000000
                          else "Not recorded")
        except Exception:
            price_text = "Not recorded"
    response = make_response(render_template_string("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job breakdown</title><style>body{font:16px Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}h1{border-bottom:3px solid #b71c1c;padding-bottom:12px}li{margin:12px 0}.small{font-size:13px;color:#555}@media print{button{display:none}body{margin:20px auto}}</style></head><body><h1>Tommy Tints — Job Work Summary</h1><p><strong>Job reference:</strong> {{ job }}</p><p><strong>Work:</strong> {{ description }}</p><h2>Work completed</h2><ol>{% for stage in stages %}<li>{{ stage }}</li>{% endfor %}</ol><p><strong>Agreed job price:</strong> {{ price }}</p><p class="small">Summary of work undertaken; not an itemised supplier-cost disclosure, VAT invoice or proof of payment.</p><button onclick="window.print()">Print / Save PDF</button></body></html>""",job=job_key,description=description,stages=stages,price=price_text))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.post("/api/hq/maintenance/check-window")
@required
def hq_maintenance_check_window():
    if not request.is_json:return jsonify(error="JSON body required"),415
    try:return jsonify(check_booking_window(request.get_json(silent=True)))
    except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400

@app.get("/api/hq/maintenance/suggestions")
@required
def hq_maintenance_suggestions():
    entry = _google_connection("sheets")
    if not entry or entry[1]<=time.time():
        return jsonify(error="Google Sheets consent required or expired",connect_url="/auth/sheets/connect"),401
    try:
        data=maintenance_data(entry[0])
        return jsonify(booking_suggestions(data["clients"],data["visits"]))
    except Exception:
        app.logger.exception("Maintenance booking suggestions failed")
        return jsonify(error="Unable to calculate maintenance booking suggestions"),502

@app.post("/api/hq/maintenance/driving-matrix")
@required
def hq_maintenance_driving_matrix():
    if not secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "not-set")):
        return jsonify(error="Invalid security token"),403
    if request.content_length is None or request.content_length > 4096:
        return jsonify(error="Invalid route request"),400
    if not request.is_json:return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error="Invalid request"),400
    addresses=data.get("addresses")
    if (not isinstance(addresses,list) or not 2<=len(addresses)<=10
            or any(not isinstance(x,str) or not 12<=len(x.strip())<=240 for x in addresses)):
        return jsonify(error="Supply 2 to 10 full addresses"),400
    if not os.environ.get("HQ_GOOGLE_MAPS_API_KEY"):
        return jsonify(error="Google Routes API key is not configured"),503
    try:
        if not reserve_route_request():
            return jsonify(error="Route usage limit reached (20/hour, 100/day). Retry later."),429
        return jsonify(driving_matrix(addresses))
    except ValueError as exc:return jsonify(error=str(exc)),400
    except RuntimeError as exc:return jsonify(error=str(exc)),503
    except Exception:
        app.logger.warning("Google Routes matrix failed; provider details withheld")
        return jsonify(error="Unable to retrieve driving times; verify addresses and API configuration"),502

@app.get("/api/hq/maintenance/travel")
@required
def hq_maintenance_travel():
    try: return jsonify(travel_charge(request.args.get("one_way_miles"),"10","0.56"))
    except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400

@app.post("/api/hq/maintenance/economics")
@required
def hq_maintenance_economics():
    if not request.is_json:return jsonify(error="JSON body required"),415
    try:return jsonify(route_economics(request.get_json(silent=True)))
    except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400

@app.get("/api/hq/maintenance")
@required
def hq_maintenance():
    entry = _google_connection("sheets")
    if not entry or entry[1]<=time.time():
        return jsonify(error="Google Sheets consent required or expired",connect_url="/auth/sheets/connect"),401
    try:
        result = maintenance_data(entry[0])
    except Exception:
        record_check("maintenance_sheets", "Maintenance workbook read failed")
        app.logger.warning("Read-only maintenance Sheets sync failed")
        return jsonify(error="Unable to read existing maintenance sheets"), 502
    if not record_check("maintenance_sheets"):
        return jsonify(error=("Maintenance Sheets was read, but HQ could not record " 
                              "the readiness evidence. Check Integration Health storage; " 
                              "do not treat this as a verified read.")), 503
    return jsonify(result)

@app.get("/hq/finance")
@required
def hq_finance_page():
    return render_template('finance_command.html',csrf=session.get('csrf',''))

@app.get('/hq/finance/sheets')
@required
def hq_finance_sheets_reference():
    return render_template('finance.html')

@app.get("/api/hq/finance")
@required
def hq_finance_data():
    from hq_sheets_live import read_finance
    entry = _google_connection("sheets")
    if not entry and not sheets_configured():
        return jsonify(error="Google Sheets connection required", connect_url="/auth/sheets/connect"), 401
    try:
        result = read_finance(entry[0] if entry else None)
    except Exception:
        record_check("finance", "Finance workbook read failed")
        return jsonify(error="Unable to refresh financial spreadsheet; check connection and permissions"), 502
    if not record_check("finance"):
        return jsonify(error=("Finance Sheets was read, but HQ could not record " 
                              "the readiness evidence. Check Integration Health storage; " 
                              "do not treat this as a verified read.")), 503
    return jsonify(result)

@app.get("/api/hq/pricing")
@required
def hq_pricing():
    entry = _google_connection("sheets")
    if not entry or entry[1] <= time.time():
        return jsonify(error="Google Sheets consent required or expired", connect_url="/auth/sheets/connect", connected=False), 401
    try:
        result = read_workbook_with_access_token(entry[0])
    except Exception:
        record_check("pricing_sheets", "Pricing workbook read failed")
        app.logger.warning("Read-only Google Sheets sync failed")
        return jsonify(error="Unable to read HQ spreadsheet. Reconnect Google Sheets and check API permissions."), 502
    if not record_check("pricing_sheets"):
        return jsonify(error=("Pricing Sheets was read, but HQ could not record " 
                              "the readiness evidence. Check Integration Health storage; " 
                              "do not treat this as a verified read.")), 503
    return jsonify(result)

_gmail_tokens = {}
_gmail_lock = Lock()


def _gmail_token():
    stored = connection("gmail", session.get("google_sub"))
    if stored:
        return stored[0]
    with _gmail_lock:
        entry = _gmail_tokens.get(session.get("gmail_token_key"))
        return entry[0] if entry and entry[1] > time.time() else None


@app.get("/hq/gmail-setup")
@required
def hq_gmail_setup():
    import re
    client_id=os.environ.get("GOOGLE_CLIENT_ID", "")
    if not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com",client_id):
        client_id="Not configured as a recognised Google OAuth web client ID"
    return render_template("gmail_setup.html", client_id=client_id,
        callback=url_for("hq_gmail_callback",_external=True,_scheme="https"), mailbox=gmail.MAILBOX)

@app.get("/auth/gmail/connect")
@required
def hq_gmail_connect():
    return google.authorize_redirect(
        url_for("hq_gmail_callback", _external=True, _scheme="https"),
        scope="openid email profile " + gmail.SCOPE,
        login_hint=gmail.MAILBOX, prompt="select_account consent", access_type="offline")


@app.get("/auth/gmail/callback")
@required
def hq_gmail_callback():
    try:
        token = google.authorize_access_token()
    except Exception:
        record_check("gmail", "Gmail OAuth exchange failed")
        return "Google authorisation exchange failed. Restart Connect Gmail; do not reuse the callback URL.", 502
    if not isinstance(token, dict) or gmail.SCOPE not in str(token.get("scope", "")).split():
        record_check("gmail", "Gmail read-only scope missing")
        return "Gmail read-only access was not granted. Reconnect and grant gmail.readonly.", 403
    try:
        access_token = token["access_token"]
        if not isinstance(access_token, str) or not access_token:
            raise ValueError()
        expiry = time.time() + min(int(token.get("expires_in", 3600)), 3600)
        if expiry <= time.time():
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        record_check("gmail", "Gmail OAuth token invalid or expired")
        return "Google returned an invalid or expired token. Restart Connect Gmail.", 401
    try:
        gmail.verify_mailbox(access_token)
    except requests.exceptions.HTTPError as exc:
        diagnostic, action = gmail.profile_error_diagnostic(exc)
        record_check("gmail", diagnostic)
        return action, 502
    except ValueError:
        record_check("gmail", "Gmail mailbox mismatch")
        return "Select info@tommytintsltd.co.uk and retry. A mailbox alias is not a separate Gmail account.", 403
    except Exception:
        record_check("gmail", "Gmail profile verification failed")
        return "Unable to verify the business Gmail profile. Retry Connect Gmail.", 502
    try:
        persisted = save_connection("gmail", session.get("google_sub"), token)
    except Exception:
        persisted = False
    if not persisted:
        record_check("gmail", "Gmail secure offline credential storage failed")
        return "Gmail profile verified, but secure offline credentials could not be saved. Check persistent HQ storage and reconnect for offline consent.", 503
    if not record_check("gmail_profile"):
        return "Gmail credentials saved, but profile verification history could not be recorded. Check Integration Health storage.", 503
    key = secrets.token_urlsafe(32)
    with _gmail_lock:
        for old, entry in list(_gmail_tokens.items()):
            if entry[1] <= time.time() or old == session.get("gmail_token_key"):
                del _gmail_tokens[old]
        _gmail_tokens[key] = (access_token, expiry)
    session["gmail_token_key"] = key
    return redirect(url_for("hq_gmail_queue"))


@app.route("/hq/gmail-enquiries", methods=["GET", "POST"])
@required
def hq_gmail_queue():
    token = _gmail_token()
    result = None
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            return "Invalid security token", 403
        if request.form.get("reviewed"):
            try:
                reviewed = gmail.review(request.form["reviewed"])
            except (OSError, sqlite3.Error):
                return "Unable to update the enquiry queue", 503
            return redirect(url_for("hq_gmail_queue"), 303) if reviewed else ("Enquiry not found", 404)
        if not token:
            return "Connect Gmail before importing", 401
        page = request.form.get("page_token", "")
        if len(page) > 512:
            return "Invalid page token", 400
        try:
            result = gmail.import_page(token, page or None)
        except PersistedSyncError:
            record_check("gmail_sync", "Gmail enquiry storage commit failed")
            return "Gmail enquiry storage unavailable. Check Integration Health.", 503
        except Exception as exc:
            diagnostic, action = gmail.import_error_diagnostic(exc)
            record_check("gmail", diagnostic)
            return action, 409 if isinstance(exc, gmail.EnquiryLabelError) else 502
        # Provider queue commit and readiness evidence are separate writes.
        # A successful import cannot be presented as a verified sync if its
        # observation transaction failed; do not replay the provider import.
        if not record_committed_sync("gmail"):
            return ("Gmail enquiries were stored, but integration read/sync evidence "
                    "could not be recorded. Check Integration Health storage; "
                    "do not treat this import as a verified sync."), 503
    try:
        count, rows = gmail.queue()
    except (OSError, sqlite3.Error):
        return "Enquiry storage unavailable", 503
    return render_template_string("""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gmail enquiries | Tommy Tints HQ</title><style>
body{max-width:800px;margin:auto;padding:24px;background:#0b0f16;color:#eef2f8;font:16px system-ui}
a{color:#ff8088}article{background:#151e2b;padding:16px;margin:12px 0;overflow-wrap:anywhere}
</style></head><body><main><a href="/hq/integrations">Integration health</a><h1>Gmail enquiry review</h1>
<p>Label genuine customer enquiries HQ-Enquiries. Keep legal/debt correspondence separately labelled HQ-Legal or HQ-Debt in Gmail. The import also skips recognisable sensitive notices; review uncertain mail in Gmail, not the sales queue.</p>
{% if connected %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button>Import latest page</button>
{% if result and result.next %}<button name="page_token" value="{{ result.next }}">Import next page</button>{% endif %}
</form>{% else %}<p><a href="/auth/gmail/connect">Connect Gmail read-only</a> · <a href="/hq/gmail-setup">Gmail setup and diagnostics</a></p>{% endif %}
{% if result %}<p>Imported {{ result.added }} new of {{ result.read }} read.</p>{% endif %}
{% if result and result.skipped_sensitive %}<p>{{ result.skipped_sensitive }} recognisable sensitive messages left in Gmail, not added to sales enquiries.</p>{% endif %}
{% if result and result.skipped_label_mismatch %}<p>{{ result.skipped_label_mismatch }} messages excluded because their live Gmail labels no longer qualified as inbox customer enquiries.</p>{% endif %}
<p>Pending review {{ count if count is not none else "Unavailable" }}.</p>
{% for item in rows %}<article><h2>{{ item.subject }}</h2><p>{{ item.sender }}</p><p>{{ item.snippet }}</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button name="reviewed" value="{{ item.id }}">Mark reviewed</button></form></article>{% endfor %}
</main></body></html>""", connected=bool(token), result=result, count=count,
        rows=rows, csrf=session.get("csrf")), 200, {"Cache-Control": "no-store"}


@app.route("/hq/square-bookings", methods=["GET", "POST"])
@required
def hq_square_snapshot():
    changed = None
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            return "Invalid security token", 403
        if not square.configured():
            return "Square read-only connection is not configured", 503
        try:
            changed = square.refresh_bookings()
        except PersistedSyncError:
            record_check("square_sync", "Square booking snapshot commit failed")
            return "Square booking storage unavailable. Check Integration Health.", 503
        except Exception:
            record_check("square", "Read-only Square booking retrieval failed")
            return "Square booking retrieval failed. Check Integration Health.", 502
        # The provider snapshot may have committed while the separate audit
        # observation failed. Report incomplete readiness rather than success.
        if not record_committed_sync("square", source_location=os.environ.get("HQ_SQUARE_LOCATION_ID")):
            return ("Square bookings were stored, but integration read/sync evidence "
                    "could not be recorded. Check Integration Health storage; "
                    "do not treat this refresh as a verified sync."), 503
    try:
        rows = square.upcoming_bookings()
    except (OSError, sqlite3.Error):
        return "Square snapshot storage is unavailable", 503
    return render_template_string("""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Square booking snapshot | Tommy Tints HQ</title><style>
body{max-width:800px;margin:auto;padding:24px;background:#0b0f16;color:#eef2f8;font:16px system-ui}
a{color:#ff8088}li{margin:8px 0}</style></head><body><main>
<a href="/hq/integrations">Integration health</a><h1>Square booking snapshot</h1>
<p>Last observed booking versions. Times are UTC. Use Square to confirm current availability.</p>
{% if configured %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button>Refresh from Square</button></form>{% else %}<p>Square read-only connection requires configuration.</p>{% endif %}
{% if changed is not none %}<p>{{ changed }} new or updated records.</p>{% endif %}
<ul>{% for row in rows %}<li>{{ row.start_at }} · {{ row.status }} · {{ row.id }}</li>
{% else %}<li>No upcoming bookings recorded.</li>{% endfor %}</ul>
</main></body></html>""", configured=square.configured(), rows=rows, changed=changed,
        csrf=session.get("csrf")), 200, {"Cache-Control": "no-store"}


def _local_google_consent(provider):
    """Report locally stored consent without an OAuth refresh or provider call."""
    if stored_grant_present(provider, session.get("google_sub")):
        return True
    # Pre-persistent session-scoped grants remain visible until expiry.
    caches = {"sheets": (_google_tokens, _google_tokens_lock),
              "calendar": (_calendar_tokens, _calendar_tokens_lock),
              "gmail": (_gmail_tokens, _gmail_lock)}
    tokens, lock = caches[provider]
    with lock:
        entry = tokens.get(session.get(provider + "_token_key"))
    return bool(entry and entry[1] > time.time())


def _hq_integrations():
    # GET status must be strictly local: _google_connection/_gmail_token may
    # otherwise refresh expired OAuth grants over the network.
    sheets = _local_google_consent("sheets")
    calendar = _local_google_consent("calendar")
    gmail_consent = _local_google_consent("gmail")
    health = integration_status([
        ("pricing_sheets", "Google Sheets", sheets),
        ("sheets_job_preflight", "Jobs link inspection (read-only)", sheets),
        ("maintenance_sheets", "Maintenance Sheets", sheets),
        ("finance", "Sheets finance", sheets),
        ("calendar", "Google Calendar", calendar),
        ("square", "Square", square.configured()),
        ("square_payments", "Square payments", square_payments.configured()),
        ("gmail", "Gmail", gmail_consent),
        ("monzo", "Monzo", None),
        ("dna", "DNA Payments", None),
    ], source_locations={
        "square": os.environ.get("HQ_SQUARE_LOCATION_ID"),
        "square_payments": os.environ.get("HQ_SQUARE_LOCATION_ID"),
    })
    try:
        count, _ = gmail.queue(limit=0)
    except (OSError, sqlite3.Error):
        count = None
    next(item for item in health["integrations"] if item["name"] == "Gmail")["pending_records"] = count
    if not square.configured():
        next(item for item in health["integrations"] if item["name"] == "Square")["status"] = "Configuration required"
    if not square_payments.configured():
        next(item for item in health["integrations"] if item["name"] == "Square payments")["status"] = "Configuration and PAYMENTS_READ permission required"
    google_configured = bool(
        (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
        or sheets_configured())
    google_oauth_configured = bool(
        os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
    health["readiness"] = build_readiness(health, {
        "sheets": {"configured": google_configured, "consent": sheets if google_oauth_configured else None},
        "sheets_job_preflight": {"configured": google_oauth_configured, "consent": sheets},
        "maintenance_sheets": {"configured": google_configured, "consent": sheets if google_oauth_configured else None},
        "finance": {"configured": google_configured, "consent": sheets if google_oauth_configured else None},
        "calendar": {"configured": google_oauth_configured, "consent": calendar},
        "gmail": {"configured": google_oauth_configured, "consent": gmail_consent},
        "square": {"configured": square.configured(), "consent": None},
        "square_payments": {"configured": square_payments.configured(), "consent": None},
        "monzo": {"configured": False, "consent": None},
        "dna": {"configured": False, "consent": None},
        "maps": {"configured": bool(os.environ.get("HQ_GOOGLE_MAPS_BROWSER_KEY")
                                    and os.environ.get("HQ_GOOGLE_MAPS_API_KEY")
                                    and os.environ.get("HQ_ROUTE_BASE_ADDRESS")), "consent": None},
    })
    # Internal products use a distinct evidence register: no provider reads,
    # accounts, receipts or booking success inferred from code/storage presence.
    health["workflow_readiness"] = build_workflow_readiness(
        persistent_storage_configured=storage_ready())
    return health

@app.get("/hq/integrations/setup")
@required
def hq_integration_setup():
    from hq_integration_setup import setup_inventory
    return render_template("integration_setup.html", rows=setup_inventory(_hq_integrations()["readiness"])), 200, {"Cache-Control":"no-store"}


@app.get("/api/hq/integrations")
@required
def hq_integration_status():
    return jsonify(_hq_integrations()), 200, {"Cache-Control": "no-store"}

@app.get("/hq/integrations")
@required
def hq_integration_dashboard():
    return render_template_string("""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integration health | Tommy Tints HQ</title>
<link rel="icon" type="image/png" href="/favicon.ico?v=20261117d">
<style>body{margin:0;background:#0b0f16;color:#eef2f8;font:16px system-ui}
main{max-width:1080px;margin:auto;padding:24px}a{color:#ff8088}
section{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,285px),1fr));gap:16px}
article{background:#151e2b;border:1px solid #2c394b;border-radius:12px;padding:16px}
h2{font-size:19px;margin-top:0}dl{margin-bottom:0}dt{color:#aab6c8}dd{margin:0 0 8px}
.readiness-scroll{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;width:100%;min-width:880px}
td,th{border-bottom:1px solid #344355;padding:9px;text-align:left;vertical-align:top}
th{color:#ced8e4}td:first-child{font-weight:650}small{color:#adbbc9}
</style>
</head><body><main><p><a href="/">Back to HQ</a></p><h1>Integration health</h1>
<p>Last observed reads only. Times are UTC.</p><p><a href="/hq/integrations/setup">Connection setup and remaining requirements</a></p><p><a href="/hq/gmail-enquiries">Gmail enquiry review</a> · <a href="/hq/square-bookings">Square booking snapshot</a> · <a href="/hq/square-payment-evidence">Square payment evidence</a> · <a href="/hq/transaction-import-preview">Transaction export schema preview</a> · <a href="/hq/sheets/job-link-preflight">Jobs row-link inspection (read-only)</a></p>
{% if not health.history_available %}<p role="alert">Integration history is unavailable.</p>{% endif %}
<section>{% for item in health.integrations %}<article>
<h2>{{ item.name }}</h2><p><strong>{{ item.status }}</strong></p>
<dl><dt>Last successful read</dt><dd>{{ item.last_success or "No recorded read" }}</dd>
<dt>Last error</dt><dd>{{ item.last_error or "No recorded error" }}
{% if item.last_error_at %} at {{ item.last_error_at }}{% endif %}</dd>
<dt>Pending records</dt><dd>{{ item.pending_records if item.pending_records is not none else "Unavailable" }}</dd>
<dt>Action required</dt><dd>{{ "Yes" if item.intervention_required else "No recorded issue" }}</dd></dl>
</article>{% endfor %}</section>
<h2>Connection &amp; reconciliation readiness</h2>
<p>Owner-only evidence. Configuration is not authorisation; historical read success is not current permission; imported payment evidence is not received bank cash. No live provider request is made by this page.</p>
<div class="readiness-scroll"><table><thead><tr>
<th>Provider / workstream</th><th>Implemented</th><th>Configured</th><th>Authorised</th>
<th>Read-tested</th><th>Sync-tested</th><th>Reconciled</th><th>Last observed success (UTC)</th>
</tr></thead><tbody>{% for item in health.readiness %}<tr>
<td>{{ item.name }}</td><td>{{ item.stages.implemented }}</td><td>{{ item.stages.configured }}</td>
<td>{{ item.stages.authorised }}</td><td>{{ item.stages.read_tested }}</td>
<td>{{ item.stages.sync_tested }}</td><td>{{ item.stages.reconciled }}</td>
<td>{{ item.last_success or "No verified read" }}</td></tr>{% endfor %}</tbody></table></div>
<section>{% for item in health.readiness %}<article>
<h2>{{ item.name }}</h2><dl><dt>Exact outstanding blocker</dt><dd>{{ item.blocker }}</dd>
<dt>Next safe owner / engineering action</dt><dd>{{ item.next_safe_action }}</dd>
</dl></article>{% endfor %}</section>
<h2>Internal workflow readiness</h2>
<p>These are code and declared-storage observations only. No live customer transaction, approved payment, private tier entitlement or successful provider synchronisation is inferred. Storage path configured does not mean writable, encrypted, backed up or restored.</p>
<div class="readiness-scroll"><table><thead><tr><th>Workstream</th><th>Implementation</th><th>Storage configuration</th><th>Current live evidence</th><th>Reconciliation</th></tr></thead>
<tbody>{% for item in health.workflow_readiness %}<tr><td>{{ item.name }}</td><td>{{ item.stages.implemented }}</td>
<td>{{ item.stages.configured }}</td><td>{{ item.stages.read_tested }} / {{ item.stages.sync_tested }}</td>
<td>{{ item.stages.reconciled }}</td></tr>{% endfor %}</tbody></table></div>
<section>{% for item in health.workflow_readiness %}<article><h2>{{ item.name }}</h2>
<dl><dt>Current code state</dt><dd>{{ item.code_status }}</dd>
<dt>Exact blocker</dt><dd>{{ item.blocker }}</dd>
<dt>Next safe action</dt><dd>{{ item.next_safe_action }}</dd></dl></article>{% endfor %}</section>
<p><small>Owner-only readiness; not a bank-reconciled ledger or permission to send, book, charge, or modify live records.</small></p>
</main></body></html>""", health=_hq_integrations()), 200, {"Cache-Control": "no-store"}

def _assistant_csrf_valid():
    return secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "not-set"))

@app.route("/api/hq/assistant/tasks", methods=["GET", "POST"])
@required
def assistant_tasks():
    if request.method == "GET":
        return jsonify(tasks=list_tasks())
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    if request.content_length is not None and request.content_length > 6000:
        return jsonify(error="Request too large"), 413
    try:
        task = create_task(request.get_json(silent=True))
        audit("task_created", "Draft task " + task["id"])
        return jsonify(task=task), 201
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

@app.post("/api/hq/assistant/tasks/<task_id>/review")
@required
def assistant_task_review(task_id):
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required"), 400
    try:
        task = review_task(task_id, data.get("decision"))
        audit("task_reviewed", "Task " + task_id + " marked " + task["status"] + "; no action executed")
        return jsonify(task=task)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404

@app.get("/api/hq/assistant/history")
@required
def hq_assistant_history():
    return jsonify(messages=assistant_history())

@app.post("/api/hq/assistant/history/clear")
@required
def hq_assistant_history_clear():
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    clear_history()
    return jsonify(cleared=True)

@app.get("/api/hq/assistant/audit")
@required
def hq_assistant_audit():
    return jsonify(events=audit_events())

@app.post("/api/hq/assistant/speech")
@required
def hq_assistant_speech():
    from flask import Response
    import hq_speech
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if request.content_length and request.content_length > 12000:
        return jsonify(error="Speech request too large"), 413
    data=request.get_json(silent=True)
    text=data.get("text") if isinstance(data,dict) else None
    if not isinstance(text,str) or not text.strip() or len(text)>2000:
        return jsonify(error="Speech text must contain 1–2000 characters"),400
    if not hq_speech.configured():
        return jsonify(error="Human-like voice requires the existing OpenAI API key to be configured. Device voice remains selectable."),503
    if not hq_speech.reserve(session.get("google_sub") or "owner"):
        return jsonify(error="Voice limit reached. Try later or choose a device voice."),429
    try:
        audio=hq_speech.generate(text)
    except Exception:
        return jsonify(error="Neural speech unavailable. Check OpenAI model access and billing, or select a device voice."),502
    return Response(audio,mimetype="audio/mpeg",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

@app.post("/api/hq/assistant/chat")
@required
def hq_assistant_chat():
    """Authenticated assistant: local HQ summary or read-only OpenAI chat."""
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    if request.content_length is not None and request.content_length > 42000:
        return jsonify(error="Request too large"), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required"), 400
    try:
        local_result = local_hq_answer(data.get("message"))
        if local_result is None:
            # Never pass private HQ record lookups to the external AI provider.
            result = openai_chat(data.get("message"), assistant_history(provider_visible_only=True))
        else:
            result = local_result
        record_chat(data.get("message"), result["reply"], provider_visible=local_result is None)
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except AssistantConfigurationError as exc:
        return jsonify(error=str(exc)), 503
    except LocalRecordsUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except AssistantUpstreamError as exc:
        return jsonify(error=str(exc)), 502

@app.post("/api/hq/assistant/plan")
@required
def hq_assistant_plan():
    """Private non-executing assistant planner."""
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    if request.content_length is not None and request.content_length > 6000:
        return jsonify(error="Request too large"), 413
    try:
        return jsonify(plan_request(request.get_json(silent=True)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

@app.post("/api/hq/enquiry/analyse")
@required
def hq_enquiry_analyse():
    """Authenticated HQ-only preview. No storage or external model calls."""
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    if request.content_length is not None and request.content_length > 12000:
        return jsonify(error="Request too large"), 413
    try:
        return jsonify(analyse_enquiry(request.get_json(silent=True)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/public/customer-request/status")
def customer_request_status():
    return jsonify(enabled=os.environ.get("HQ_PUBLIC_REQUESTS_ENABLED", "").lower()=="true" and storage_ready())

@app.post("/api/public/customer-request")
def public_customer_request():
    """Unconfirmed customer enquiry intake; no booking, payment or outbound messaging."""
    if os.environ.get("HQ_PUBLIC_REQUESTS_ENABLED", "").lower() != "true" or not storage_ready():
        return jsonify(error="Customer requests are not yet enabled"), 503
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    if request.content_length is not None and request.content_length > 8000:
        return jsonify(error="Request too large"), 413
    data=request.get_json(silent=True)
    try:
        # Never store raw IP addresses; hash with a server-held secret for throttling.
        ip=request.remote_addr or "unknown"
        fingerprint=hmac.new(app.secret_key.encode() if isinstance(app.secret_key,str) else app.secret_key,ip.encode(),hashlib.sha256).hexdigest()
        request_id=save_customer_request(data,fingerprint)
        return jsonify(reference=request_id,status="received",confirmed_booking=False), 201
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/hq/customer-requests")
@required
def hq_customer_requests():
    """Owner-only customer request inbox."""
    return jsonify(requests=list_customer_requests())

@app.post("/api/hq/customer-requests/<request_id>/status")
@required
def hq_customer_request_status(request_id):
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error="JSON object required"), 400
    try:
        status=update_request_status(request_id,data.get("status"))
        audit("customer_request_reviewed","Request "+request_id+" marked "+status+"; no external action executed")
        return jsonify(id=request_id,status=status,actions_executed=0)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404

@app.get("/api/hq/customer-request-drafts")
@required
def hq_customer_request_drafts():
    return jsonify(drafts=list_review_drafts())

@app.post("/api/hq/customer-requests/<request_id>/draft")
@required
def hq_customer_request_draft(request_id):
    """Create an internal review draft, not a quote sent or a job scheduled."""
    if not _assistant_csrf_valid():
        return jsonify(error="Invalid CSRF token"), 403
    if not request.is_json:
        return jsonify(error="JSON body required"), 415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error="JSON object required"), 400
    try:
        draft=create_review_draft(request_id,data.get("draft_type"))
        audit("customer_request_draft","Internal "+draft["draft_type"]+" draft "+draft["id"]+" linked to request "+request_id+"; no external action executed")
        return jsonify(draft=draft,external_actions=0), 201
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404

@app.get("/api/hq/work-records")
@required
def hq_work_records():
    return jsonify(records=list_work_records())


@app.get("/api/hq/work-records/<record_id>/sheet-sync")
@required
def hq_work_record_sheet_sync(record_id):
    from hq_job_sync import status
    try:
        from hq_core_records import read_work_record
        read_work_record(record_id)
        return jsonify(status(record_id)), 200, {"Cache-Control":"no-store"}
    except ValueError:
        return jsonify(error="Invalid work record"),400
    except LookupError:
        return jsonify(error="Work record not found"),404

@app.get("/api/hq/work-records/<record_id>/history")
@required
def hq_work_record_history(record_id):
    """Owner-only, PII-minimised read-only quote-to-job and pricing audit."""
    try:
        response = jsonify(work_record_history(record_id))
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except (RuntimeError, OSError, sqlite3.Error):
        app.logger.exception("Work record history unavailable")
        return jsonify(error="Persistent storage unavailable"), 503


@app.get("/api/hq/work-records/<record_id>/costing")
@required
def hq_work_record_costing(record_id):
    """Owner-only read-only snapshot of saved costing against recorded HQ job price."""
    owner = str(session.get("google_sub") or "")
    if not owner:
        return jsonify(error="Owner identity missing"), 403
    try:
        return jsonify(costing_snapshot(owner, record_id))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except (RuntimeError, OSError, sqlite3.Error):
        app.logger.exception("Job costing snapshot unavailable")
        return jsonify(error="Persistent storage unavailable"), 503


@app.get("/api/hq/work-records/search")
@required
def hq_search_work_records():
    """Owner-only read-only search; legacy latest-100 endpoint is preserved."""
    try:
        def page_integer(name, default):
            raw = request.args.get(name, str(default))
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                raise ValueError("Invalid " + name)
            return int(raw)
        result = search_work_records(
            query=request.args.get("q", ""),
            kind=request.args.get("kind", ""),
            status=request.args.get("status", ""),
            limit=page_integer("limit", 25),
            offset=page_integer("offset", 0),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

@app.post("/api/hq/customer-requests/<request_id>/work-record")
@required
def hq_create_work_record(request_id):
    """Create a linked internal work record only, with no external side effects."""
    if not _assistant_csrf_valid(): return jsonify(error="Invalid CSRF token"),403
    try:
        record_id=create_internal_work_record(request_id)
        audit("work_record_created","Internal work record "+record_id+" from request "+request_id)
        return jsonify(id=record_id,live_booking=False,external_actions=0),201
    except ValueError as exc: return jsonify(error=str(exc)),400
    except LookupError as exc: return jsonify(error=str(exc)),404

@app.post("/api/hq/test-work-records")
@required
def hq_test_work_record():
    if not _assistant_csrf_valid(): return jsonify(error="Invalid CSRF token"),403
    record_id=create_test_job()
    audit("test_work_record_created","Owner created synthetic test record "+record_id)
    return jsonify(id=record_id,synthetic=True,external_actions=0),201

@app.delete("/api/hq/test-work-records/<record_id>")
@required
def hq_delete_test_work_record(record_id):
    if not _assistant_csrf_valid(): return jsonify(error="Invalid CSRF token"),403
    try:
        delete_test_job(record_id)
        audit("test_work_record_deleted","Owner deleted synthetic test record "+record_id)
        return jsonify(deleted=True,external_actions=0)
    except ValueError as exc: return jsonify(error=str(exc)),400
    except LookupError as exc: return jsonify(error=str(exc)),404

@app.get("/api/hq/work-summary")
@required
def hq_work_summary():
    return jsonify(work_summary())

@app.post("/api/hq/work-records/<record_id>/status")
@required
def hq_work_record_status(record_id):
    if not _assistant_csrf_valid(): return jsonify(error="Invalid CSRF token"),403
    if not request.is_json: return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict): return jsonify(error="JSON object required"),400
    if type(data.get("expected_revision")) is not int:
        return jsonify(error="Expected work record revision is required"),400
    try:
        status=change_work_status(record_id,data.get("status"),data["expected_revision"])
        audit("work_record_status","Internal work record "+record_id+" marked "+status+"; no external action")
        return jsonify(id=record_id,status=status,external_actions=0)
    except StaleWorkRecordError as exc: return jsonify(error=str(exc)),409
    except ValueError as exc: return jsonify(error=str(exc)),400
    except LookupError as exc: return jsonify(error=str(exc)),404

@app.post("/api/hq/work-records/<record_id>/financials")
@required
def hq_work_record_financials(record_id):
    if not _assistant_csrf_valid(): return jsonify(error="Invalid CSRF token"),403
    if not request.is_json: return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict): return jsonify(error="JSON object required"),400
    if type(data.get("expected_revision")) is not int:
        return jsonify(error="Expected work record revision is required"),400
    try:
        result=set_draft_financials(record_id,data.get("price_pence"),data.get("deposit_pence"),data["expected_revision"])
        audit("work_record_pricing","Internal draft pricing updated for "+record_id)
        return jsonify(result)
    except StaleWorkRecordError as exc: return jsonify(error=str(exc)),409
    except ValueError as exc: return jsonify(error=str(exc)),400
    except LookupError as exc: return jsonify(error=str(exc)),404

@app.get("/hq-icon-180.png")
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.get("/favicon.ico")
def hq_apple_icon():
    return app.send_static_file("hq-icon-180.png")

@app.get("/hq-icon-512.png")
def hq_large_icon():
    return app.send_static_file("hq-icon-512.png")

@app.get("/hq-icon-1024.png")
def hq_master_icon():
    return app.send_static_file("hq-icon-1024.png")

@app.get("/book")
def customer_booking_preview():
    """Public, non-transactional booking design preview."""
    return redirect("/static/customer-booking-preview.html", code=302)

@app.get("/health")
def health():
    return jsonify(status="ok",application="tommy-tints-hq",stage="authenticated-preview" if configured() else "authentication-setup-required",live_data=False,sheets_credentials_configured=sheets_configured())
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
