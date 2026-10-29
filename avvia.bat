@echo off
rem Avvio Gestione Chiavi. Al primo avvio crea l'ambiente Python e installa le dipendenze.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creazione ambiente Python...
    py -3 -m venv .venv || python -m venv .venv || goto :errore
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :errore
)

rem --- Configurazione (decommentare/modificare se serve) ---
rem set GC_PORT=8080
rem set GC_LDAP_SSL=1
rem set GC_LDAP_CA_FILE=C:\percorso\ca-cpt.pem
rem set GC_SECURE_COOKIE=1

".venv\Scripts\python.exe" app.py
goto :eof

:errore
echo Errore durante l'installazione. Serve Python 3.10 o superiore.
pause
