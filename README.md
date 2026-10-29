# Gestione Chiavi

Applicazione web per il prestito delle chiavi del CPT Trevano. Si usa dal browser
di PC e tablet (interfaccia touch, firma con dito o penna). Gli operatori accedono
con l'account di dominio (Active Directory).

- [Funzioni](#funzioni)
- [Accesso e permessi](#accesso-e-permessi-active-directory)
- [**Guida al deploy**](#guida-al-deploy)
- [Configurazione](#configurazione)
- [Note legali e privacy](#note-legali-e-privacy)
- [Sviluppo e test](#sviluppo-e-test)

## Funzioni

- **Due tipi di chiave**; ogni utente può averne al massimo una per tipo.
  - *Tutte uguali*: si indica la quantità totale; al prestito si spunta la casella.
  - *Numerate*: si indica la quantità totale; il numero si scrive al momento del
    prestito. Un numero già fuori viene rifiutato.
- **Prestito**: cognome e nome (obbligatori), email e telefono facoltativi, firma
  obbligatoria. Gli utenti già registrati vengono suggeriti mentre si scrive.
- **Chiave non in elenco**: nel prestito si può scrivere al momento il nome di una
  chiave non censita; vale solo per quel prestito e non viene aggiunta all'elenco.
- **Riconsegna** per utente: una, alcune o tutte le chiavi (anche di prestiti
  diversi), con firma obbligatoria.
- **Ricevuta PDF** dal modello Excel `modelli/ricevuta_chiavi.xlsx`: "Ricevuta PDF"
  nella pagina del prestito e "Scheda PDF" (tutte le chiavi dell'utente) nella
  pagina dell'utente. Una riga per chiave con data, firma di ricevuta, firma e
  data di ritorno; oltre 14 righe si aggiungono pagine.
- **Storico** con ricerca libera, filtro per movimento, chiave e periodo
  (scorciatoie Oggi / 7 giorni / 30 giorni / mese / anno), esportazione CSV.
- **Utenti** ordinabili su ogni colonna (predefinito: cognome) con ricerca.
- **Modalità scura**: pulsante in basso a destra (automatico come il PC → scuro →
  chiaro). La scelta viene ricordata in un cookie se l'utente accetta i cookie.
- Piè di pagina con copyright, informativa sulla privacy e banner dei cookie.

## Accesso e permessi (Active Directory)

L'operatore entra con le credenziali di dominio (`mario`, `CPT\mario` o
`mario@cpt.local`). L'app fa il bind sul server LDAP e poi controlla i gruppi:

| Gruppo | Permessi |
|---|---|
| `Gestione_Chiavi_Write` | tutto: prestiti, riconsegne, anagrafica chiavi e utenti |
| `Gestione_Chiavi_Read_Only` | sola consultazione |

L'appartenenza **indiretta** è supportata (utente → gruppo → gruppo → … →
`Gestione_Chiavi_Write`) tramite la regola AD `LDAP_MATCHING_RULE_IN_CHAIN`
(OID 1.2.840.113556.1.4.1941). Se un utente è in entrambi i gruppi vale Write.
I permessi vengono letti al login: dopo una modifica dei gruppi l'operatore deve
uscire e rientrare.

## Guida al deploy

### 1. Requisiti

- Un server (Linux o Windows) con **Docker** e **Docker Compose**.
- Il server deve **risolvere `cpt.local`** e raggiungere un domain controller
  sulla porta **389** (LDAP) oppure **636** (LDAPS). Verifica:
  ```bash
  docker run --rm busybox nslookup cpt.local
  ```
- Una porta libera per l'app (predefinita **8080**) aperta sul firewall verso i
  PC e i tablet che la useranno.

### 2. Avvio con l'immagine pubblicata

L'immagine è pubblicata su GitHub Container Registry:
`ghcr.io/bluwolfer777/keymanagement:latest`.

Se il pacchetto non è pubblico, prima di scaricarlo accedi al registry con un
token GitHub che abbia il permesso `read:packages`:

```bash
docker login ghcr.io -u bluwolfer777
```

Crea una cartella (es. `/opt/gestione-chiavi` o `C:\gestione-chiavi`) con questo
`docker-compose.yml`:

```yaml
services:
  gestione-chiavi:
    image: ghcr.io/bluwolfer777/keymanagement:latest
    container_name: gestione-chiavi
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - gestione-chiavi-data:/data
    environment:
      GC_LDAP_SERVER: cpt.local
      TZ: Europe/Zurich
      # GC_LDAP_SSL: "1"                 # LDAPS, vedi punto 4
      # GC_LDAP_CA_FILE: /data/ca-cpt.pem
      # GC_SECURE_COOKIE: "1"            # quando è servita in HTTPS, vedi punto 5

volumes:
  gestione-chiavi-data:
```

Avvia e controlla i log:

```bash
docker compose up -d
```

```bash
docker compose logs -f
```

Apri `http://NOME-SERVER:8080` ed entra con un account membro di uno dei due
gruppi. Al primo avvio il database viene creato vuoto nel volume
`gestione-chiavi-data`; da **Chiavi → + Nuova chiave** si censiscono le chiavi.

### 3. Compilare l'immagine in locale (alternativa)

Dalla cartella del repository:

```bash
docker compose up -d --build
```

Il `docker-compose.yml` del repository ha sia `image:` sia `build:`, quindi
funziona in entrambi i modi.

### 4. LDAPS (consigliato)

Con LDAP sulla 389 la password dell'operatore viaggia in chiaro sulla rete, e i
domain controller recenti possono rifiutare il bind non cifrato. Se i DC hanno un
certificato (Servizi certificati Active Directory):

1. esporta il certificato della CA interna in formato PEM (Base-64) e copialo nel
   volume, ad esempio:
   ```bash
   docker cp ca-cpt.pem gestione-chiavi:/data/ca-cpt.pem
   ```
2. nel `docker-compose.yml` imposta `GC_LDAP_SSL: "1"` e
   `GC_LDAP_CA_FILE: /data/ca-cpt.pem`;
3. `docker compose up -d` per applicare.

### 5. HTTPS

Le firme e i dati personali passano dal browser al server: in rete è meglio
servire l'app in HTTPS con un reverse proxy davanti al container. Esempio con
**Caddy** e un certificato interno:

```
chiavi.cpt.local {
    tls /certs/chiavi.crt /certs/chiavi.key
    reverse_proxy gestione-chiavi:8080
}
```

In alternativa IIS con URL Rewrite + ARR. Con HTTPS attivo imposta
`GC_SECURE_COOKIE: "1"` e non pubblicare più la porta 8080 all'esterno.

### 6. Backup e ripristino

Tutto (anagrafiche, prestiti, firme) è nel file SQLite `/data/chiavi.db` del
volume. Copia coerente anche a container acceso:

```bash
docker exec gestione-chiavi python -c "import sqlite3; s=sqlite3.connect('/data/chiavi.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
```

```bash
docker cp gestione-chiavi:/data/backup.db ./chiavi_backup.db
```

Per ripristinare: ferma il container (`docker compose stop`), copia il file di
backup come `/data/chiavi.db` (`docker cp ./chiavi_backup.db gestione-chiavi:/data/chiavi.db`)
e riavvia (`docker compose start`). Conviene pianificare il backup ogni notte.

### 7. Aggiornamento

```bash
docker compose pull
```

```bash
docker compose up -d
```

Fai sempre un backup prima. All'avvio la nuova versione aggiorna da sola la
struttura del database: le versioni precedenti di questa app vengono convertite
automaticamente (es. esemplari numerati → quantità, nome unico → nome e cognome).

### 8. Senza Docker (server Windows)

Serve Python 3.10 o superiore. Copia il repository sul server e lancia
`avvia.bat`: al primo avvio crea l'ambiente e installa le dipendenze. Per
l'avvio automatico registralo come servizio con [NSSM](https://nssm.cc/)
(`nssm install GestioneChiavi`, programma `avvia.bat`, cartella dell'app come
directory di lavoro). Le variabili di configurazione si impostano in `avvia.bat`.

### 9. Modello della ricevuta PDF

Il PDF ridisegna `modelli/ricevuta_chiavi.xlsx` (logo, etichette, larghezze,
bordi, margini) e lo riempie. Puoi cambiare testi o logo nel file Excel mantenendo
la struttura (intestazioni "Data", "Tipo di chiave", "Firma di ricevuta", "Firma
di ritorno", "Data di ritorno" e righe unite a gruppi). Per usare un modello
diverso senza ricompilare l'immagine, montalo e indica il percorso:

```yaml
    volumes:
      - ./ricevuta_chiavi.xlsx:/app/modelli/ricevuta_chiavi.xlsx:ro
```

## Configurazione

Variabili d'ambiente (in `docker-compose.yml`, in `avvia.bat` o nel servizio):

| Variabile | Predefinito | Note |
|---|---|---|
| `GC_LDAP_SERVER` | `cpt.local` | nome del dominio o di un domain controller |
| `GC_LDAP_SSL` | `0` | `1` = LDAPS sulla porta 636 (consigliato) |
| `GC_LDAP_STARTTLS` | `0` | in alternativa: StartTLS sulla 389 |
| `GC_LDAP_CA_FILE` | | certificato della CA interna (PEM) |
| `GC_LDAP_VALIDATE_CERT` | `1` | `0` solo per prove: non verifica il certificato |
| `GC_LDAP_PORT` | `389` / `636` | |
| `GC_LDAP_DOMAIN` | `cpt.local` | suffisso UPN per il login |
| `GC_LDAP_BASE_DN` | `DC=cpt,DC=local` | |
| `GC_GROUP_READ` / `GC_GROUP_WRITE` | `Gestione_Chiavi_Read_Only` / `Gestione_Chiavi_Write` | |
| `GC_PORT` | `8080` | |
| `GC_DATA_DIR` | `/data` (Docker), `./data` | database e chiave di sessione |
| `GC_SESSION_HOURS` | `10` | durata massima della sessione operatore |
| `GC_SECURE_COOKIE` | `0` | `1` se servita in HTTPS |
| `GC_SECRET_KEY` | generata al primo avvio | chiave dei cookie di sessione |
| `GC_COPYRIGHT_OWNER` | `CPT Trevano` | nel piè di pagina e nei PDF |
| `GC_PRIVACY_CONTROLLER` | `Centro Professionale Tecnico (CPT) Lugano-Trevano` | titolare nell'informativa |
| `GC_PRIVACY_CONTACT` | `la direzione del CPT Trevano` | a chi rivolgersi per la privacy |
| `GC_PRIVACY_RETENTION_YEARS` | `5` | durata di conservazione indicata nell'informativa |
| `GC_RICEVUTA_TEMPLATE` | `modelli/ricevuta_chiavi.xlsx` | modello del PDF |

## Note legali e privacy

- L'informativa è alla pagina `/privacy` (raggiungibile anche senza login) e
  linkata nel piè di pagina e nel modulo di prestito, sopra la firma.
- Il banner dei cookie compare alla prima visita. L'app usa solo cookie tecnici
  propri: `session` (accesso, necessario), `gc_consent` (scelta sul banner) e
  `gc_theme` (tema scelto, salvato solo con "Accetta").
- **Il testo dell'informativa è un modello**: va fatto rivedere dal responsabile
  della protezione dei dati dell'istituto e adattato con le variabili
  `GC_PRIVACY_*` (titolare, contatto, durata di conservazione).
- La durata di conservazione indicata **non** è applicata automaticamente: la
  cancellazione dei dati vecchi va fatta periodicamente da chi gestisce l'app.
- I log del container contengono login riusciti e falliti con l'indirizzo IP.

## Sviluppo e test

Solo per prove senza Active Directory: con `GC_DEV_USERS=mario:write,ospite:read`
si entra con quegli utenti e password `dev`. **Mai in produzione** (all'avvio
compare un avviso nei log).

```bash
pip install -r requirements.txt
```

```bash
GC_DEV_USERS=mario:write GC_DATA_DIR=./data-dev python app.py
```

## Licenza

GPL-3.0, vedi [LICENSE](LICENSE).
