"""Gestione prestiti chiavi - applicazione web (Flask)."""
import base64
import binascii
import csv
import io
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)

import config
import db
import directory
import ricevuta
from auth import AuthError, authenticate, normalize_username

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gestione_chiavi")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.get_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,
)
db.init_app(app)

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()./\s-]{5,25}$")
SIGNATURE_PREFIX = "data:image/png;base64,"


# --- Utilità -------------------------------------------------------------------

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.template_filter("dt")
def format_dt(value, fmt="%d/%m/%Y %H:%M"):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime(fmt)
    except ValueError:
        return value


@app.context_processor
def inject_globals():
    user = session.get("user")
    theme = request.cookies.get("gc_theme")
    consent = request.cookies.get("gc_consent")
    return {"user": user, "can_write": bool(user and user["role"] == "write"),
            "csrf_token": csrf_token, "describe_items": db.describe_items,
            "theme": theme if theme in ("light", "dark") else None,
            "consent": consent if consent in ("all", "necessary") else None,
            "year": datetime.now().year, "copyright_owner": config.COPYRIGHT_OWNER}


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", controller=config.PRIVACY_CONTROLLER, contact=config.PRIVACY_CONTACT,
                           retention_years=config.PRIVACY_RETENTION_YEARS)


@app.before_request
def check_csrf():
    if request.method == "POST":
        token = request.form.get("_csrf", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            abort(400, "Sessione scaduta o token di sicurezza non valido: ricarica la pagina e riprova.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                            "script-src 'self'; frame-ancestors 'none'")
    return resp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def write_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if session["user"]["role"] != "write":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def operator_name():
    u = session["user"]
    return f"{u['display_name']} ({u['username']})"


def clean(value, max_len=500):
    return " ".join((value or "").split())[:max_len]


def valid_signature(sig):
    if not sig.startswith(SIGNATURE_PREFIX) or len(sig) > 3_000_000:
        return False
    try:
        data = base64.b64decode(sig[len(SIGNATURE_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        return False
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Login ---------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        try:
            user = authenticate(username, request.form.get("password", ""))
        except AuthError as exc:
            log.warning("Login fallito per '%s' da %s: %s", username, request.remote_addr, exc)
            flash(str(exc), "error")
            return render_template("login.html", username=username), 401
        session.clear()
        session.permanent = True
        session["user"] = user
        directory.after_login(normalize_username(username), request.form.get("password", ""))
        log.info("Login %s (%s) ruolo=%s", user["username"], request.remote_addr, user["role"])
        nxt = request.args.get("next", "")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = url_for("dashboard")
        return redirect(nxt)
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return render_template("login.html", username="")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Panoramica ------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    types = db.key_type_stats(active_only=True)
    holders = db.borrowers(only_open=True)
    for h in holders:
        h["summary"] = db.describe_items(db.outstanding_items(h["id"]), remaining_only=True)
    totals = {"types": len(types), "total": sum(t["total"] for t in types),
              "out": sum(t["out_qty"] for t in types), "holders": len(holders)}
    return render_template("dashboard.html", types=types, holders=holders, totals=totals)


# --- Chiavi --------------------------------------------------------------------

@app.route("/chiavi")
@login_required
def keys():
    return render_template("keys.html", types=db.key_type_stats())


@app.route("/chiavi/<int:type_id>")
@login_required
def key_detail(type_id):
    kt = db.key_type(type_id) or abort(404)
    numbers = db.numbers_out(type_id) if kt["kind"] == "numbered" else []
    return render_template("key_detail.html", kt=kt, numbers=numbers, holders=db.holders(type_id))


@app.route("/chiavi/nuova", methods=["GET", "POST"])
@write_required
def key_new():
    form = request.form
    if request.method == "POST":
        name = clean(form.get("name"), 120)
        kind = form.get("kind")
        qty = to_int(form.get("total_qty"), -1)
        errors = []
        if not name:
            errors.append("Il nome è obbligatorio.")
        if kind not in ("generic", "numbered"):
            errors.append("Scegli il tipo di chiave.")
        if qty < 0:
            errors.append("Indica una quantità valida.")
        if not errors:
            with db.transaction() as conn:
                type_id = conn.execute(
                    "INSERT INTO key_types (name, description, location, kind, total_qty, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, clean(form.get("description")), clean(form.get("location"), 200), kind,
                     qty, db.now())).lastrowid
            flash(f"Chiave «{name}» creata.", "ok")
            return redirect(url_for("key_detail", type_id=type_id))
        for e in errors:
            flash(e, "error")
    return render_template("key_form.html", kt=None, form=form)


@app.route("/chiavi/<int:type_id>/modifica", methods=["GET", "POST"])
@write_required
def key_edit(type_id):
    kt = db.key_type(type_id) or abort(404)
    form = request.form
    if request.method == "POST":
        name = clean(form.get("name"), 120)
        errors = []
        if not name:
            errors.append("Il nome è obbligatorio.")
        qty = to_int(form.get("total_qty"), -1)
        if qty < kt["out_qty"]:
            errors.append(f"La quantità non può essere inferiore alle chiavi in prestito ({kt['out_qty']}).")
        if not errors:
            with db.transaction() as conn:
                conn.execute("UPDATE key_types SET name = ?, description = ?, location = ?, total_qty = ? "
                             "WHERE id = ?", (name, clean(form.get("description")),
                                              clean(form.get("location"), 200), qty, type_id))
            flash("Modifiche salvate.", "ok")
            return redirect(url_for("key_detail", type_id=type_id))
        for e in errors:
            flash(e, "error")
    return render_template("key_form.html", kt=kt, form=form if request.method == "POST" else kt)


@app.post("/chiavi/<int:type_id>/stato")
@write_required
def key_toggle(type_id):
    kt = db.key_type(type_id) or abort(404)
    if kt["active"] and kt["out_qty"]:
        flash("Non puoi disattivare una chiave con esemplari ancora in prestito.", "error")
    else:
        with db.transaction() as conn:
            conn.execute("UPDATE key_types SET active = ? WHERE id = ?", (0 if kt["active"] else 1, type_id))
        flash("Chiave disattivata." if kt["active"] else "Chiave riattivata.", "ok")
    return redirect(url_for("key_detail", type_id=type_id))


@app.post("/chiavi/<int:type_id>/elimina")
@write_required
def key_delete(type_id):
    kt = db.key_type(type_id) or abort(404)
    if kt["used"]:
        flash("La chiave compare nello storico prestiti: puoi solo disattivarla.", "error")
        return redirect(url_for("key_detail", type_id=type_id))
    with db.transaction() as conn:
        conn.execute("DELETE FROM key_types WHERE id = ?", (type_id,))
    flash(f"Chiave «{kt['name']}» eliminata.", "ok")
    return redirect(url_for("keys"))


# --- Prestiti --------------------------------------------------------------------

def read_person(form, errors):
    first = clean(form.get("first_name"), 100)
    last = clean(form.get("last_name"), 100)
    email = clean(form.get("email"), 200)
    phone = clean(form.get("phone"), 40)
    if not first:
        errors.append("Il nome è obbligatorio.")
    if not last:
        errors.append("Il cognome è obbligatorio.")
    if email and not EMAIL_RE.match(email):
        errors.append("Indirizzo email non valido.")
    if phone and not PHONE_RE.match(phone):
        errors.append("Numero di telefono non valido.")
    return first, last, email, phone


@app.route("/prestiti/nuovo", methods=["GET", "POST"])
@write_required
def loan_new():
    form = request.form
    if request.method == "GET" and request.args.get("dominio"):
        # Pulsante "Prestito" dalla vista Dominio: modulo già compilato con l'utente scelto.
        d = db.directory_user(request.args["dominio"].strip().lower())
        if d:
            form = {"first_name": d["first_name"], "last_name": d["last_name"], "email": d["email"],
                    "phone": d["phone"], "ad_guid": d["guid"]}
    if request.method == "POST":
        errors = []
        first, last, email, phone = read_person(form, errors)
        notes = (form.get("notes") or "").strip()[:2000]
        signature = form.get("signature", "")
        generic, numbered = [], {}
        for k, v in form.items():
            if k.startswith("key_") and v == "1":
                generic.append(to_int(k[4:]))
            elif k.startswith("num_"):
                number = clean(v, 30)
                if number:
                    numbered[to_int(k[4:])] = number
        ad_guid = form.get("ad_guid", "").strip().lower()
        if ad_guid and not (GUID_RE.match(ad_guid) and db.directory_user(ad_guid)):
            ad_guid = None
        extras = [clean(x, 100) for x in form.getlist("extra") if clean(x, 100)]
        if len(extras) > 10:
            errors.append("Al massimo 10 chiavi non in elenco per prestito.")
        if not generic and not numbered and not extras:
            errors.append("Seleziona almeno una chiave.")
        if not valid_signature(signature):
            errors.append("La firma è obbligatoria.")
        if not errors:
            try:
                loan_id = db.create_loan(first, last, email, phone, notes, signature, operator_name(),
                                         generic, numbered, extras[:10], ad_guid)
            except db.Unavailable as exc:
                errors.append(str(exc))
            else:
                log.info("Prestito #%s a '%s %s' da %s", loan_id, first, last, session["user"]["username"])
                flash("Prestito registrato.", "ok")
                return redirect(url_for("loan_detail", loan_id=loan_id))
        for e in errors:
            flash(e, "error")
    types = db.key_type_stats(active_only=True)
    out_numbers = {t["id"]: [n["key_number"] for n in db.numbers_out(t["id"])]
                   for t in types if t["kind"] == "numbered"}
    extras = [x for x in (form.getlist("extra") if hasattr(form, "getlist") else []) if x.strip()] or [""]
    linked = db.directory_user(form.get("ad_guid", "")) if form.get("ad_guid") else None
    return render_template("loan_new.html", types=types, out_numbers=out_numbers, form=form, extras=extras,
                           linked=linked), \
        (400 if request.method == "POST" else 200)


@app.route("/prestiti")
@login_required
def loans():
    status = request.args.get("stato", "aperti")
    if status not in ("aperti", "chiusi", "tutti"):
        status = "aperti"
    q = clean(request.args.get("q"), 100)
    return render_template("loans.html", loans=db.loans(status, q), status=status, q=q)


@app.route("/prestiti/<int:loan_id>")
@login_required
def loan_detail(loan_id):
    ln = db.loan(loan_id) or abort(404)
    items = db.loan_items([loan_id]).get(loan_id, [])
    return render_template("loan_detail.html", loan=ln, items=items,
                           returns=db.returns(loan_id=loan_id),
                           remaining=sum(i["remaining"] for i in items))


def csv_response(header, data, prefix):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(header)
    w.writerows(data)
    filename = f"{prefix}_{datetime.now():%Y%m%d}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/prestiti/export.csv")
@login_required
def loans_export():
    data = []
    for ln in db.loans("tutti", limit=1_000_000):
        for it in ln["items"]:
            data.append([ln["id"], format_dt(ln["created_at"]), ln["last_name"], ln["first_name"], ln["email"],
                         ln["phone"], it["type_name"], it["key_number"] or "", it["quantity"],
                         it["returned_qty"], it["remaining"], ln["operator"], ln["notes"]])
    return csv_response(["Prestito", "Data", "Cognome", "Nome", "Email", "Telefono", "Chiave", "Numero",
                         "Quantità", "Riconsegnate", "Da riconsegnare", "Operatore", "Note"],
                        data, "prestiti_chiavi")


# --- Ricevute PDF (dal modello Excel) ----------------------------------------------

def receipt_rows(items):
    result = []
    for it in items:
        key = it["type_name"] + (f" n. {it['key_number']}" if it["key_number"] else "")
        if it["quantity"] > 1:
            key += f" ×{it['quantity']}"
        returned = it["returned_qty"] >= it["quantity"]
        date_in = format_dt(it["return_date"], "%d/%m/%Y") if it["return_date"] else ""
        if it["returned_qty"] and not returned:
            date_in += f" ({it['returned_qty']} di {it['quantity']})"
        result.append({"date_out": format_dt(it["loan_date"], "%d/%m/%Y"), "key": key,
                       "sig_out": it["loan_signature"], "date_in": date_in,
                       "sig_in": it["return_signature"] if it["returned_qty"] else ""})
    return result


def pdf_response(data, filename):
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "utente"


@app.route("/prestiti/<int:loan_id>/ricevuta.pdf")
@login_required
def loan_pdf(loan_id):
    ln = db.loan(loan_id) or abort(404)
    pdf = ricevuta.build_pdf({"operator": ln["operator"], "name": f"{ln['last_name']} {ln['first_name']}"},
                             receipt_rows(db.receipt_items(loan_id=loan_id)), footer=f"Prestito n. {loan_id}")
    return pdf_response(pdf, f"ricevuta_{loan_id}_{safe_filename(ln['last_name'])}.pdf")


@app.route("/utenti/<int:borrower_id>/scheda.pdf")
@login_required
def borrower_pdf(borrower_id):
    b = db.borrower(borrower_id) or abort(404)
    pdf = ricevuta.build_pdf({"operator": operator_name(), "name": f"{b['last_name']} {b['first_name']}"},
                             receipt_rows(db.receipt_items(borrower_id=borrower_id)),
                             footer=f"Scheda chiavi di {b['last_name']} {b['first_name']}")
    return pdf_response(pdf, f"scheda_chiavi_{safe_filename(b['last_name'] + '_' + b['first_name'])}.pdf")


# --- Storico ---------------------------------------------------------------------

def history_filters():
    args = request.args
    f = {"q": clean(args.get("q"), 100), "kind": args.get("tipo", "tutti"),
         "date_from": args.get("da", ""), "date_to": args.get("a", ""), "type_id": to_int(args.get("chiave")) or None}
    if f["kind"] not in ("tutti", "prestiti", "riconsegne"):
        f["kind"] = "tutti"
    for k in ("date_from", "date_to"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[k]):
            f[k] = ""
    return f


@app.route("/storico")
@login_required
def history():
    f = history_filters()
    events, truncated = db.history(**f)
    today = date.today()
    presets = []
    for label, start, end in [("Sempre", None, None), ("Oggi", today, today),
                              ("Ultimi 7 giorni", today - timedelta(days=6), today),
                              ("Ultimi 30 giorni", today - timedelta(days=29), today),
                              ("Questo mese", today.replace(day=1), today),
                              ("Quest'anno", today.replace(month=1, day=1), today)]:
        da, a = (start.isoformat(), end.isoformat()) if start else ("", "")
        args = {k: v for k, v in request.args.items() if k not in ("da", "a") and v}
        args.update({k: v for k, v in (("da", da), ("a", a)) if v})
        presets.append({"label": label, "url": url_for("history", **args),
                        "active": (f["date_from"], f["date_to"]) == (da, a)})
    return render_template("history.html", events=events, truncated=truncated, f=f, presets=presets,
                           types=db.key_type_stats(), export_args=request.args.to_dict())


@app.route("/storico/export.csv")
@login_required
def history_export():
    events, _ = db.history(**history_filters(), limit=1_000_000)
    data = [[format_dt(e["created_at"]), "Prestito" if e["kind"] == "prestito" else "Riconsegna", e["id"],
             e["name"], e["email"], e["phone"], e["summary"], e["operator"], e["notes"]] for e in events]
    return csv_response(["Data", "Movimento", "N.", "Utente", "Email", "Telefono", "Chiavi", "Operatore",
                         "Note"], data, "storico_chiavi")


# --- Utenti e riconsegne -------------------------------------------------------------

@app.route("/utenti")
@login_required
def borrowers():
    q = clean(request.args.get("q"), 100)
    only_open = request.args.get("aperti") == "1"
    sort = request.args.get("ordina", "cognome")
    if sort not in db.BORROWER_SORTS:
        sort = "cognome"
    desc = request.args.get("verso") == "desc"
    view = "dominio" if request.args.get("vista") == "dominio" else ("aperti" if only_open else "tutti")
    people = db.directory_users(q) if view == "dominio" else db.borrowers(q, only_open, sort, desc)
    return render_template("borrowers.html", borrowers=people, q=q, only_open=only_open, sort=sort, desc=desc,
                           view=view, sync=directory.status())


@app.post("/utenti/sincronizza")
@write_required
def directory_sync():
    if directory.sync_now_async():
        flash("Aggiornamento degli utenti di dominio avviato: ci vuole qualche istante.", "ok")
    else:
        flash("Aggiornamento non avviato (già in corso o account di servizio non configurato).", "warn")
    return redirect(url_for("borrowers", vista="dominio"))


@app.route("/utenti/<int:borrower_id>", methods=["GET", "POST"])
@login_required
def borrower_detail(borrower_id):
    b = db.borrower(borrower_id) or abort(404)
    if request.method == "POST":
        if session["user"]["role"] != "write":
            abort(403)
        errors = []
        form = request.form
        if b["ad_guid"]:
            # Per gli utenti di dominio nome e cognome arrivano da Active Directory.
            form = form.copy()
            form["first_name"], form["last_name"] = b["first_name"], b["last_name"]
        first, last, email, phone = read_person(form, errors)
        for e in errors:
            flash(e, "error")
        if not errors:
            with db.transaction() as conn:
                conn.execute("UPDATE borrowers SET first_name = ?, last_name = ?, name = ?, email = ?, phone = ? "
                             "WHERE id = ?", (first, last, db.full_name(first, last), email, phone, borrower_id))
            flash("Dati aggiornati.", "ok")
        return redirect(url_for("borrower_detail", borrower_id=borrower_id))
    return render_template("borrower_detail.html", b=b, items=db.outstanding_items(borrower_id),
                           loans=db.loans("tutti", borrower_id=borrower_id),
                           returns=db.returns(borrower_id=borrower_id))


@app.route("/utenti/<int:borrower_id>/riconsegna", methods=["GET", "POST"])
@write_required
def return_new(borrower_id):
    b = db.borrower(borrower_id) or abort(404)
    form = request.form
    if request.method == "POST":
        quantities = {}
        errors = []
        for k, v in form.items():
            if k.startswith("ret_"):
                qty = to_int(v)
                if qty < 0:
                    errors.append("Quantità non valida.")
                elif qty > 0:
                    quantities[to_int(k[4:])] = qty
        signature = form.get("signature", "")
        if not valid_signature(signature):
            errors.append("La firma di chi riconsegna è obbligatoria.")
        if not quantities:
            errors.append("Seleziona almeno una chiave da riconsegnare.")
        if not errors:
            try:
                return_id = db.create_return(borrower_id, quantities, signature,
                                             (form.get("notes") or "").strip()[:2000], operator_name())
            except db.Unavailable as exc:
                errors.append(str(exc))
            else:
                log.info("Riconsegna #%s da '%s' registrata da %s", return_id, b["name"],
                         session["user"]["username"])
                left = db.outstanding_items(borrower_id)
                flash("Riconsegna registrata." + (" L'utente non ha più chiavi in prestito." if not left else ""),
                      "ok")
                return redirect(url_for("borrower_detail", borrower_id=borrower_id))
        for e in errors:
            flash(e, "error")
    items = db.outstanding_items(borrower_id)
    if not items and request.method == "GET":
        flash("Questo utente non ha chiavi in prestito.", "warn")
        return redirect(url_for("borrower_detail", borrower_id=borrower_id))
    return render_template("return_new.html", b=b, items=items, form=form), \
        (400 if request.method == "POST" else 200)


@app.route("/riconsegne/<int:return_id>")
@login_required
def return_detail(return_id):
    found = db.returns(return_ids=[return_id])
    if not found:
        abort(404)
    return render_template("return_detail.html", r=found[0])


@app.route("/api/utenti")
@login_required
def api_borrowers():
    q = clean(request.args.get("q"), 100)
    if len(q) < 2:
        return jsonify([])
    return jsonify([{k: p[k] for k in ("first_name", "last_name", "email", "phone", "out", "source", "guid",
                                       "username")} for p in db.suggest_people(q)])


# --- Errori ----------------------------------------------------------------------

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(exc):
    titles = {400: "Richiesta non valida", 403: "Accesso negato", 404: "Pagina non trovata"}
    messages = {403: "Il tuo profilo è in sola lettura: non puoi eseguire questa operazione."}
    message = messages.get(exc.code) or (exc.description if exc.code == 400 else "")
    return render_template("error.html", title=titles[exc.code], message=message), exc.code


directory.start_scheduler()


if __name__ == "__main__":
    if config.DEV_USERS:
        log.warning("ATTENZIONE: autenticazione di TEST attiva (GC_DEV_USERS). Non usare in produzione!")
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress non installato: uso il server di sviluppo di Flask")
        app.run(host=config.HOST, port=config.PORT)
    else:
        log.info("Gestione Chiavi in ascolto su http://%s:%s", config.HOST, config.PORT)
        serve(app, host=config.HOST, port=config.PORT, threads=8)
