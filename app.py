"""Tommy Tints HQ interactive preview. No customer data is served until auth is configured."""
import os, secrets, time
from threading import Lock
from flask import Flask, jsonify, render_template_string, session, request, redirect, url_for
from quote_engine import calculate_quote
from materials_costing import calculate_materials
from job_material_store import read_log, write_log
from job_profitability import profitability
from job_negotiation import evaluate as evaluate_negotiation
from maintenance_planner import maintenance_data
from maintenance_economics import travel_charge, route_economics
from maintenance_maps import driving_matrix
from maintenance_suggestions import booking_suggestions
from maintenance_calendar import check_booking_window
from maintenance_calendar_google import live_conflicts
from hq_sheets_live import configured as sheets_configured, read_workbook, read_workbook_with_access_token
from hq_auth import auth, oauth, google, required, configured
app = Flask(__name__)
app.secret_key = os.environ.get('HQ_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.environ.get('HQ_DEV_HTTP') != '1', SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID', '')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET', '')
oauth.init_app(app)
app.register_blueprint(auth)
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tommy Tints HQ</title><style>
:root{font-family:system-ui,Arial;color:#eef2f8;background:#0b0f16}*{box-sizing:border-box}body{margin:0}.shell{display:flex;min-height:100vh}aside{width:235px;background:#111824;padding:24px;border-right:1px solid #283344}main{padding:30px;flex:1;min-width:0;max-width:1250px}h1{font-size:32px;margin:0 0 8px}h2{font-size:21px}p,small{color:#aab6c8}.brand{font-size:23px;font-weight:900;margin-bottom:28px}.brand b{color:#f0444d}button,.button{background:#263447;color:white;border:1px solid #43536a;padding:11px 14px;border-radius:9px;cursor:pointer;font-weight:650}button:hover{background:#364c67}button.active,.primary{background:#c82737;border-color:#e03a49}.nav{display:block;width:100%;text-align:left;margin:7px 0}.card{background:#151e2b;border:1px solid #2c394b;padding:20px;border-radius:14px;margin:15px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:13px}.grid .card{margin:0}.muted{color:#9baac0}.value{font-size:27px;font-weight:800}.hidden{display:none}label{display:block;margin:12px 0 5px;color:#c6d0df}input,select{background:#0d1420;color:white;border:1px solid #45536a;padding:11px;border-radius:8px;width:100%;max-width:420px}table{border-collapse:collapse;width:100%;margin-top:12px}td,th{padding:11px;border-bottom:1px solid #344055;text-align:left}a{color:#ff7c84}.notice{border-left:3px solid #eab44c;padding:12px 15px;background:#302718;margin:15px 0;color:#f0d99d}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}#timer{font-size:40px;font-variant-numeric:tabular-nums} @media(max-width:750px){.shell{display:block}aside{width:100%;padding:14px}.brand{margin-bottom:8px}.nav{display:inline-block;width:auto;margin:4px;font-size:12px}main{padding:17px}}
</style></head><body><div class="shell"><aside><div class="brand">TOMMY <b>TINTS</b> HQ</div><button class="nav active" data-page="overview">Overview</button><button class="nav" data-page="calculator">Trade price calculator</button><button class="nav" data-page="timer">Job timer</button><button class="nav" data-page="materials">Materials costing</button><button class="nav" data-page="jobs">Jobs & Google Sheets</button><button class="nav" data-page="maintenance">Maintenance & Routes</button><button class="nav" data-page="integrations">Integrations</button></aside><main><h1 id="heading">Command centre</h1><p>Interactive development preview · real business records are not loaded</p><div class="notice">Preview only: login and Google service credentials are required before displaying private business data. No information entered here is sent to Google Sheets.</div>
<section id="overview" class="page"><div class="grid"><div class="card"><small>Jobs synced</small><div class="value">Not connected</div></div><div class="card"><small>Bank feed</small><div class="value">Not connected</div></div><div class="card"><small>Bookings</small><div class="value">Not connected</div></div></div><div class="card"><h2>Start working</h2><div class="row"><button class="primary" data-go="calculator">Calculate a quote</button><button data-go="timer">Start a job timer</button><button data-go="jobs">Open existing HQ sheet</button></div></div></section>
<section id="calculator" class="page hidden"><div class="card"><h2>Quick Quote · Internal estimator</h2><p>Select a live Cost Library service to prefill labour and materials. Estimates require your approval; prices are not automatically sent to customers.</p><label>Cost Library service</label><select id="service"><option value="">Manual estimate — no service selected</option></select><p id="serviceInfo" class="muted">Load HQ records to select a live service.</p><label>Customer type</label><select id="tier"><option value="retail">Retail — £85/hour</option><option value="trade">Trade — £70/hour</option></select><label>Work description (for your notes only)</label><input id="workDescription" maxlength="300" placeholder="e.g. Hyundai bonnet repair and repaint"><label>Labour hours</label><input id="hours" type="number" min="0" step=".1" value="2.1"><p id="rateDisplay" class="muted">Confirmed retail labour rate: £85/hour</p><label>Materials (£)</label><input id="materials" type="number" min="0" step=".01" value="8.27"><label>Consumables (£)</label><input id="consumables" type="number" min="0" step=".01" value="2.97"><label>Additional overhead allowance (£)</label><input id="overhead" type="number" min="0" step=".01" value="0"><label>Quoted price (£, optional)</label><input id="quoted" type="number" min="0" step=".01" value="150"><p><button class="primary" id="calculate">Calculate</button></p><div id="quoteResult" aria-live="polite"></div></div></section>
<section id="materials" class="page hidden"><div class="card"><h2>Materials & consumables · job costing</h2><p>Calculate actual material cost. Nothing is written to Sheets. Add each clearcoat component separately; basecoat can use the mixing system's actual price.</p><label>Job reference</label><select id="matJob"><option value="">Unassigned</option></select><p id="matJobHelp">Existing jobs load after Google Sheets is connected. If unavailable, enter an exact job reference below.</p><label>Manual job reference (when not listed)</label><input id="matManualJob" placeholder="e.g. J0057"><button id="matUseManual">Use reference</button><label>Selling price (£)</label><input id="matSale" type="number" min="0" step=".01"><label>Active labour hours</label><input id="matLabour" type="number" min="0" step=".1"><label>Actual labour cost (£ per hour, not customer rate)</label><input id="matActualRate" type="number" min="0" step=".01" placeholder="Required for meaningful profit"><label>Allocated overhead for this job (£)</label><input id="matOverhead" type="number" min="0" step=".01" placeholder="Enter job overhead"><label>Other job costs (£)</label><input id="matOther" type="number" min="0" step=".01" placeholder="Transport, subcontractors, etc."><label>Material name</label><input id="matName" value="Clearcoat"><label>Cost method</label><select id="matMethod"><option value="clearcoat_kit">Clearcoat kit (clear + hardener)</option><option value="quantity">Quantity from purchased pack</option><option value="direct">Actual cost from mixing system</option><option value="filler_preset">Filler size estimate</option></select><label>Clearcoat volume in kit (ml)</label><input id="matClear" type="number" min="0" value="5000"><label>Hardener volume in kit (ml)</label><input id="matHardener" type="number" min="0" value="2500"><label>Purchase price (£)</label><input id="matPrice" type="number" min="0" step=".01" value="100"><label>Pack quantity (ml, g, discs, etc.)</label><input id="matPack" type="number" min=".01" step=".01" value="5000"><label>Quantity used (same units)</label><input id="matUsed" type="number" min="0" step=".01" value="300"><label>Actual direct cost (£)</label><input id="matDirect" type="number" min="0" step=".01" value="8.61"><label>Filler repair size</label><select id="matPreset"><option value="small">Small skim (30 g)</option><option value="medium">Medium (90 g)</option><option value="heavy">Heavy (200 g)</option></select><label>Waste allowance (%)</label><input id="matWaste" type="number" min="0" max="100" value="0"><p><button id="addMaterial">Add material to job</button></p><p id="matStatus" role="status"></p><div id="matLines"></div><h2 id="matTotal">Total material cost: £0.00</h2><p id="matProfit"></p><div id="matBreakdown"></div><hr><h3>Price negotiation</h3><p>Set a minimum sustainable margin. The slider never changes the agreed job price automatically.</p><label>Minimum sustainable margin (%)</label><input id="matMinMargin" type="number" min="0" max="99" step="1" placeholder="Set your policy"><label>Proposed customer price (£)</label><input id="matOffer" type="range" min="0" max="2000" step="5" value="0"><p id="matOfferLabel">Proposed price: £0.00</p><p id="matNegotiation" role="status"></p><h3>Quiet-period cashflow check</h3><p id="matQuiet">Cash-only acceptance is not a sustainable pricing recommendation. Costs and capacity must be verified first.</p><hr><h3>Customer-facing job breakdown</h3><p>Only the work description, work stages, job reference and agreed selling price are printed. Internal costs and margins are excluded. Save job to HQ after editing the description or stages, then print.</p><label>Customer-facing job description</label><input id="matDescription" maxlength="200" placeholder="e.g. Hyundai bonnet preparation and repaint"><label>Work completed (one stage per line)</label><textarea id="matStages" rows="5" placeholder="Preparation and masking&#10;Surface repair&#10;Primer and paint&#10;Clearcoat and finishing"></textarea><button id="matPrint">Open printable customer breakdown</button><button id="clearMaterials">Clear list</button> <button id="syncMaterials" class="primary">Save job to HQ</button><p id="syncStatus" role="status">Material lines are local until saved to HQ.</p></div></section>
<section id="timer" class="page hidden"><div class="card"><h2>Job timer</h2><p>Use this to measure actual active working time. Timer data is saved on this device only and is not synced across devices.</p><label>Existing HQ job</label><select id="jobSelect"><option value="">Manual timer — no job selected</option></select><label>Job reference (no customer details)</label><input id="jobRef" maxlength="40" placeholder="e.g. J0082"><label>Estimated active labour (hours)</label><input id="estimatedHours" type="number" min="0.1" max="500" step="0.25" value="4"><p class="muted">Alerts at 75%, 90% and 100% of estimated active time. Save your timer locally on this device; browser alerts require permission and may not fire if the page is closed.</p><div id="timeProgress" role="progressbar" aria-label="Time used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="height:12px;background:#263447;border-radius:8px;overflow:hidden"><div id="timeFill" style="height:100%;width:0%;background:#eab44c"></div></div><p id="timeRemaining" role="status"></p><div id="timer">00:00:00</div><div class="row"><button class="primary" id="start">Start / Resume</button><button id="pause">Pause</button><button id="reset">Reset</button><button id="copyTime">Copy time</button></div><p id="timerMessage" role="status"></p></div></section>
<section id="jobs" class="page hidden"><div class="card"><h2>Existing HQ jobs</h2><p>Google Sheets remains the source of truth. Connect read-only to load current jobs and Cost Library information. Editing remains disabled.</p><p><a href="/auth/sheets/connect">Connect Google Sheets (read-only)</a> · <button id="loadSheets">Load HQ records</button></p><p id="sheetsStatus" role="status"></p><div id="sheetsSummary"></div><a href="https://docs.google.com/spreadsheets/d/1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M/edit#gid=330634230" target="_blank" rel="noopener noreferrer">Open the live Jobs sheet</a></div></section>
<section id="maintenance" class="page hidden"><div class="card"><h2>Maintenance & Route Planner</h2><p>Read-only view of existing Maintenance Clients, Maintenance Visits and Route Rules. Customer data stays within your authenticated HQ session.</p><button id="loadMaintenance" class="primary">Load maintenance customers</button><p id="maintenanceStatus" role="status"></p><div id="maintenanceSummary"></div><div id="maintenanceClusters"></div><hr><h3>Customers due together</h3><p>Suggests customers due within the next 7 days who share a recorded route area. Does not send reminders or create bookings.</p><button id="maintenanceSuggest">Find booking opportunities</button><div id="maintenanceSuggestions" role="status"></div><hr><h3>Live Google Calendar conflict check</h3><p>Checks your primary Google Calendar, read-only. Square bookings and other calendars are not included. Permission expires and may require reconnection.</p><p><a href="/auth/calendar/connect">Connect Google Calendar (read-only)</a></p><label for="liveCalendarDate">Appointment date</label><input id="liveCalendarDate" type="date"><label for="liveCalendarStartTime">Start time</label><input id="liveCalendarStartTime" type="time" value="09:00"><label for="liveCalendarEndTime">End time</label><input id="liveCalendarEndTime" type="time" value="10:00"><p>Times use UK local time (GMT/BST). No technical date format needed.</p><button id="liveCalendarCheck">Check live Google Calendar</button><p id="liveCalendarResult" role="status"></p><hr><h3>Booking conflict preview</h3><p>Paste proposed appointment and existing calendar event times in ISO 8601 format, including UK timezone offset. This checks overlaps only; it does not read your live calendar or create appointments.</p><label>Proposed start</label><input id="calendarStart" placeholder="2026-11-18T10:00:00+00:00"><label>Proposed end</label><input id="calendarEnd" placeholder="2026-11-18T11:00:00+00:00"><label>Existing busy events (one per line: start,end)</label><textarea id="calendarEvents" rows="3" placeholder="2026-11-18T10:30:00+00:00,2026-11-18T12:00:00+00:00"></textarea><button id="calendarCheck">Check overlapping appointments</button><p id="calendarResult" role="status"></p><hr><h3>Driving-time grouping (Google Routes)</h3><p>Optional integration: requires a configured server-side Google Maps Routes API key and verified full addresses. No automatic booking changes.</p><label>Full addresses (one per line, include street, postcode and UK)</label><textarea id="maintenanceAddresses" rows="5" placeholder="Unit 5 &amp; 6 Portland Lodge, RM14 3TJ, UK&#10;Customer full street address, SS11 7LL, UK"></textarea><button id="maintenanceMapCalculate">Compare driving times</button><div id="maintenanceMapResult" role="status"></div><hr><h3>Travel charge calculator</h3><p>Uses the existing 10 free one-way miles and £0.56 per mile setting. Enter verified one-way miles; no postcode-only distance guesses.</p><label>Verified one-way miles from HQ</label><input id="maintenanceMiles" type="number" min="0" step=".1" placeholder="Enter map distance"><button id="maintenanceTravelCalculate">Calculate travel surcharge</button><p id="maintenanceTravelResult" role="status"></p><hr><h3>Route profitability</h3><p>Enter total actual route distance and driving time, including journeys between customers and the return to HQ. Estimate costs across all stops.</p><label>Verified total route miles</label><input id="routeMiles" type="number" min="0" step=".1"><label>Total driving minutes</label><input id="routeMinutes" type="number" min="0" step="1"><label>Actual labour cost per hour (£)</label><input id="routeLabour" type="number" min="0" step=".01"><label>Allocated overhead per hour (£)</label><input id="routeOverhead" type="number" min="0" step=".01"><label>Stops (one per line: price, cleaning minutes, materials cost)</label><textarea id="routeStops" rows="5" placeholder="80,60,5.50&#10;80,60,5.50"></textarea><button id="routeCalculate">Calculate route contribution</button><p id="routeResult" role="status"></p><p>Suggested groups are based on your existing route areas, not verified driving times. No bookings are created or changed.</p><p><a href="https://docs.google.com/spreadsheets/d/1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M/edit" target="_blank" rel="noopener noreferrer">Open maintenance source workbook</a></p></div></section><section id="integrations" class="page hidden"><div class="card"><h2>Integration readiness</h2><table><tr><th>Integration</th><th>Status</th></tr><tr><td>Google Sheets adapter</td><td>Google Sheets read-only OAuth consent available; server-side token persistence pending</td></tr><tr><td>Square / Calendar</td><td>Not connected to this app</td></tr><tr><td>Monzo / DNA Payments</td><td>Not connected to this app</td></tr><tr><td>Login / roles</td><td>Required before private data sync</td></tr></table></div></section>
</main></div><script>
"use strict";
const $=id=>document.getElementById(id);
const TIMER_STORAGE_KEY="tommy-tints-hq-timer-v2";
let hqData=null;
const titles={overview:"Command centre",calculator:"Trade price calculator",materials:"Materials costing",timer:"Job timer",jobs:"Jobs",maintenance:"Maintenance & Routes",integrations:"Integrations"};
function navigate(page){if(!titles[page])return;document.querySelectorAll(".page").forEach(el=>el.classList.toggle("hidden",el.id!==page));document.querySelectorAll("[data-page]").forEach(el=>el.classList.toggle("active",el.dataset.page===page));$("heading").textContent=titles[page];}
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.page)));
document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.go)));
function ukCalendarTime(date,time){
 if(!/^\\d{4}-\\d{2}-\\d{2}$/.test(date)||!/^\\d{2}:\\d{2}$/.test(time))throw Error("Select a date and time.");
 const [year,month,day]=date.split("-").map(Number);
 const [hour,minute]=time.split(":").map(Number);
 if(hour>23||minute>59||new Date(Date.UTC(year,month-1,day)).toISOString().slice(0,10)!==date)throw Error("Invalid date or time.");
 // UK DST: starts last Sunday of March at 01:00 UTC, ends last Sunday of October at 01:00 UTC.
 const lastSunday=(m)=>{const d=new Date(Date.UTC(year,m,0));return d.getUTCDate()-d.getUTCDay();};
 const dstStart=Date.UTC(year,2,lastSunday(3),1);
 const dstEnd=Date.UTC(year,9,lastSunday(10),1);
 const local=Date.UTC(year,month-1,day,hour,minute);
 // For the repeated 01:00 hour in October, choose GMT; for the skipped March hour, reject.
 const summer=local>=dstStart+3600000&&local<dstEnd+3600000;
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
function matKey(){return "hq-materials-job:"+currentMaterialJob;}
function saveMat(){try{localStorage.setItem(matKey(),JSON.stringify({lines:materialLines,sale:$("matSale").value,labour:$("matLabour").value,actual_labour_rate:$("matActualRate").value,overhead:$("matOverhead").value,other_costs:$("matOther").value,minimum_margin_percent:$("matMinMargin").value,description:$("matDescription").value,stages:$("matStages").value}));}catch(e){}}
function loadMat(){try{const d=JSON.parse(localStorage.getItem(matKey())||"{}");materialLines=Array.isArray(d.lines)?d.lines:[];$("matSale").value=d.sale||"";$("matLabour").value=d.labour||"";$("matActualRate").value=d.actual_labour_rate||"";$("matOverhead").value=d.overhead||"";$("matOther").value=d.other_costs||"";$("matMinMargin").value=d.minimum_margin_percent||"";$("matDescription").value=d.description||"";$("matStages").value=d.stages||"";}catch(e){materialLines=[];}showMaterials();}
$("matUseManual").addEventListener("click",async()=>{
 const key=$("matManualJob").value.trim().toUpperCase();
 if(!/^[A-Z0-9][A-Z0-9 _-]{0,79}$/.test(key)){$("syncStatus").textContent="Enter a valid job reference (letters, numbers, spaces, hyphens or underscores).";return;}
 if(!Array.from($("matJob").options).some(o=>o.value===key))$("matJob").add(new Option(key+" (manual — not verified against Sheets)",key));
 $("matJob").value=key;currentMaterialJob=key;loadMat();await loadServerMat();
});
$("matJob").addEventListener("change",async()=>{currentMaterialJob=$("matJob").value;loadMat();await loadServerMat();});
async function loadServerMat(){
 if(!currentMaterialJob)return;
 try{const res=await fetch("/api/job-materials/"+encodeURIComponent(currentMaterialJob));
 const d=await res.json();if(!res.ok)throw Error(d.error||"Unable to load saved job");
 if(d.data){materialLines=d.data.lines||[];$("matSale").value=d.data.sale||"";$("matLabour").value=d.data.labour||"";$("matActualRate").value=d.data.actual_labour_rate||"";$("matOverhead").value=d.data.overhead||"";$("matOther").value=d.data.other_costs||"";$("matMinMargin").value=d.data.minimum_margin_percent||"";$("matDescription").value=d.data.description||"";$("matStages").value=d.data.stages||"";showMaterials();$("syncStatus").textContent="Loaded saved HQ job record ("+d.updated_at+").";}
 else $("syncStatus").textContent="No saved HQ record for this job. Local values are shown.";
 }catch(e){$("syncStatus").textContent=e.message+"; local values retained.";}
}
$("syncMaterials").addEventListener("click",async()=>{
 if(!currentMaterialJob){$("syncStatus").textContent="Select an existing job before saving to HQ.";return;}
 try{const csrf=document.querySelector('input[name="csrf"]').value;
 const res=await fetch("/api/job-materials/"+encodeURIComponent(currentMaterialJob),{method:"PUT",headers:{"Content-Type":"application/json","X-HQ-CSRF":csrf},body:JSON.stringify({lines:materialLines,sale:$("matSale").value,labour:$("matLabour").value,actual_labour_rate:$("matActualRate").value,overhead:$("matOverhead").value,other_costs:$("matOther").value})});
 const d=await res.json();if(!res.ok)throw Error(d.error||"Save failed");
 $("syncStatus").textContent="Saved securely to HQ at "+d.updated_at;
 }catch(e){$("syncStatus").textContent="Not saved: "+e.message;}
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
@app.get("/")
@required
def home():
    return render_template_string(PAGE.replace("</main>", '<form action="/logout" method="post"><input type="hidden" name="csrf" value="' + session["csrf"] + '"><button type="submit">Sign out</button></form></main>'))
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
            key = session.get("sheets_token_key")
            with _google_tokens_lock:
                entry = _google_tokens.get(key) if key else None
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

@app.get("/auth/sheets/connect")
@required
def sheets_connect():
    return google.authorize_redirect(
        url_for("sheets_callback", _external=True, _scheme="https"),
        scope=SHEETS_SCOPE, prompt="consent")

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
        scope=CALENDAR_SCOPE,prompt="consent")

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
    key=session.get("calendar_token_key")
    with _calendar_tokens_lock:
        entry=_calendar_tokens.get(key) if key else None
    if not entry or entry[1]<=time.time():
        return jsonify(error="Connect Google Calendar read-only first",connect_url="/auth/calendar/connect"),401
    try:return jsonify(live_conflicts(entry[0],data.get("start"),data.get("end")))
    except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400
    except RuntimeError as exc:return jsonify(error=str(exc)),409
    except Exception:
        app.logger.exception("Live Calendar check failed")
        return jsonify(error="Unable to verify Google Calendar availability"),502

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
        for line in data["lines"]:
            if not isinstance(line,dict) or not isinstance(line.get("name"),str) or len(line["name"])>100:
                return jsonify(error="Invalid material line"),400
            try:
                value=float(line["cost"])
                if not (0<=value<1000000): raise ValueError()
            except (ValueError,TypeError,KeyError):
                return jsonify(error="Invalid material cost"),400
        return jsonify(write_log(owner,job_key,data))
    except (RuntimeError,OSError):
        app.logger.exception("Persistent materials storage unavailable")
        return jsonify(error="Persistent storage unavailable"),503
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
    except (RuntimeError,OSError): return "Job storage unavailable",503
    if not record: return "Save this job in HQ before printing.",404
    description=str(record.get("description") or "Vehicle work completed")[:200]
    stages=[str(x).strip()[:200] for x in str(record.get("stages") or "").splitlines() if str(x).strip()][:30]
    if not stages: return "Add customer-facing work stages and save the job before printing.",400
    price=record.get("sale")
    try:
        from decimal import Decimal
        price_text="£"+format(Decimal(str(price)),",.2f") if price is not None and str(price).strip()!="" and Decimal(str(price)).is_finite() and Decimal(str(price))>=0 else "Not recorded"
    except Exception: price_text="Not recorded"
    return render_template_string("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job breakdown</title><style>body{font:16px Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}h1{border-bottom:3px solid #b71c1c;padding-bottom:12px}li{margin:12px 0}.small{font-size:13px;color:#555}@media print{button{display:none}body{margin:20px auto}}</style></head><body><h1>Tommy Tints — Job Work Summary</h1><p><strong>Job reference:</strong> {{ job }}</p><p><strong>Work:</strong> {{ description }}</p><h2>Work completed</h2><ol>{% for stage in stages %}<li>{{ stage }}</li>{% endfor %}</ol><p><strong>Agreed job price:</strong> {{ price }}</p><p class="small">Summary of work undertaken; not an itemised supplier-cost disclosure, VAT invoice or proof of payment.</p><button onclick="window.print()">Print / Save PDF</button></body></html>""",job=job_key,description=description,stages=stages,price=price_text)

@app.post("/api/hq/maintenance/check-window")
@required
def hq_maintenance_check_window():
    if not request.is_json:return jsonify(error="JSON body required"),415
    try:return jsonify(check_booking_window(request.get_json(silent=True)))
    except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400

@app.get("/api/hq/maintenance/suggestions")
@required
def hq_maintenance_suggestions():
    key=session.get("sheets_token_key")
    with _google_tokens_lock:
        entry=_google_tokens.get(key) if key else None
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
    if not request.is_json:return jsonify(error="JSON body required"),415
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error="Invalid request"),400
    try:return jsonify(driving_matrix(data.get("addresses")))
    except ValueError as exc:return jsonify(error=str(exc)),400
    except RuntimeError as exc:return jsonify(error=str(exc)),503
    except Exception:
        app.logger.exception("Google Routes matrix failed")
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
    key=session.get("sheets_token_key")
    with _google_tokens_lock:
        entry=_google_tokens.get(key) if key else None
    if not entry or entry[1]<=time.time():
        return jsonify(error="Google Sheets consent required or expired",connect_url="/auth/sheets/connect"),401
    try: return jsonify(maintenance_data(entry[0]))
    except Exception:
        app.logger.exception("Read-only maintenance Sheets sync failed")
        return jsonify(error="Unable to read existing maintenance sheets"),502

@app.get("/api/hq/pricing")
@required
def hq_pricing():
    key = session.get("sheets_token_key")
    with _google_tokens_lock:
        entry = _google_tokens.get(key) if key else None
    if not entry or entry[1] <= time.time():
        return jsonify(error="Google Sheets consent required or expired",connect_url="/auth/sheets/connect",connected=False), 401
    try:
        return jsonify(read_workbook_with_access_token(entry[0]))
    except Exception:
        app.logger.exception("Read-only Google Sheets sync failed")
        return jsonify(error="Unable to read HQ spreadsheet. Reconnect Google Sheets and check API permissions."), 502

@app.get("/health")
def health():
    return jsonify(status="ok",application="tommy-tints-hq",stage="authenticated-preview" if configured() else "authentication-setup-required",live_data=False,sheets_credentials_configured=sheets_configured())
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
