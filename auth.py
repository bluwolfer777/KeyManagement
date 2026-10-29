"""Autenticazione operatori su Active Directory (LDAP).

L'utente fa il bind con le proprie credenziali (utente@cpt.local), poi si verifica
l'appartenenza ai gruppi con la regola LDAP_MATCHING_RULE_IN_CHAIN
(1.2.840.113556.1.4.1941), che risolve anche i gruppi annidati:
utente -> gruppo A -> gruppo B -> Gestione_Chiavi_Write funziona.
"""
import logging
import ssl

import config

log = logging.getLogger(__name__)

IN_CHAIN = "1.2.840.113556.1.4.1941"


class AuthError(Exception):
    pass


def normalize_username(username):
    username = (username or "").strip()
    if "\\" in username:            # CPT\mario
        username = username.split("\\", 1)[1]
    if "@" in username:             # mario@cpt.local
        username = username.split("@", 1)[0]
    return username


def authenticate(username, password):
    """Ritorna {'username', 'display_name', 'role'} con role 'write' o 'read'."""
    username = normalize_username(username)
    if not username or not password:
        # Una password vuota in AD produce un "unauthenticated bind" che riesce: va rifiutata.
        raise AuthError("Inserisci nome utente e password.")
    if config.DEV_USERS:
        return _dev_authenticate(username, password)
    return _ldap_authenticate(username, password)


def _dev_authenticate(username, password):
    users = {}
    for item in config.DEV_USERS.split(","):
        if ":" in item:
            u, r = item.split(":", 1)
            users[u.strip().lower()] = r.strip()
    role = users.get(username.lower())
    if password != "dev" or role not in ("read", "write"):
        raise AuthError("Credenziali non valide.")
    return {"username": username, "display_name": f"{username} (test)", "role": role}


def open_connection(bind_user, password):
    """Connessione LDAP autenticata (bind riuscito) oppure AuthError.
    bind_user: UPN (utente@cpt.local) o DN completo."""
    from ldap3 import SIMPLE, Connection, Server, Tls
    from ldap3.core.exceptions import LDAPSocketOpenError

    tls = None
    if config.LDAP_USE_SSL or config.LDAP_START_TLS:
        tls = Tls(
            validate=ssl.CERT_REQUIRED if config.LDAP_VALIDATE_CERT else ssl.CERT_NONE,
            ca_certs_file=config.LDAP_CA_FILE,
        )
    server = Server(config.LDAP_SERVER, port=config.LDAP_PORT, use_ssl=config.LDAP_USE_SSL,
                    tls=tls, connect_timeout=config.LDAP_TIMEOUT)
    conn = Connection(server, user=bind_user, password=password, authentication=SIMPLE,
                      receive_timeout=config.LDAP_TIMEOUT, raise_exceptions=False)
    try:
        conn.open()
    except LDAPSocketOpenError as exc:
        log.error("Server LDAP %s:%s non raggiungibile: %s", config.LDAP_SERVER, config.LDAP_PORT, exc)
        raise AuthError("Server di autenticazione non raggiungibile.") from exc
    if config.LDAP_START_TLS and not conn.start_tls():
        raise AuthError("Impossibile avviare la connessione sicura con il server LDAP.")
    if not conn.bind():
        if conn.result.get("result") == 49:   # invalidCredentials
            raise AuthError("Credenziali non valide.")
        log.error("Bind LDAP fallito per %s: %s", bind_user, conn.result)
        raise AuthError("Accesso non riuscito (errore del server LDAP).")
    return conn


def _ldap_authenticate(username, password):
    from ldap3 import BASE, SUBTREE
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    conn = None
    try:
        conn = open_connection(f"{username}@{config.LDAP_DOMAIN}", password)
        conn.search(config.LDAP_BASE_DN,
                    f"(&(objectCategory=person)(objectClass=user)"
                    f"(sAMAccountName={escape_filter_chars(username)}))",
                    search_scope=SUBTREE, attributes=["givenName", "sn", "displayName", "sAMAccountName"])
        if not conn.entries:
            raise AuthError("Utente non trovato nella directory.")
        entry = conn.entries[0]
        user_dn = entry.entry_dn
        # Nome e cognome reali: il nome utente per molti account è un codice.
        full = " ".join(str(v) for v in (entry.givenName.value, entry.sn.value) if v)
        display_name = full or str(entry.displayName.value or entry.sAMAccountName.value or username)

        role = None
        for role_name, group_cn in (("write", config.GROUP_WRITE), ("read", config.GROUP_READ)):
            conn.search(config.LDAP_BASE_DN,
                        f"(&(objectClass=group)(cn={escape_filter_chars(group_cn)}))",
                        search_scope=SUBTREE, attributes=["cn"])
            if not conn.entries:
                log.warning("Gruppo %s non trovato in %s", group_cn, config.LDAP_BASE_DN)
                continue
            group_dn = conn.entries[0].entry_dn
            # Ricerca sul solo oggetto utente: torna l'utente se è membro (anche indiretto).
            conn.search(user_dn, f"(memberOf:{IN_CHAIN}:={escape_filter_chars(group_dn)})",
                        search_scope=BASE, attributes=["cn"])
            if conn.entries:
                role = role_name
                break
        if role is None:
            raise AuthError("Non sei autorizzato: serve l'appartenenza a "
                            f"{config.GROUP_READ} o {config.GROUP_WRITE}.")
        return {"username": str(entry.sAMAccountName.value or username),
                "display_name": display_name, "role": role}
    except LDAPException as exc:
        log.exception("Errore LDAP")
        raise AuthError("Errore di comunicazione con il server LDAP.") from exc
    finally:
        if conn is not None:
            try:
                conn.unbind()
            except Exception:
                pass
