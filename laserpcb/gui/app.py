"""Point d'entrée de la GUI : ``python -m laserpcb.gui.app``."""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from .main_window import MainWindow

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
