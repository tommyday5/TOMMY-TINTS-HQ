"""Tommy Tints HQ interactive preview. No customer data is served until auth is configured."""
import os, secrets
from flask import Flask, jsonify, render_template_string, session, request
from quote_engine import calculate_quote
from hq_sheets_live import configured as sheets_configured, read_workbook
from hq_auth import auth, oauth, required, configured
app = Flask(__name__)
app.secret_key = os.environ.get('HQ_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.environ.get('HQ_DEV_HTTP') != '1', SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID', '')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET', '')
oauth.init_app(app)
app.register_blueprint(auth)
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tommy Tints HQ</title><style>
:root{font-family:system-ui,Arial;color:#eef2f8;background:#0b0f16}*{box-sizing:border-box}body{margin:0}.shell{display:flex;min-height:100vh}aside{width:235px;background:#111824;padding:24px;border-right:1px solid #283344}main{padding:30px;flex:1;min-width:0;max-width:1250px}h1{font-size:32px;margin:0 0 8px}h2{font-size:21px}p,small{color:#aab6c8}.brand{font-size:23px;font-weight:900;margin-bottom:28px}.brand b{color:#f0444d}button,.button{background:#263447;color:white;border:1px solid #43536a;padding:11px 14px;border-radius:9px;cursor:pointer;font-weight:650}button:hover{background:#364c67}button.active,.primary{background:#c82737;border-color:#e03a49}.nav{display:block;width:100%;text-align:left;margin:7px 0}.card{background:#151e2b;border:1px solid #2c394b;padding:20px;border-radius:14px;margin:15px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:13px}.grid .card{margin:0}.muted{color:#9baac0}.value{font-size:27px;font-weight:800}.hidden{display:none}label{display:block;margin:12px 0 5px;color:#c6d0df}input,select{background:#0d1420;color:white;border:1px solid #45536a;padding:11px;border-radius:8px;width:100%;max-width:420px}table{border-collapse:collapse;width:100%;margin-top:12px}td,th{padding:11px;border-bottom:1px solid #344055;text-align:left}a{color:#ff7c84}.notice{border-left:3px solid #eab44c;padding:12px 15px;background:#302718;margin:15px 0;color:#f0d99d}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}#timer{font-size:40px;font-variant-numeric:tabular-nums} @media(max-width:750px){.shell{display:block}aside{width:100%;padding:14px}.brand{margin-bottom:8px}.nav{display:inline-block;width:auto;margin:4px;font-size:12px}main{padding:17px}}
</style></head><body><div class="shell"><aside><div class="brand">TOMMY <b>TINTS</b> HQ</div><button class="nav active" data-page="overview">Overview</button><button class="nav" data-page="calculator">Trade price calculator</button><button class="nav" data-page="timer">Job timer</button><button class="nav" data-page="jobs">Jobs & Google Sheets</button><button class="nav" data-page="integrations">Integrations</button></aside><main><h1 id="heading">Command centre</h1><p>Interactive development preview · real business records are not loaded</p><div class="notice">Preview only: login and Google service credentials are required before displaying private business data. No information entered here is sent to Google Sheets.</div>
<section id="overview" class="page"><div class="grid"><div class="card"><small>Jobs synced</small><div class="value">Not connected</div></div><div class="card"><small>Bank feed</small><div class="value">Not connected</div></div><div class="card"><small>Bookings</small><div class="value">Not connected</div></div></div><div class="card"><h2>Start working</h2><div class="row"><button class="primary" data-go="calculator">Calculate a quote</button><button data-go="timer">Start a job timer</button><button data-go="jobs">Open existing HQ sheet</button></div></div></section>
<section id="calculator" class="page hidden"><div class="card"><h2>Quick Quote · Internal estimator</h2><p>Uses confirmed Tommy Tints labour rates. Estimates require your approval before sending to customers. The full Cost Library and AI assistant are not connected yet.</p><label>Customer type</label><select id="tier"><option value="retail">Retail — £85/hour</option><option value="trade">Trade — £70/hour</option></select><label>Work description (for your notes only)</label><input id="workDescription" maxlength="300" placeholder="e.g. Hyundai bonnet repair and repaint"><label>Labour hours</label><input id="hours" type="number" min="0" step=".1" value="2.1"><p id="rateDisplay" class="muted">Confirmed retail labour rate: £85/hour</p><label>Materials (£)</label><input id="materials" type="number" min="0" step=".01" value="8.27"><label>Consumables (£)</label><input id="consumables" type="number" min="0" step=".01" value="2.97"><label>Additional overhead allowance (£)</label><input id="overhead" type="number" min="0" step=".01" value="0"><label>Quoted price (£, optional)</label><input id="quoted" type="number" min="0" step=".01" value="150"><p><button class="primary" id="calculate">Calculate</button></p><div id="quoteResult" aria-live="polite"></div></div></section>
<section id="timer" class="page hidden"><div class="card"><h2>Job timer</h2><p>Use this to measure actual active working time. Timer data is saved on this device only and is not synced across devices.</p><label>Job reference (no customer details)</label><input id="jobRef" maxlength="40" placeholder="e.g. J0082"><label>Estimated active labour (hours)</label><input id="estimatedHours" type="number" min="0.1" max="500" step="0.25" value="4"><p class="muted">Alerts at 75%, 90% and 100% of estimated active time. Save your timer locally on this device; browser alerts require permission and may not fire if the page is closed.</p><div id="timeProgress" role="progressbar" aria-label="Time used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" style="height:12px;background:#263447;border-radius:8px;overflow:hidden"><div id="timeFill" style="height:100%;width:0%;background:#eab44c"></div></div><p id="timeRemaining" role="status"></p><div id="timer">00:00:00</div><div class="row"><button class="primary" id="start">Start / Resume</button><button id="pause">Pause</button><button id="reset">Reset</button><button id="copyTime">Copy time</button></div><p id="timerMessage" role="status"></p></div></section>
<section id="jobs" class="page hidden"><div class="card"><h2>Existing HQ jobs</h2><p>Google Sheets remains the source of truth. Job editing and synchronisation are disabled until secure authentication is configured.</p><a href="https://docs.google.com/spreadsheets/d/1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M/edit#gid=330634230" target="_blank" rel="noopener noreferrer">Open the live Jobs sheet</a></div></section>
<section id="integrations" class="page hidden"><div class="card"><h2>Integration readiness</h2><table><tr><th>Integration</th><th>Status</th></tr><tr><td>Google Sheets adapter</td><td>Read-only connector prepared; service account permission required</td></tr><tr><td>Square / Calendar</td><td>Not connected to this app</td></tr><tr><td>Monzo / DNA Payments</td><td>Not connected to this app</td></tr><tr><td>Login / roles</td><td>Required before private data sync</td></tr></table></div></section>
</main></div><script>
"use strict";
const $=id=>document.getElementById(id);
const TIMER_STORAGE_KEY="tommy-tints-hq-timer-v2";
const titles={overview:"Command centre",calculator:"Trade price calculator",timer:"Job timer",jobs:"Jobs",integrations:"Integrations"};
function navigate(page){if(!titles[page])return;document.querySelectorAll(".page").forEach(el=>el.classList.toggle("hidden",el.id!==page));document.querySelectorAll("[data-page]").forEach(el=>el.classList.toggle("active",el.dataset.page===page));$("heading").textContent=titles[page];}
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.page)));
document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.go)));
function num(id){const raw=$(id).value.trim();if(!raw)return 0;const n=Number(raw);if(!Number.isFinite(n)||n<0)throw Error("Enter valid non-negative numbers.");return n}
function money(n){return new Intl.NumberFormat("en-GB",{style:"currency",currency:"GBP"}).format(n)}
$("tier").addEventListener("change",()=>{$("rateDisplay").textContent="Confirmed "+$("tier").value+" labour rate: £"+($("tier").value==="trade"?"70":"85")+"/hour";});
$("calculate").addEventListener("click",async()=>{
 try{
  const payload={tier:$("tier").value,hours:num("hours"),materials:num("materials"),consumables:num("consumables"),overhead:num("overhead"),quoted_price:num("quoted")};
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
$("calculate").click();render();
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
        return jsonify(calculate_quote(data))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/hq/pricing")
@required
def hq_pricing():
    if not sheets_configured():
        return jsonify(error="Google Sheets service account not configured",connected=False), 503
    try:
        return jsonify(read_workbook())
    except Exception:
        app.logger.exception("Read-only Google Sheets sync failed")
        return jsonify(error="Unable to read HQ spreadsheet. Check service-account access and Railway configuration."), 502

@app.get("/health")
def health():
    return jsonify(status="ok",application="tommy-tints-hq",stage="authenticated-preview" if configured() else "authentication-setup-required",live_data=False,sheets_credentials_configured=sheets_configured())
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
