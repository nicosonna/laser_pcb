@echo off
rem ============================================================
rem  laser_pcb - lanceur : double-cliquez sur ce fichier.
rem  Utilise le venv du projet (.venv) et ouvre la GUI sans console.
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Environnement Python introuvable.
    echo Creez-le une fois avec :
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

set MPLBACKEND=QtAgg
start "laser_pcb" ".venv\Scripts\pythonw.exe" -m laserpcb.gui.app
