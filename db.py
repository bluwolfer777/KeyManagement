"""Accesso al database SQLite e logica di prestito/riconsegna."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from flask import g

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS key_types (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL CHECK (kind IN ('generic', 'numbered')),
    total_qty   INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS borrowers (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL DEFAULT '',
    last_name   TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,                  -- "Nome Cognome"
    email       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    ad_guid     TEXT,                           -- collegamento all'utente di dominio, se lo è
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory_users (     -- copia locale degli utenti di dominio
    guid        TEXT PRIMARY KEY,               -- objectGUID
    username    TEXT NOT NULL DEFAULT '',
    first_name  TEXT NOT NULL DEFAULT '',       -- givenName
    last_name   TEXT NOT NULL DEFAULT '',       -- sn
    email       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    synced_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY,
    borrower_id INTEGER NOT NULL REFERENCES borrowers(id),
    first_name  TEXT NOT NULL DEFAULT '',       -- dati al momento del prestito
    last_name   TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    signature   TEXT NOT NULL,                  -- PNG data URL
    notes       TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_items (
    id           INTEGER PRIMARY KEY,
    loan_id      INTEGER NOT NULL REFERENCES loans(id),
    key_type_id  INTEGER REFERENCES key_types(id),  -- NULL per le chiavi non in elenco
    custom_name  TEXT,                           -- nome della chiave non in elenco, scritto al prestito
    key_number   TEXT,                           -- solo per 'numbered', scritto al prestito
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    returned_qty INTEGER NOT NULL DEFAULT 0 CHECK (returned_qty >= 0 AND returned_qty <= quantity),
    CHECK (key_type_id IS NOT NULL OR custom_name IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS returns (
    id          INTEGER PRIMARY KEY,
    borrower_id INTEGER NOT NULL REFERENCES borrowers(id),
    signature   TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS return_items (
    id           INTEGER PRIMARY KEY,
    return_id    INTEGER NOT NULL REFERENCES returns(id),
    loan_item_id INTEGER NOT NULL REFERENCES loan_items(id),
    quantity     INTEGER NOT NULL CHECK (quantity > 0)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_borrowers_last ON borrowers(last_name COLLATE NOCASE, first_name COLLATE NOCASE);
CREATE UNIQUE INDEX IF NOT EXISTS ux_borrowers_guid ON borrowers(ad_guid) WHERE ad_guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_directory_names ON directory_users(last_name COLLATE NOCASE, first_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_loans_borrower ON loans(borrower_id);
CREATE INDEX IF NOT EXISTS ix_loans_date ON loans(created_at);
CREATE INDEX IF NOT EXISTS ix_items_loan ON loan_items(loan_id);
CREATE INDEX IF NOT EXISTS ix_items_type ON loan_items(key_type_id, key_number);
CREATE INDEX IF NOT EXISTS ix_returns_date ON returns(created_at);
CREATE INDEX IF NOT EXISTS ix_return_items_li ON return_items(loan_item_id);
CREATE INDEX IF NOT EXISTS ix_return_items_ret ON return_items(return_id);
"""


class Unavailable(Exception):
    """Richiesta non soddisfacibile (chiave non disponibile, quantità errata...)."""


def now():
    return datetime.now().isoformat(timespec="seconds")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def full_name(first, last):
    return f"{first} {last}".strip()


def split_name(name):
    """Per i dati preesistenti: prima parola = nome, il resto = cognome."""
    parts = (name or "").split()
    if len(parts) < 2:
        return "", " ".join(parts)
    return parts[0], " ".join(parts[1:])


def connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Aggiorna i database creati con la versione precedente."""
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # loan_items va ricostruita (procedura SQLite: nuova tabella, copia, drop, rename) se:
        # - ha ancora il riferimento agli esemplari numerati (key_copies), ora il numero si scrive al prestito;
        # - non ammette ancora le chiavi non in elenco (custom_name, key_type_id facoltativo).
        cols = _columns(conn, "loan_items")
        if "key_copy_id" in cols or "custom_name" not in cols:
            create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS loan_items"):]
            create = create[:create.index(";\n")].replace("IF NOT EXISTS loan_items", "loan_items_new")
            conn.execute(create)
            if "key_copy_id" in cols:
                number, join = "c.number", "LEFT JOIN key_copies c ON c.id = li.key_copy_id"
            else:
                number, join = "li.key_number", ""
            conn.execute(f"""INSERT INTO loan_items_new (id, loan_id, key_type_id, key_number, quantity, returned_qty)
                             SELECT li.id, li.loan_id, li.key_type_id, {number}, li.quantity, li.returned_qty
                             FROM loan_items li {join}""")
            conn.execute("DROP TABLE loan_items")
            conn.execute("ALTER TABLE loan_items_new RENAME TO loan_items")
        if "key_copies" in tables:
            conn.execute("""UPDATE key_types SET total_qty =
                                (SELECT COUNT(*) FROM key_copies c WHERE c.key_type_id = key_types.id AND c.active = 1)
                            WHERE kind = 'numbered'""")
            conn.execute("DROP TABLE key_copies")
        if "ad_guid" not in _columns(conn, "borrowers"):
            conn.execute("ALTER TABLE borrowers ADD COLUMN ad_guid TEXT")
        # Nome e cognome separati
        for table in ("borrowers", "loans"):
            cols = _columns(conn, table)
            if "last_name" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN first_name TEXT NOT NULL DEFAULT ''")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")
                for r in conn.execute(f"SELECT id, name FROM {table}").fetchall():
                    first, last = split_name(r["name"])
                    conn.execute(f"UPDATE {table} SET first_name = ?, last_name = ? WHERE id = ?",
                                 (first, last, r["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def init_app(app):
    conn = connect()
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(INDEXES)
    conn.close()

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()


@contextmanager
def transaction():
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def rows(sql, args=()):
    return [dict(r) for r in get_db().execute(sql, args).fetchall()]


def row(sql, args=()):
    r = get_db().execute(sql, args).fetchone()
    return dict(r) if r else None


# --- Chiavi ------------------------------------------------------------------

def key_type_stats(active_only=False, type_id=None):
    where, args = [], []
    if active_only:
        where.append("kt.active = 1")
    if type_id is not None:
        where.append("kt.id = ?")
        args.append(type_id)
    sql = f"""
        SELECT kt.*, kt.total_qty AS total,
          COALESCE((SELECT SUM(li.quantity - li.returned_qty) FROM loan_items li
                    WHERE li.key_type_id = kt.id), 0) AS out_qty,
          (SELECT COUNT(*) FROM loan_items li WHERE li.key_type_id = kt.id) AS used
        FROM key_types kt
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY kt.name COLLATE NOCASE"""
    result = rows(sql, args)
    for r in result:
        r["available"] = max(r["total"] - r["out_qty"], 0)
    return result


def key_type(type_id):
    found = key_type_stats(type_id=type_id)
    return found[0] if found else None


def numbers_out(type_id):
    """Numeri attualmente in prestito per una chiave numerata, con chi li ha."""
    result = rows("""
        SELECT li.key_number, li.loan_id, l.created_at AS since, b.id AS holder_id, b.name AS holder
        FROM loan_items li
        JOIN loans l ON l.id = li.loan_id
        JOIN borrowers b ON b.id = l.borrower_id
        WHERE li.key_type_id = ? AND li.key_number IS NOT NULL AND li.returned_qty < li.quantity""",
                  (type_id,))
    result.sort(key=lambda r: natural_key(r["key_number"]))
    return result


def holders(type_id):
    return rows("""
        SELECT b.id, b.name, SUM(li.quantity - li.returned_qty) AS qty, MIN(l.created_at) AS since
        FROM loan_items li
        JOIN loans l ON l.id = li.loan_id
        JOIN borrowers b ON b.id = l.borrower_id
        WHERE li.key_type_id = ? AND li.returned_qty < li.quantity
        GROUP BY b.id ORDER BY b.last_name COLLATE NOCASE, b.first_name COLLATE NOCASE""", (type_id,))


def type_out_qty(conn, type_id):
    return conn.execute("SELECT COALESCE(SUM(quantity - returned_qty), 0) FROM loan_items "
                        "WHERE key_type_id = ?", (type_id,)).fetchone()[0]


# --- Utenti (chi prende in prestito) -------------------------------------------

BORROWER_SORTS = {
    "cognome": ("b.last_name COLLATE NOCASE", "b.last_name = ''"),
    "nome": ("b.first_name COLLATE NOCASE", "b.first_name = ''"),
    "email": ("b.email COLLATE NOCASE", "b.email = ''"),
    "telefono": ("b.phone", "b.phone = ''"),
    "chiavi": ("out_qty", None),
    "ultimo": ("last_loan", "last_loan IS NULL"),
}


def borrowers(q="", only_open=False, sort="cognome", desc=False):
    col, empty_last = BORROWER_SORTS.get(sort, BORROWER_SORTS["cognome"])
    order = (f"{empty_last}, " if empty_last else "") + f"{col} {'DESC' if desc else 'ASC'}, " \
        "b.last_name COLLATE NOCASE, b.first_name COLLATE NOCASE"
    sql = f"""
        SELECT b.*, COALESCE(SUM(li.quantity - li.returned_qty), 0) AS out_qty,
               COUNT(DISTINCT l.id) AS n_loans, MAX(l.created_at) AS last_loan
        FROM borrowers b
        LEFT JOIN loans l ON l.borrower_id = b.id
        LEFT JOIN loan_items li ON li.loan_id = l.id
        WHERE (? = '' OR b.name LIKE ? OR (b.last_name || ' ' || b.first_name) LIKE ?
               OR b.email LIKE ? OR b.phone LIKE ?)
        GROUP BY b.id
        HAVING (? = 0 OR out_qty > 0)
        ORDER BY {order}"""
    like = f"%{q}%"
    return rows(sql, (q, like, like, like, like, 1 if only_open else 0))


def borrower(borrower_id):
    return row("SELECT * FROM borrowers WHERE id = ?", (borrower_id,))


def outstanding_items(borrower_id):
    result = rows("""
        SELECT li.*, li.quantity - li.returned_qty AS remaining, l.created_at AS loan_date,
               COALESCE(kt.name, li.custom_name) AS type_name, COALESCE(kt.kind, 'adhoc') AS kind
        FROM loan_items li
        JOIN loans l ON l.id = li.loan_id
        LEFT JOIN key_types kt ON kt.id = li.key_type_id
        WHERE l.borrower_id = ? AND li.returned_qty < li.quantity""", (borrower_id,))
    result.sort(key=lambda r: (r["type_name"].lower(), natural_key(r["key_number"]), r["loan_date"]))
    return result


def upsert_borrower(conn, first, last, email, phone, ad_guid=None):
    """Trova o crea chi riceve le chiavi. Con ad_guid (utente scelto dal dominio) nome e cognome
    sono quelli di Active Directory e l'utente resta collegato al suo account."""
    d = conn.execute("SELECT * FROM directory_users WHERE guid = ?", (ad_guid,)).fetchone() if ad_guid else None
    if d:
        first, last = d["first_name"], d["last_name"]
        email, phone = email or d["email"], phone or d["phone"]
        found = conn.execute("SELECT * FROM borrowers WHERE ad_guid = ?", (d["guid"],)).fetchone() or             conn.execute("SELECT * FROM borrowers WHERE ad_guid IS NULL AND first_name = ? COLLATE NOCASE "
                         "AND last_name = ? COLLATE NOCASE ORDER BY id LIMIT 1", (first, last)).fetchone()
        if found:
            conn.execute("UPDATE borrowers SET ad_guid = ?, first_name = ?, last_name = ?, name = ?, "
                         "email = CASE WHEN ? <> '' THEN ? ELSE email END, "
                         "phone = CASE WHEN ? <> '' THEN ? ELSE phone END WHERE id = ?",
                         (d["guid"], first, last, full_name(first, last), email, email, phone, phone, found["id"]))
            return found["id"]
        return conn.execute("INSERT INTO borrowers (first_name, last_name, name, email, phone, ad_guid, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (first, last, full_name(first, last), email, phone, d["guid"], now())).lastrowid
    found = conn.execute("SELECT * FROM borrowers WHERE first_name = ? COLLATE NOCASE "
                         "AND last_name = ? COLLATE NOCASE ORDER BY ad_guid IS NOT NULL, id LIMIT 1",
                         (first, last)).fetchone()
    if found:
        conn.execute("UPDATE borrowers SET email = CASE WHEN ? <> '' THEN ? ELSE email END, "
                     "phone = CASE WHEN ? <> '' THEN ? ELSE phone END WHERE id = ?",
                     (email, email, phone, phone, found["id"]))
        return found["id"]
    return conn.execute("INSERT INTO borrowers (first_name, last_name, name, email, phone, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (first, last, full_name(first, last), email, phone, now())).lastrowid


def _token_filter(columns, q):
    """Ogni parola cercata deve comparire in almeno una delle colonne (ordine libero: 'rossi mario')."""
    where, args = [], []
    for token in q.split()[:5]:
        where.append("(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")")
        args += [f"%{token}%"] * len(columns)
    return " AND ".join(where) or "1", args


def suggest_people(q, limit=15):
    """Suggerimenti per il prestito: prima chi ha già avuto chiavi, poi gli utenti del dominio."""
    where, args = _token_filter(["b.first_name", "b.last_name", "b.email", "b.phone"], q)
    local = rows(f"""
        SELECT b.first_name, b.last_name, b.email, b.phone, b.ad_guid AS guid, d.username,
               COALESCE((SELECT SUM(li.quantity - li.returned_qty) FROM loans l JOIN loan_items li
                         ON li.loan_id = l.id WHERE l.borrower_id = b.id), 0) AS out
        FROM borrowers b LEFT JOIN directory_users d ON d.guid = b.ad_guid
        WHERE {where}
        ORDER BY b.last_name COLLATE NOCASE, b.first_name COLLATE NOCASE LIMIT 8""", args)
    for r in local:
        r["source"] = "dominio" if r["guid"] else "locale"
    taken = {r["guid"] for r in local if r["guid"]}
    where, args = _token_filter(["first_name", "last_name", "username", "email"], q)
    domain = rows(f"""
        SELECT first_name, last_name, email, phone, guid, username, 0 AS out FROM directory_users
        WHERE active = 1 AND {where}
        ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE LIMIT ?""", args + [limit + len(taken)])
    result = local + [dict(r, source="dominio") for r in domain if r["guid"] not in taken]
    return result[:limit]


def directory_user(guid):
    return row("SELECT * FROM directory_users WHERE guid = ? AND active = 1", (guid,))


def directory_users(q="", limit=300):
    where, args = _token_filter(["d.first_name", "d.last_name", "d.username", "d.email", "d.phone"], q)
    return rows(f"""
        SELECT d.*, b.id AS borrower_id,
               COALESCE((SELECT SUM(li.quantity - li.returned_qty) FROM loans l JOIN loan_items li
                         ON li.loan_id = l.id WHERE l.borrower_id = b.id), 0) AS out_qty
        FROM directory_users d LEFT JOIN borrowers b ON b.ad_guid = d.guid
        WHERE d.active = 1 AND {where}
        ORDER BY d.last_name COLLATE NOCASE, d.first_name COLLATE NOCASE LIMIT ?""", args + [limit])


# --- Prestiti ------------------------------------------------------------------

def create_loan(first, last, email, phone, notes, signature, operator, generic, numbered, extras=(), ad_guid=None):
    """generic: id delle chiavi senza numero; numbered: {key_type_id: numero scritto dall'operatore};
    extras: nomi di chiavi non in elenco, scritti al momento e non salvati nell'anagrafica chiavi.
    Ogni utente può avere al massimo una chiave per tipo."""
    with transaction() as conn:
        borrower_id = upsert_borrower(conn, first, last, email, phone, ad_guid)
        b = conn.execute("SELECT first_name, last_name, email, phone FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
        first, last = b["first_name"], b["last_name"]
        email, phone = email or b["email"], phone or b["phone"]
        requested = [(type_id, "generic", None) for type_id in generic] + \
                    [(type_id, "numbered", number) for type_id, number in numbered.items()]
        items = []
        for type_id, kind, number in requested:
            kt = conn.execute("SELECT * FROM key_types WHERE id = ? AND active = 1 AND kind = ?",
                              (type_id, kind)).fetchone()
            if not kt:
                raise Unavailable("Tipo di chiave non valido o disattivato.")
            held = conn.execute("""SELECT li.key_number FROM loan_items li JOIN loans l ON l.id = li.loan_id
                                   WHERE l.borrower_id = ? AND li.key_type_id = ?
                                     AND li.returned_qty < li.quantity""", (borrower_id, type_id)).fetchone()
            if held:
                which = f"la n. {held['key_number']}" if held["key_number"] else "una"
                raise Unavailable(f"«{kt['name']}»: {full_name(first, last)} ne ha già {which} "
                                  "(massimo una per utente).")
            if number and conn.execute("""SELECT 1 FROM loan_items WHERE key_type_id = ?
                                          AND key_number = ? COLLATE NOCASE AND returned_qty < quantity""",
                                       (type_id, number)).fetchone():
                raise Unavailable(f"«{kt['name']}» n. {number} risulta già in prestito.")
            if kt["total_qty"] - type_out_qty(conn, type_id) < 1:
                raise Unavailable(f"«{kt['name']}»: nessuna copia disponibile.")
            items.append((type_id, None, number, 1))
        items += [(None, name, None, 1) for name in extras]
        loan_id = conn.execute(
            "INSERT INTO loans (borrower_id, first_name, last_name, name, email, phone, signature, notes, "
            "operator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (borrower_id, first, last, full_name(first, last), email, phone, signature, notes, operator,
             now())).lastrowid
        conn.executemany(
            "INSERT INTO loan_items (loan_id, key_type_id, custom_name, key_number, quantity) VALUES (?, ?, ?, ?, ?)",
            [(loan_id, t, c, n, q) for t, c, n, q in items])
        return loan_id


def held_numbered_types(borrower_id):
    """{key_type_id: numero} delle chiavi numerate che l'utente ha in mano."""
    return {r["key_type_id"]: r["key_number"] for r in rows("""
        SELECT li.key_type_id, li.key_number FROM loan_items li JOIN loans l ON l.id = li.loan_id
        WHERE l.borrower_id = ? AND li.key_number IS NOT NULL AND li.returned_qty < li.quantity""",
                                                             (borrower_id,))}


def loan(loan_id):
    return row("SELECT * FROM loans WHERE id = ?", (loan_id,))


def loan_items(loan_ids):
    if not loan_ids:
        return {}
    marks = ",".join("?" * len(loan_ids))
    result = rows(f"""
        SELECT li.*, li.quantity - li.returned_qty AS remaining, COALESCE(kt.name, li.custom_name) AS type_name, COALESCE(kt.kind, 'adhoc') AS kind
        FROM loan_items li
        LEFT JOIN key_types kt ON kt.id = li.key_type_id
        WHERE li.loan_id IN ({marks})""", list(loan_ids))
    result.sort(key=lambda r: (r["type_name"].lower(), natural_key(r["key_number"])))
    by_loan = {}
    for r in result:
        by_loan.setdefault(r["loan_id"], []).append(r)
    return by_loan


def loans(status="aperti", q="", borrower_id=None, limit=300):
    where, args = [], []
    if q:
        where.append("(l.name LIKE ? OR (l.last_name || ' ' || l.first_name) LIKE ? OR l.email LIKE ? "
                     "OR l.phone LIKE ? OR l.operator LIKE ?)")
        args += [f"%{q}%"] * 5
    if borrower_id is not None:
        where.append("l.borrower_id = ?")
        args.append(borrower_id)
    having = {"aperti": "HAVING remaining > 0", "chiusi": "HAVING remaining = 0"}.get(status, "")
    result = rows(f"""
        SELECT l.id, l.borrower_id, l.first_name, l.last_name, l.name, l.email, l.phone, l.notes,
               l.operator, l.created_at,
               SUM(li.quantity) AS total, SUM(li.quantity - li.returned_qty) AS remaining
        FROM loans l JOIN loan_items li ON li.loan_id = l.id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY l.id {having}
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT ?""", args + [limit])
    items = loan_items([r["id"] for r in result])
    for r in result:
        r["items"] = items.get(r["id"], [])
    return result


def describe_items(items, remaining_only=False):
    """Riassunto testuale: 'Portone ×2; Armadietto n. 7'."""
    groups = {}
    for it in items:
        qty = it["remaining"] if remaining_only else it["quantity"]
        if qty <= 0:
            continue
        g_ = groups.setdefault(it["type_name"], {"qty": 0, "numbers": []})
        if it.get("key_number"):
            g_["numbers"].append(it["key_number"])
        else:
            g_["qty"] += qty
    parts = []
    for name, g_ in groups.items():
        if g_["numbers"]:
            parts.append(f"{name} n. {', '.join(sorted(g_['numbers'], key=natural_key))}")
        if g_["qty"]:
            parts.append(f"{name} ×{g_['qty']}" if g_["qty"] > 1 else name)
    return "; ".join(parts)


# --- Riconsegne ----------------------------------------------------------------

def create_return(borrower_id, quantities, signature, notes, operator):
    """quantities: {loan_item_id: quantità da rendere}"""
    with transaction() as conn:
        checked = []
        for li_id, qty in quantities.items():
            it = conn.execute("""SELECT li.*, l.borrower_id FROM loan_items li
                                 JOIN loans l ON l.id = li.loan_id WHERE li.id = ?""", (li_id,)).fetchone()
            if not it or it["borrower_id"] != borrower_id:
                raise Unavailable("Chiave non associata a questo utente.")
            if qty > it["quantity"] - it["returned_qty"]:
                raise Unavailable("Quantità da riconsegnare superiore a quella in prestito "
                                  "(forse è già stata riconsegnata).")
            checked.append((li_id, qty))
        return_id = conn.execute(
            "INSERT INTO returns (borrower_id, signature, notes, operator, created_at) VALUES (?, ?, ?, ?, ?)",
            (borrower_id, signature, notes, operator, now())).lastrowid
        for li_id, qty in checked:
            conn.execute("INSERT INTO return_items (return_id, loan_item_id, quantity) VALUES (?, ?, ?)",
                         (return_id, li_id, qty))
            conn.execute("UPDATE loan_items SET returned_qty = returned_qty + ? WHERE id = ?", (qty, li_id))
        return return_id


def returns(borrower_id=None, loan_id=None, return_ids=None):
    where, args = [], []
    if borrower_id is not None:
        where.append("r.borrower_id = ?")
        args.append(borrower_id)
    if loan_id is not None:
        where.append("r.id IN (SELECT ri2.return_id FROM return_items ri2 JOIN loan_items li2 "
                     "ON li2.id = ri2.loan_item_id WHERE li2.loan_id = ?)")
        args.append(loan_id)
    if return_ids is not None:
        if not return_ids:
            return []
        where.append(f"r.id IN ({','.join('?' * len(return_ids))})")
        args += list(return_ids)
    data = rows(f"""
        SELECT r.id, r.borrower_id, r.signature, r.notes, r.operator, r.created_at,
               ri.quantity, li.loan_id, li.key_number, COALESCE(kt.name, li.custom_name) AS type_name, b.name AS borrower_name
        FROM returns r
        JOIN return_items ri ON ri.return_id = r.id
        JOIN loan_items li ON li.id = ri.loan_item_id
        LEFT JOIN key_types kt ON kt.id = li.key_type_id
        JOIN borrowers b ON b.id = r.borrower_id
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY r.created_at DESC, r.id DESC""", args)
    grouped = {}
    for d in data:
        ret = grouped.setdefault(d["id"], {k: d[k] for k in (
            "id", "borrower_id", "borrower_name", "signature", "notes", "operator", "created_at")})
        ret.setdefault("items", []).append({
            "type_name": d["type_name"], "key_number": d["key_number"], "loan_id": d["loan_id"],
            "quantity": d["quantity"], "remaining": d["quantity"]})
    for ret in grouped.values():
        ret["summary"] = describe_items(ret["items"])
    return list(grouped.values())


def receipt_items(loan_id=None, borrower_id=None):
    """Righe per la ricevuta PDF: una per chiave, con firma e data dell'ultima riconsegna."""
    where, args = ("l.id = ?", loan_id) if loan_id is not None else ("l.borrower_id = ?", borrower_id)
    return rows(f"""
        SELECT li.id, li.quantity, li.returned_qty, li.key_number, COALESCE(kt.name, li.custom_name) AS type_name,
               l.created_at AS loan_date, l.signature AS loan_signature,
               (SELECT r.created_at FROM return_items ri JOIN returns r ON r.id = ri.return_id
                WHERE ri.loan_item_id = li.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS return_date,
               (SELECT r.signature FROM return_items ri JOIN returns r ON r.id = ri.return_id
                WHERE ri.loan_item_id = li.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS return_signature
        FROM loan_items li
        JOIN loans l ON l.id = li.loan_id
        LEFT JOIN key_types kt ON kt.id = li.key_type_id
        WHERE {where}
        ORDER BY l.created_at, l.id, type_name COLLATE NOCASE, li.key_number""", (args,))


# --- Storico -------------------------------------------------------------------

def history(q="", date_from="", date_to="", kind="tutti", type_id=None, limit=500):
    """Prestiti e riconsegne in ordine cronologico inverso, con filtri."""
    like = f"%{q}%"
    events = []

    if kind in ("tutti", "prestiti"):
        where, args = [], []
        if q:
            where.append("""(l.name LIKE ? OR (l.last_name || ' ' || l.first_name) LIKE ? OR l.email LIKE ?
                OR l.phone LIKE ? OR l.operator LIKE ? OR l.notes LIKE ?
                OR EXISTS (SELECT 1 FROM loan_items li LEFT JOIN key_types kt ON kt.id = li.key_type_id
                           WHERE li.loan_id = l.id AND (COALESCE(kt.name, li.custom_name) LIKE ? OR li.key_number LIKE ?)))""")
            args += [like] * 8
        if date_from:
            where.append("date(l.created_at) >= ?")
            args.append(date_from)
        if date_to:
            where.append("date(l.created_at) <= ?")
            args.append(date_to)
        if type_id:
            where.append("EXISTS (SELECT 1 FROM loan_items li WHERE li.loan_id = l.id AND li.key_type_id = ?)")
            args.append(type_id)
        found = rows(f"""SELECT l.id, l.borrower_id, l.name, l.email, l.phone, l.operator, l.notes, l.created_at
                         FROM loans l {"WHERE " + " AND ".join(where) if where else ""}
                         ORDER BY l.created_at DESC, l.id DESC LIMIT ?""", args + [limit + 1])
        items = loan_items([r["id"] for r in found])
        for r in found:
            r.update(kind="prestito", items=items.get(r["id"], []))
            r["summary"] = describe_items(r["items"])
            events.append(r)

    if kind in ("tutti", "riconsegne"):
        where, args = [], []
        if q:
            where.append("""(b.name LIKE ? OR (b.last_name || ' ' || b.first_name) LIKE ? OR b.email LIKE ?
                OR b.phone LIKE ? OR r.operator LIKE ? OR r.notes LIKE ?
                OR EXISTS (SELECT 1 FROM return_items ri JOIN loan_items li ON li.id = ri.loan_item_id
                           LEFT JOIN key_types kt ON kt.id = li.key_type_id
                           WHERE ri.return_id = r.id AND (COALESCE(kt.name, li.custom_name) LIKE ? OR li.key_number LIKE ?)))""")
            args += [like] * 8
        if date_from:
            where.append("date(r.created_at) >= ?")
            args.append(date_from)
        if date_to:
            where.append("date(r.created_at) <= ?")
            args.append(date_to)
        if type_id:
            where.append("""EXISTS (SELECT 1 FROM return_items ri JOIN loan_items li ON li.id = ri.loan_item_id
                            WHERE ri.return_id = r.id AND li.key_type_id = ?)""")
            args.append(type_id)
        found = rows(f"""SELECT r.id FROM returns r JOIN borrowers b ON b.id = r.borrower_id
                         {"WHERE " + " AND ".join(where) if where else ""}
                         ORDER BY r.created_at DESC, r.id DESC LIMIT ?""", args + [limit + 1])
        for r in returns(return_ids=[x["id"] for x in found]):
            b = borrower(r["borrower_id"])
            events.append({"kind": "riconsegna", "id": r["id"], "borrower_id": r["borrower_id"],
                           "name": r["borrower_name"], "email": b["email"], "phone": b["phone"],
                           "operator": r["operator"], "notes": r["notes"], "created_at": r["created_at"],
                           "items": r["items"], "summary": r["summary"]})

    events.sort(key=lambda e: (e["created_at"], e["id"]), reverse=True)
    return events[:limit], len(events) > limit
