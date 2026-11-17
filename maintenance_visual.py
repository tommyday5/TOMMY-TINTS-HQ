"""Owner route planner data and verified Google Routes calculations."""
from datetime import datetime, timedelta
from decimal import Decimal
import os
import re
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, abort, jsonify, redirect, render_template, request, session, url_for
import requests
import secrets

from hq_auth import required
from customer_edits import edit_page, form_values, maintenance_edits, save_maintenance_edit
from maintenance_economics import decimal, pounds
from maintenance_planner import maintenance_data
from maintenance_suggestions import parse_due

visual_routes = Blueprint("visual_routes", __name__)


def route_customers(data, today=None, edits=None):
    today = today or datetime.now(ZoneInfo("Europe/London")).date()
    edits = edits or {}
    visits = {}
    for row in data["visits"]:
        day = parse_due(row.get("Visit Date"))
        state = row.get("Visit Status", "").casefold()
        if day and state in {"booked", "confirmed", "scheduled", "completed"}:
            key = (row.get("Client ID", "").casefold(), day)
            if key not in visits or state == "completed":
                visits[key] = "completed" if state == "completed" else "scheduled"
    customers = {}
    for row in data["clients"]:
        ref = row["Client ID"]
        due = parse_due(row.get("Next Due"))
        state = visits.get((ref.casefold(), due))
        if state is None:
            state = ("date missing" if not due else "overdue" if due < today
                     else "due soon" if due <= today + timedelta(days=7) else "upcoming")
        postcode = row.get("Postcode", "").strip()
        address = next((row.get(field, "").strip() for field in
                        ("Full Address", "Address", "Service Address", "Address Line 1") if row.get(field)), "")
        edit = edits.get(ref)
        if edit is not None:
            address, postcode = edit["address"], edit["postcode"]
        if not address or not postcode or re.sub(r"\s+", "", address).casefold() == postcode.replace(" ", "").casefold():
            address = ""
        elif postcode.replace(" ", "").casefold() not in re.sub(r"\s+", "", address).casefold():
            address += ", " + postcode
        raw_price = row.get("Selected Visit £") or row.get("Normal Visit £")
        try:
            price = pounds(decimal(str(raw_price).replace("£", "").replace(",", ""), "Visit price"))
        except ValueError:
            price = None
        customers[ref] = {"id": ref, "name": (edit["name"] if edit is not None else row.get("Customer")) or ref, "address": address,
                          "frequency": row.get("Frequency"), "due": due.isoformat() if due else None,
                          "status": state, "price": price}
    return customers


def calculate_route(customers, ids, optimise, base, key):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or not all(isinstance(i, str) for i in ids):
        raise ValueError("Select 1 to 10 stops")
    if len(set(ids)) != len(ids) or any(i not in customers or not customers[i]["address"] for i in ids):
        raise ValueError("Each stop must have its own recorded full address and postcode")
    if type(optimise) is not bool:
        raise ValueError("Invalid route mode")
    response = requests.post("https://routes.googleapis.com/directions/v2:computeRoutes",
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask":
                 "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,routes.optimizedIntermediateWaypointIndex"},
        json={"origin": {"address": base}, "destination": {"address": base},
              "intermediates": [{"address": customers[i]["address"]} for i in ids],
              "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE",
              "optimizeWaypointOrder": optimise}, timeout=(5, 20))
    response.raise_for_status()
    route, *_ = response.json()["routes"]
    order = route["optimizedIntermediateWaypointIndex"] if optimise else list(range(len(ids)))
    meters = route["distanceMeters"]
    duration = route["duration"]
    polyline = route["polyline"]["encodedPolyline"]
    if (not isinstance(order, list) or any(type(i) is not int for i in order)
            or sorted(order) != list(range(len(ids))) or type(meters) is not int or meters <= 0
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", duration) or not polyline):
        raise RuntimeError("Incomplete routing response")
    seconds = Decimal(duration[:-1])
    if seconds <= 0:
        raise RuntimeError("Incomplete routing duration")
    miles = Decimal(meters) / Decimal("1609.344")
    rate = decimal(os.environ.get("HQ_ROUTE_MILEAGE_RATE", "0.56"), "Mileage rate")
    mileage = miles * rate
    prices = [customers[i]["price"] for i in ids]
    revenue = sum(map(Decimal, prices)) if all(p is not None for p in prices) else None
    return {"stop_ids": [ids[i] for i in order], "polyline": polyline,
            "miles": str(round(miles, 1)), "minutes": str(round(seconds / 60, 1)),
            "mileage": pounds(mileage), "rate": str(rate),
            "revenue": pounds(revenue) if revenue is not None else None,
            "after_mileage": pounds(revenue - mileage) if revenue is not None else None}


def _token():
    entry = current_app.extensions["hq_sheets_connection"]()
    return entry[0] if entry and entry[1] > datetime.now().timestamp() else None


@visual_routes.get("/hq/maintenance/routes")
@required
def planner():
    return render_template("route_planner.html", csrf=session.get("csrf"),
                           browser_key=os.environ.get("HQ_GOOGLE_MAPS_BROWSER_KEY", ""),
                           routes_enabled=bool(os.environ.get("HQ_GOOGLE_MAPS_API_KEY")
                                               and os.environ.get("HQ_ROUTE_BASE_ADDRESS")))


@visual_routes.route("/api/hq/maintenance/visual-route", methods=["GET", "POST"])
@required
def data():
    if request.method == "POST":
        if not secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "not-set")):
            return jsonify(error="Invalid security token"), 403
        if not request.is_json or request.content_length is None or request.content_length > 4096:
            return jsonify(error="Invalid route request"), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Invalid route request"), 400
        base = os.environ.get("HQ_ROUTE_BASE_ADDRESS", "")
        key = os.environ.get("HQ_GOOGLE_MAPS_API_KEY", "")
        if not base or not key:
            return jsonify(error="Routing requires a Maps server key and the workshop base address"), 503
    token = _token()
    if not token:
        return jsonify(error="Connect Google Sheets to load maintenance customers"), 401
    try:
        customers = route_customers(maintenance_data(token), edits=maintenance_edits())
    except Exception:
        return jsonify(error="Unable to load the maintenance sheet"), 502
    if request.method == "GET":
        return jsonify(customers=list(customers.values()))
    try:
        return jsonify(calculate_route(customers, payload.get("ids"), payload.get("optimise"), base, key))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="Google could not verify the route. Check addresses and Routes API access."), 502


@visual_routes.route("/hq/maintenance/customer/edit", methods=["GET", "POST"])
@required
def edit_maintenance():
    ref = request.args.get("id", "")
    if not 1 <= len(ref) <= 120:
        abort(404)
    token = _token()
    if not token:
        return redirect("/auth/sheets/connect")
    try:
        source = maintenance_data(token)
        rows = [row for row in source["clients"] if row["Client ID"] == ref]
    except Exception:
        return "Unable to load maintenance customer", 502
    if len(rows) != 1:
        abort(404)
    back = url_for(".planner")
    if request.method == "POST":
        try:
            values = form_values()
        except ValueError as exc:
            return edit_page(request.form, back, str(exc), maintenance=True)
        save_maintenance_edit(ref, values)
        return redirect(url_for(".edit_maintenance", id=ref, saved=1), 303)
    row = rows[0]
    customer = maintenance_edits().get(ref)
    if customer is None:
        customer = {
            "name": row.get("Customer") or ref, "email": row.get("Email") or "",
            "phone": row.get("Phone") or row.get("Mobile") or "",
            "address": next((row[field] for field in ("Full Address", "Address", "Service Address",
                                                       "Address Line 1") if row.get(field)), ""),
            "postcode": row.get("Postcode") or "",
        }
    return edit_page(customer, back, maintenance=True)
