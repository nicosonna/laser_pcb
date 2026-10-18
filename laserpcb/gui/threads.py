"""Fin de vie des ``QThread``.

Lâcher la DERNIÈRE référence Python d'un ``QThread`` qui n'est pas encore sorti
de ``run()`` fait appeler ``qFatal()`` à Qt : le process meurt sur-le-champ,
sans exception Python ni dump — d'où des « plantages » impossibles à lire dans
les journaux.

Or tous nos workers émettent leur résultat en **dernière ligne** de ``run()``.
Le slot tourne sur le fil graphique ; si celui-ci le traite avant que le worker
ait fini de sortir de ``run()``, mettre son attribut à ``None`` le détruit en
pleine course. La fenêtre est minuscule d'habitude — mais elle s'élargit dès
que le GIL est saturé, par exemple pendant la génération du G-code.

Qt lève son drapeau ``finished`` **avant** d'émettre le signal du même nom :
``isFinished()`` distingue donc exactement le cas fatal du cas sans danger.
"""
from PySide6.QtCore import QTimer

_alive: list = []
_reaper: QTimer | None = None


def retire(th) -> None:
    """Garde une référence sur ``th`` jusqu'à sa mort réelle.

    À appeler juste avant de lâcher la référence d'un worker.
    """
    global _reaper
    if th is None or th.isFinished():
        return                              # déjà sorti de run() : sans danger
    _alive.append(th)
    if _reaper is None:                     # créé depuis le fil graphique
        _reaper = QTimer()
        _reaper.setInterval(2000)
        _reaper.timeout.connect(_sweep)
        _reaper.start()


def _sweep() -> None:
    """Libère les threads réellement terminés (depuis le fil graphique)."""
    if _alive:
        _alive[:] = [t for t in _alive if not t.isFinished()]


def pending() -> int:
    """Nombre de threads encore en sursis (diagnostic / tests)."""
    return len(_alive)
