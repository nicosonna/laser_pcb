"""Point d'entrée de la GUI : ``python -m laserpcb.gui.app``.

Lancée par ``pythonw`` (sans console), l'application n'a nulle part où écrire ses
erreurs : un plantage disparaît sans laisser de trace. On journalise donc tout
dans ``laser_pcb.log``, y compris les plantages de l'interpréteur lui-même
(``faulthandler``), et une exception non rattrapée est AFFICHÉE au lieu de tuer
la fenêtre silencieusement.
"""

from __future__ import annotations

import faulthandler
import sys
import traceback
from datetime import datetime
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / "laser_pcb.log"
_FAULT_FILE = None          # garde le descripteur vivant pour faulthandler


def _write(text: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
    except Exception:  # noqa: BLE001
        pass            # journaliser ne doit jamais empêcher l'app de tourner


def _excepthook(kind, value, tb) -> None:
    """Une exception non rattrapée ne doit pas faire disparaître la fenêtre."""
    text = "".join(traceback.format_exception(kind, value, tb))
    _write("EXCEPTION NON RATTRAPEE\n" + text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance():
            QMessageBox.critical(
                None, "Erreur interne",
                f"{kind.__name__} : {value}\n\n"
                f"Détail écrit dans :\n{LOG}")
    except Exception:  # noqa: BLE001
        pass


def _qt_message(mode, ctx, msg) -> None:
    """Journalise les messages de Qt, y compris les FATALS.

    Qt les ecrit sur ``stderr`` ; redirige vers un fichier, ce flux est tamponne
    et un ``qFatal()`` tue le process avant la vidange — le message se perd.
    En les ecrivant nous-memes (avec fermeture du fichier a chaque ligne), une
    mort brutale laisse enfin sa cause par ecrit.
    """
    from PySide6.QtCore import QtMsgType
    names = {QtMsgType.QtDebugMsg: "debug", QtMsgType.QtInfoMsg: "info",
             QtMsgType.QtWarningMsg: "ATTENTION", QtMsgType.QtCriticalMsg: "CRITIQUE",
             QtMsgType.QtFatalMsg: "FATAL"}
    level = names.get(mode, str(mode))
    if level in ("debug", "info"):
        return                       # bruit : on ne garde que ce qui compte
    where = ""
    if getattr(ctx, "file", None):
        where = f" [{ctx.file}:{ctx.line}]"
    _write(f"Qt {level} : {msg}{where}")


def main() -> int:
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    from .main_window import MainWindow

    _write("--- demarrage ---")
    try:
        # Fichier BINAIRE non tamponne : un crash natif tue le process sans vider
        # les tampons, un fichier texte perdrait la trace (c'est ce qui s'est passe).
        global _FAULT_FILE
        _FAULT_FILE = open(LOG.with_name("laser_pcb_crash.log"), "ab", buffering=0)
        faulthandler.enable(_FAULT_FILE, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    sys.excepthook = _excepthook
    qInstallMessageHandler(_qt_message)

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    code = app.exec()
    _write(f"--- arret normal (code {code}) ---")
    return code


if __name__ == "__main__":
    sys.exit(main())
