"""Copia locale degli utenti di dominio, usata per i suggerimenti nel prestito.

Gli utenti sono tanti: si scaricano tutti con una ricerca LDAP paginata (nessun limite di
1000 risultati) e si salvano in SQLite, dove la ricerca mentre si scrive è immediata.
Si usano givenName (nome) e sn (cognome), non il nome utente che spesso è un codice.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

import config
import db
from auth import AuthError, open_connection

log = logging.getLogger(__name__)
_lock = threading.Lock()

ATTRIBUTES = ["objectGUID", "sAMAccountName", "givenName", "sn", "mail", "telephoneNumber", "mobile"]
# Persone con account abilitato (bit 2 di userAccountControl = disabilitato).
USER_FILTER = "(&(objectCategory=person)(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"


def _first(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip() if value is not None else ""


def fetch(conn):
    """Utenti attivi del dominio con nome o cognome valorizzati."""
    from ldap3 import SUBTREE
    flt = f"(&{USER_FILTER}{config.DIRECTORY_FILTER})" if config.DIRECTORY_FILTER else USER_FILTER
    users = []
    for item in conn.extend.standard.paged_search(config.DIRECTORY_BASE_DN, flt, search_scope=SUBTREE,
                                                  attributes=ATTRIBUTES, paged_size=500, generator=True):
        if item.get("type") != "searchResEntry":
            continue
        a = item.get("attributes", {})
        first, last = _first(a.get("givenName")), _first(a.get("sn"))
        raw = item.get("raw_attributes", {}).get("objectGUID") or [b""]
        guid = str(uuid.UUID(bytes_le=raw[0])) if len(raw[0]) == 16 else _first(a.get("objectGUID")).strip("{}").lower()
        if not guid or not (first or last):      # account tecnici senza nome e cognome
            continue
        users.append({"guid": guid, "username": _first(a.get("sAMAccountName")), "first_name": first,
                      "last_name": last, "email": _first(a.get("mail")),
                      "phone": _first(a.get("telephoneNumber")) or _first(a.get("mobile"))})
    if conn.result and conn.result.get("result") not in (0, None):
        raise RuntimeError(f"ricerca LDAP interrotta: {conn.result.get('description')} {conn.result.get('message')}")
    return users


def store(users):
    """Aggiorna la tabella locale; chi non è più nel dominio viene segnato come non attivo."""
    if not users:
        # Una ricerca vuota è quasi sempre un errore (base DN o filtro sbagliati): non si disattiva nessuno.
        raise RuntimeError("nessun utente trovato: controllare GC_DIRECTORY_BASE_DN e GC_DIRECTORY_FILTER")
    stamp = db.now()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("""
            INSERT INTO directory_users (guid, username, first_name, last_name, email, phone, active, synced_at)
            VALUES (:guid, :username, :first_name, :last_name, :email, :phone, 1, :stamp)
            ON CONFLICT (guid) DO UPDATE SET username = excluded.username, first_name = excluded.first_name,
                last_name = excluded.last_name, email = excluded.email, phone = excluded.phone,
                active = 1, synced_at = excluded.synced_at""", [dict(u, stamp=stamp) for u in users])
        conn.execute("UPDATE directory_users SET active = 0 WHERE synced_at < ?", (stamp,))
        # Chi ha lasciato il dominio e non ha mai avuto chiavi non viene conservato.
        conn.execute("""DELETE FROM directory_users WHERE active = 0
                        AND guid NOT IN (SELECT ad_guid FROM borrowers WHERE ad_guid IS NOT NULL)""")
        # Gli utenti del prestito collegati al dominio seguono nome e cognome di Active Directory.
        conn.execute("""
            UPDATE borrowers SET first_name = d.first_name, last_name = d.last_name,
                   name = trim(d.first_name || ' ' || d.last_name),
                   email = CASE WHEN borrowers.email = '' THEN d.email ELSE borrowers.email END,
                   phone = CASE WHEN borrowers.phone = '' THEN d.phone ELSE borrowers.phone END
            FROM directory_users d WHERE d.guid = borrowers.ad_guid AND d.active = 1""")
        for key, value in (("directory_synced_at", stamp), ("directory_count", str(len(users))),
                           ("directory_error", "")):
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _set_error(message):
    conn = db.connect()
    try:
        conn.execute("INSERT INTO meta (key, value) VALUES ('directory_error', ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (f"{db.now()} · {message}",))
    finally:
        conn.close()


def sync(bind_user, password):
    """Sincronizzazione completa. Ritorna il numero di utenti, None se ne è già in corso una."""
    if not _lock.acquire(blocking=False):
        return None
    ldap = None
    started = time.monotonic()
    try:
        ldap = open_connection(bind_user, password)
        users = fetch(ldap)
        store(users)
        log.info("Utenti di dominio sincronizzati: %s in %.1f s", len(users), time.monotonic() - started)
        return len(users)
    except (AuthError, Exception) as exc:   # noqa: BLE001 - qualsiasi errore va registrato e mostrato
        log.exception("Sincronizzazione utenti di dominio non riuscita")
        _set_error(str(exc))
        return 0
    finally:
        if ldap is not None:
            try:
                ldap.unbind()
            except Exception:
                pass
        _lock.release()


def status():
    conn = db.connect()
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'directory_%'")}
        active = conn.execute("SELECT COUNT(*) FROM directory_users WHERE active = 1").fetchone()[0]
    finally:
        conn.close()
    return {"synced_at": meta.get("directory_synced_at"), "count": active,
            "error": meta.get("directory_error") or "", "running": _lock.locked(),
            "service_account": bool(config.LDAP_BIND_USER), "enabled": enabled()}


def enabled():
    return config.DIRECTORY_SYNC and not config.DEV_USERS


def is_stale():
    synced = status()["synced_at"]
    if not synced:
        return True
    return datetime.now() - datetime.fromisoformat(synced) > timedelta(hours=config.DIRECTORY_SYNC_HOURS)


def _start(bind_user, password):
    threading.Thread(target=sync, args=(bind_user, password), name="directory-sync", daemon=True).start()


def sync_now_async():
    """Avvio manuale (pulsante): possibile solo con l'account di servizio."""
    if enabled() and config.LDAP_BIND_USER and not _lock.locked():
        _start(config.LDAP_BIND_USER, config.LDAP_BIND_PASSWORD)
        return True
    return False


def after_login(username, password):
    """Al login di un operatore: se i dati sono vecchi si aggiornano in background.
    Senza account di servizio si usano le credenziali dell'operatore, solo per questa sincronizzazione."""
    if not enabled() or _lock.locked() or not is_stale():
        return
    if config.LDAP_BIND_USER:
        _start(config.LDAP_BIND_USER, config.LDAP_BIND_PASSWORD)
    else:
        _start(f"{username}@{config.LDAP_DOMAIN}", password)


def start_scheduler():
    """Con l'account di servizio: sincronizza all'avvio e poi ogni GC_DIRECTORY_SYNC_HOURS."""
    if not enabled() or not config.LDAP_BIND_USER:
        return

    def loop():
        while True:
            try:
                if is_stale():
                    sync(config.LDAP_BIND_USER, config.LDAP_BIND_PASSWORD)
            except Exception:
                log.exception("Errore nel ciclo di sincronizzazione")
            time.sleep(15 * 60)

    threading.Thread(target=loop, name="directory-scheduler", daemon=True).start()
