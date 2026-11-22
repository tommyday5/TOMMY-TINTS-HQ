"""HQ-managed retail catalogue and append-only maintenance entitlement ledger."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import os
import re
from pathlib import Path
import secrets
import sqlite3
import uuid

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session

from customer_request_store import storage_ready
from hq_auth import required
from hq_customer_portal import active_customer

commerce = Blueprint("commerce", __name__)


@contextmanager
def database():
    if not storage_ready():
        raise RuntimeError("Persistent commerce storage is unavailable")
    path = Path(os.environ["HQ_DATA_DIR"]) / "commerce.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, variant TEXT NOT NULL,
            description TEXT NOT NULL, retail INTEGER NOT NULL, trade INTEGER,
            cost INTEGER NOT NULL, stock INTEGER NOT NULL, delivery INTEGER NOT NULL,
            published INTEGER NOT NULL, version INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS packages (
            id TEXT PRIMARY KEY, customer_ref TEXT NOT NULL, title TEXT NOT NULL,
            price INTEGER NOT NULL, terms TEXT NOT NULL, created TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS visits (
            id TEXT PRIMARY KEY, package_id TEXT NOT NULL, event TEXT NOT NULL,
            delta INTEGER NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL,
            UNIQUE(package_id,event), FOREIGN KEY(package_id) REFERENCES packages(id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS commerce_audit (
            id TEXT PRIMARY KEY, action TEXT NOT NULL, reference TEXT NOT NULL,
            created TEXT NOT NULL)""")
        # Additive inventory journal; never reconstruct historical supplier receipts.
        db.execute("""CREATE TABLE IF NOT EXISTS commerce_stock_movements (
            id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
            product_id TEXT NOT NULL REFERENCES products(id),
            movement TEXT NOT NULL, delta INTEGER NOT NULL,
            before_quantity INTEGER NOT NULL, after_quantity INTEGER NOT NULL,
            version_before INTEGER NOT NULL, version_after INTEGER NOT NULL,
            note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE INDEX IF NOT EXISTS commerce_stock_movements_product
            ON commerce_stock_movements(product_id,created_at)""")
        db.execute("PRAGMA foreign_keys=ON")
        yield db


def now():
    return datetime.now(timezone.utc).isoformat()


def audit(db, action, reference):
    db.execute("INSERT INTO commerce_audit VALUES(?,?,?,?)", (uuid.uuid4().hex, action, reference, now()))


def integer(value, name, maximum=100000000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(name + " must be a nonnegative integer")
    return value


def text(value, name, limit, required=True):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError("Invalid " + name)
    return value.strip()


def _stock_event(db, operation_id, product_id, movement, delta, before, after,
                 version_before, version_after, note, actor):
    db.execute("""INSERT INTO commerce_stock_movements
        (id,operation_id,product_id,movement,delta,before_quantity,after_quantity,
         version_before,version_after,note,actor,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        uuid.uuid4().hex, operation_id, product_id, movement, delta,
        before, after, version_before, version_after, note, actor[:100], now()))


def _ensure_stock_baseline(db, row):
    """Legacy catalogue quantity is a snapshot, NOT reconciled historic stock."""
    if not db.execute(
        "SELECT 1 FROM commerce_stock_movements WHERE product_id=? LIMIT 1",
        (row["id"],)
    ).fetchone():
        _stock_event(db, "legacy:" + row["id"], row["id"], "legacy_baseline",
                     0, row["stock"], row["stock"], row["version"], row["version"],
                     "Existing catalogue quantity, historical receipts not verified", "system")


def save_product(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid product")
    ref = data.get("id") or uuid.uuid4().hex
    if not isinstance(ref, str) or len(ref) != 32 or any(c not in "0123456789abcdef" for c in ref):
        raise ValueError("Invalid product reference")
    values = (text(data.get("name"), "name", 120),
              text(data.get("variant", ""), "variant", 100, False),
              text(data.get("description", ""), "description", 1500, False),
              integer(data.get("retail"), "Retail price"),
              integer(data["trade"], "Trade price") if data.get("trade") is not None else None,
              integer(data.get("cost"), "Unit cost"),
              integer(data.get("stock"), "Stock", 100000),
              integer(data.get("delivery"), "Delivery charge"),
              integer(data.get("published"), "Publication flag", 1))
    version = integer(data.get("version", 0), "Version")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if version == 0:
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?,1)", (ref, *values))
            _stock_event(db, "opening:" + ref, ref, "declared_opening",
                         0, values[6], values[6], 1, 1,
                         "Owner-entered opening quantity; supplier evidence not verified", "owner")
        else:
            previous = db.execute("SELECT * FROM products WHERE id=?", (ref,)).fetchone()
            if not previous or previous["version"] != version:
                raise ValueError("Product changed. Reload before saving.")
            if previous["stock"] != values[6]:
                _ensure_stock_baseline(db, previous)
            changed = db.execute("""UPDATE products SET name=?,variant=?,description=?,retail=?,trade=?,
                cost=?,stock=?,delivery=?,published=?,version=version+1 WHERE id=? AND version=?""",
                (*values, ref, version)).rowcount
            if not changed:
                raise ValueError("Product changed. Reload before saving.")
            if previous["stock"] != values[6]:
                _stock_event(db, "catalogue:" + ref + ":" + str(version), ref,
                             "catalogue_edit", values[6] - previous["stock"],
                             previous["stock"], values[6], version, version + 1,
                             "Manual catalogue quantity edit; evidence not verified", "owner")
        audit(db, "product_saved", ref)
    return ref


def basket_quote(lines, fulfilment):
    if fulfilment not in {"collection", "delivery"}:
        raise ValueError("Choose collection or delivery")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
        raise ValueError("Choose 1 to 30 products")
    seen = set()
    total = 0
    delivery = 0
    items = []
    with database() as db:
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("id"), str):
                raise ValueError("Invalid basket item")
            quantity = integer(line.get("quantity"), "Quantity", 100)
            if quantity == 0 or line["id"] in seen:
                raise ValueError("Invalid or duplicate basket item")
            seen.add(line["id"])
            row = db.execute("SELECT * FROM products WHERE id=? AND published=1", (line["id"],)).fetchone()
            if not row or row["stock"] < quantity:
                raise ValueError("A product is unavailable or exceeds current stock")
            amount = quantity * row["retail"]
            total += amount
            delivery = max(delivery, row["delivery"]) if fulfilment == "delivery" else 0
            items.append({"id": row["id"], "name": row["name"], "variant": row["variant"],
                          "quantity": quantity, "unit_pence": row["retail"], "total_pence": amount})
    return {"items": items, "subtotal_pence": total, "delivery_pence": delivery,
            "total_pence": total + delivery, "checkout_enabled": False,
            "stock_reserved": False}


def create_package(data):
    customer = text(data.get("customer_ref"), "customer reference", 100)
    title = text(data.get("title"), "package title", 120)
    terms = text(data.get("terms"), "service conditions", 2000)
    purchased = integer(data.get("visits"), "Purchased visits", 1000)
    if purchased < 1:
        raise ValueError("Enter at least one visit")
    price = integer(data.get("price"), "Package price")
    ref = uuid.uuid4().hex
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)", (ref, customer, title, price, terms, now()))
        db.execute("INSERT INTO visits VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ref, "opening", purchased, "Owner recorded entitlement", now()))
        audit(db, "package_created", ref)
    return ref


def visit_event(ref, event, delta, reason):
    event = text(event, "event reference", 120)
    reason = text(reason, "reason", 500)
    if type(delta) is not int or delta not in {-1, 0, 1} or event == "opening":
        raise ValueError("Invalid visit adjustment")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM packages WHERE id=?", (ref,)).fetchone():
            raise LookupError("Package not found")
        existing = db.execute("SELECT delta,reason FROM visits WHERE package_id=? AND event=?", (ref, event)).fetchone()
        if existing:
            if existing["delta"] != delta or existing["reason"] != reason:
                raise ValueError("Event reference already used for a different entry")
            return False
        balance = db.execute("SELECT COALESCE(SUM(delta),0) FROM visits WHERE package_id=?", (ref,)).fetchone()[0]
        if balance + delta < 0:
            raise ValueError("No visits remaining")
        db.execute("INSERT INTO visits VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, ref, event, delta, reason, now()))
        audit(db, "visit_event", ref)
    return True


STOCK_MOVEMENTS = {"received": 1, "used": -1, "correction_in": 1, "correction_out": -1}


def record_stock_movement(product_id, operation_id, expected_version,
                          movement, quantity, note, actor="owner"):
    """Append-only stock correction with transaction, revision and replay protection."""
    if not isinstance(product_id, str) or not re.fullmatch(r"[0-9a-f]{32}", product_id):
        raise ValueError("Invalid product")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("Invalid stock operation")
    if type(expected_version) is not int or expected_version < 1 or expected_version > 100000000:
        raise ValueError("Invalid stock revision")
    if movement not in STOCK_MOVEMENTS:
        raise ValueError("Invalid stock movement")
    if type(quantity) is not int or not 1 <= quantity <= 100000:
        raise ValueError("Quantity must be between 1 and 100000")
    note = text(note, "stock evidence note", 300)
    if len(note) < 5:
        raise ValueError("Explain why the stock changed")
    delta = STOCK_MOVEMENTS[movement] * quantity
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous_operation = db.execute(
            "SELECT * FROM commerce_stock_movements WHERE operation_id=?",
            (operation_id,)).fetchone()
        if previous_operation:
            if (previous_operation["product_id"], previous_operation["movement"],
                previous_operation["delta"], previous_operation["note"],
                previous_operation["version_before"]) != (
                    product_id, movement, delta, note, expected_version):
                raise ValueError("Operation ID was already used for a different stock change")
            return previous_operation["version_after"]
        row = db.execute("SELECT id,stock,version FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            raise LookupError("Product not found")
        if row["version"] != expected_version:
            raise ValueError("Stock changed elsewhere; reload before adjusting")
        after = row["stock"] + delta
        if after < 0 or after > 100000:
            raise ValueError("Insufficient stock or maximum stock exceeded")
        _ensure_stock_baseline(db, row)
        changed = db.execute(
            "UPDATE products SET stock=?,version=version+1 WHERE id=? AND version=?",
            (after, product_id, expected_version)).rowcount
        if changed != 1:
            raise ValueError("Stock changed elsewhere; reload before adjusting")
        _stock_event(db, operation_id, product_id, movement, delta,
                     row["stock"], after, expected_version, expected_version + 1,
                     note, actor)
        audit(db, "stock_movement", product_id)
        return expected_version + 1


@commerce.route("/hq/shop/stock", methods=["GET", "POST"])
@required
def stock_ledger():
    error = None
    if request.method == "POST":
        protected_write()
        raw_version = request.form.get("version", "")
        raw_quantity = request.form.get("quantity", "")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_version) or not re.fullmatch(
                r"[1-9][0-9]{0,5}", raw_quantity):
            abort(400)
        try:
            record_stock_movement(
                request.form.get("product_id"), request.form.get("operation_id"),
                int(raw_version), request.form.get("movement"), int(raw_quantity),
                request.form.get("note", ""), session.get("google_sub", "owner"))
            return redirect("/hq/shop/stock?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            abort(503)
    with database() as db:
        products = [dict(p, operation_id=uuid.uuid4().hex) for p in db.execute(
            "SELECT id,name,variant,stock,version FROM products ORDER BY name,variant")]
        movements = db.execute("""SELECT m.*,p.name,p.variant FROM commerce_stock_movements m
            JOIN products p ON p.id=m.product_id
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""").fetchall()
    return render_template("commerce_stock.html", products=products, movements=movements,
                           csrf=session.get("csrf"), error=error), (409 if error else 200)


def protected_write():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    if request.content_length is None or request.content_length > 12000:
        abort(413)


def amounts(form, fields):
    result = {}
    for key in fields:
        raw = form.get(key, "")
        if key == "trade" and not raw:
            result[key] = None
        else:
            try:
                result[key] = int(raw)
            except (ValueError, TypeError):
                raise ValueError(key + " must be an integer")
    return result


@commerce.route("/hq/shop", methods=["GET", "POST"])
@required
def admin_shop():
    error = None
    if request.method == "POST":
        protected_write()
        try:
            save_product({**request.form, **amounts(request.form, ["retail", "trade", "cost", "stock", "delivery", "version"]),
                          "published": int(request.form.get("published") == "1")})
            return redirect("/hq/shop", 303)
        except (ValueError, sqlite3.IntegrityError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else "Product already exists. Reload before saving."
    with database() as db:
        products = db.execute("SELECT * FROM products ORDER BY name,variant").fetchall()
    return render_template("commerce_admin.html", products=products, csrf=session.get("csrf"), error=error), (400 if error else 200)


@commerce.get("/book/shop")
def shop():
    with database() as db:
        products = db.execute("""SELECT id,name,variant,description,retail,stock FROM products
            WHERE published=1 ORDER BY name,variant""").fetchall()
    return render_template("commerce_shop.html", products=products,
                           customer_active=bool(active_customer()),
                           customer_csrf=session.get("customer_portal_csrf", ""),
                           operation_id=uuid.uuid4().hex)


@commerce.post("/api/shop/basket")
def check_basket():
    if not request.is_json or request.content_length is None or request.content_length > 8000:
        return jsonify(error="Invalid basket"), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid basket"), 400
    try:
        return jsonify(basket_quote(data.get("lines"), data.get("fulfilment")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@commerce.route("/hq/maintenance/packages", methods=["GET", "POST"])
@required
def packages():
    error = None
    if request.method == "POST":
        protected_write()
        try:
            if request.form.get("package_id"):
                visit_event(request.form["package_id"], request.form.get("event"),
                            {"completed": -1, "complimentary": 0, "credit": 1}[request.form.get("action")],
                            request.form.get("reason"))
            else:
                create_package({**request.form, **amounts(request.form, ["visits", "price"])})
            return redirect("/hq/maintenance/packages", 303)
        except (ValueError, LookupError, KeyError) as exc:
            error = str(exc)
    with database() as db:
        rows = db.execute("""SELECT p.*,COALESCE(SUM(v.delta),0) AS remaining,
            SUM(CASE WHEN v.delta=-1 THEN 1 ELSE 0 END) AS completed
            FROM packages p LEFT JOIN visits v ON p.id=v.package_id GROUP BY p.id ORDER BY p.created DESC""").fetchall()
        ledger = db.execute("SELECT * FROM visits ORDER BY created DESC LIMIT 100").fetchall()
    return render_template("commerce_packages.html", packages=rows, ledger=ledger,
                           csrf=session.get("csrf"), error=error), (400 if error else 200)
