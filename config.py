"""Configurazione dell'applicazione.

Tutti i valori possono essere sovrascritti con variabili d'ambiente (prefisso GC_)
senza modificare questo file.
"""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(name, default):
    return os.environ.get(name, default)


def _bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "si", "on")


# --- LDAP / Active Directory -------------------------------------------------
LDAP_SERVER = _env("GC_LDAP_SERVER", "cpt.local")
LDAP_USE_SSL = _bool("GC_LDAP_SSL", False)          # LDAPS (porta 636)
LDAP_START_TLS = _bool("GC_LDAP_STARTTLS", False)   # StartTLS su porta 389
LDAP_PORT = int(_env("GC_LDAP_PORT", "636" if LDAP_USE_SSL else "389"))
LDAP_VALIDATE_CERT = _bool("GC_LDAP_VALIDATE_CERT", True)
LDAP_CA_FILE = _env("GC_LDAP_CA_FILE", "") or None
LDAP_DOMAIN = _env("GC_LDAP_DOMAIN", "cpt.local")   # suffisso UPN (utente@cpt.local)
LDAP_BASE_DN = _env("GC_LDAP_BASE_DN", ",".join(f"DC={p}" for p in LDAP_DOMAIN.split(".")))
LDAP_TIMEOUT = int(_env("GC_LDAP_TIMEOUT", "8"))

GROUP_READ = _env("GC_GROUP_READ", "Gestione_Chiavi_Read_Only")
GROUP_WRITE = _env("GC_GROUP_WRITE", "Gestione_Chiavi_Write")

# --- Utenti di dominio per i suggerimenti nel prestito --------------------------
# Gli utenti attivi del dominio vengono copiati in una tabella locale (nome, cognome, email,
# telefono) e proposti mentre si scrive il nome nel prestito.
DIRECTORY_SYNC = _bool("GC_DIRECTORY_SYNC", True)
DIRECTORY_SYNC_HOURS = float(_env("GC_DIRECTORY_SYNC_HOURS", "12"))
DIRECTORY_BASE_DN = _env("GC_DIRECTORY_BASE_DN", "") or LDAP_BASE_DN
DIRECTORY_FILTER = _env("GC_DIRECTORY_FILTER", "")   # filtro LDAP aggiuntivo, es. (department=*)
# Account di servizio (facoltativo) per la sincronizzazione periodica. Senza, la sincronizzazione
# avviene in background al login di un operatore, con le sue credenziali, quando i dati sono vecchi.
LDAP_BIND_USER = _env("GC_LDAP_BIND_USER", "")       # es. svc-chiavi@cpt.local
LDAP_BIND_PASSWORD = _env("GC_LDAP_BIND_PASSWORD", "")

# --- Applicazione ------------------------------------------------------------
DATA_DIR = _env("GC_DATA_DIR", os.path.join(BASE_DIR, "data"))
DATABASE = os.path.join(DATA_DIR, "chiavi.db")
HOST = _env("GC_HOST", "0.0.0.0")
PORT = int(_env("GC_PORT", "8080"))
SESSION_HOURS = int(_env("GC_SESSION_HOURS", "10"))
# Impostare a 1 quando l'app è servita in HTTPS (consigliato)
SESSION_COOKIE_SECURE = _bool("GC_SECURE_COOKIE", False)

# Intestazioni legali (piè di pagina, informativa privacy, PDF)
COPYRIGHT_OWNER = _env("GC_COPYRIGHT_OWNER", "CPT Trevano")
PRIVACY_CONTROLLER = _env("GC_PRIVACY_CONTROLLER", "Centro Professionale Tecnico (CPT) Lugano-Trevano")
PRIVACY_CONTACT = _env("GC_PRIVACY_CONTACT", "la direzione del CPT Trevano")
# Durata di conservazione indicata nell'informativa (anni dalla chiusura del prestito)
PRIVACY_RETENTION_YEARS = int(_env("GC_PRIVACY_RETENTION_YEARS", "5"))

# SOLO PER TEST, senza Active Directory: "mario:write,ospite:read" (password: "dev").
# Lasciare vuoto in produzione.
DEV_USERS = _env("GC_DEV_USERS", "")


def get_secret_key():
    """Chiave di firma dei cookie, generata al primo avvio e salvata in data/."""
    key = os.environ.get("GC_SECRET_KEY")
    if key:
        return key
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
    with open(path, encoding="ascii") as f:
        return f.read().strip()
