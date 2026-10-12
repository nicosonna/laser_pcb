"""Dialogue Nomenclature (BOM) : tableau des composants + exports CSV / HTML."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .. import bom

_HEADERS = ["Qté", "Références", "Valeur", "Boîtier", "Dimensions", "Fabricant",
            "Réf. fabricant", "Face", "Description", "Achat"]


class BomDialog(QDialog):
    def __init__(self, lines: list[bom.BomLine], title: str, parent=None):
        super().__init__(parent)
        self.lines = lines
        self.title = title
        self.setWindowTitle(f"Nomenclature — {title}")
        self.resize(980, 560)

        root = QVBoxLayout(self)
        total = sum(l.qty for l in lines)
        root.addWidget(QLabel(f"<b>{len(lines)}</b> références — "
                              f"<b>{total}</b> composants"))

        table = QTableWidget(len(lines), len(_HEADERS))
        table.setHorizontalHeaderLabels(_HEADERS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        import webbrowser
        for r, l in enumerate(lines):
            vals = (str(l.qty), " ".join(l.refs), l.value or l.part, l.part,
                    l.size, l.manufacturer, l.mpn, l.sides, l.description)
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setTextAlignment(0x84)      # AlignHCenter | AlignVCenter
                table.setItem(r, c, item)
            btn = QPushButton("🔍 AliExpress")
            btn.setToolTip(f"Recherche : {bom.search_query(l)}")
            btn.clicked.connect(lambda _=False, url=bom.aliexpress_url(l):
                                webbrowser.open(url))
            table.setCellWidget(r, len(_HEADERS) - 1, btn)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)  # Description
        root.addWidget(table)

        btns = QHBoxLayout()
        b_csv = QPushButton("Exporter CSV…"); b_csv.clicked.connect(self._csv)
        b_html = QPushButton("Exporter HTML (joli)…"); b_html.clicked.connect(self._html)
        b_ali = QPushButton("🛒 Tout ouvrir sur AliExpress")
        b_ali.setToolTip("Ouvre un onglet de recherche par ligne du BOM.")
        b_ali.clicked.connect(self._open_all)
        b_close = QPushButton("Fermer"); b_close.clicked.connect(self.accept)
        btns.addWidget(b_csv); btns.addWidget(b_html); btns.addWidget(b_ali)
        btns.addStretch(1); btns.addWidget(b_close)
        root.addLayout(btns)

    def _open_all(self):
        import webbrowser
        from PySide6.QtWidgets import QMessageBox
        if len(self.lines) > 12:
            ok = QMessageBox.question(
                self, "AliExpress",
                f"Ouvrir {len(self.lines)} onglets de recherche ?")
            if ok != QMessageBox.Yes:
                return
        for l in self.lines:
            webbrowser.open(bom.aliexpress_url(l))

    def _csv(self):
        f, _ = QFileDialog.getSaveFileName(self, "Exporter la nomenclature (CSV)",
                                           f"{self.title}_bom.csv", "CSV (*.csv)")
        if f:
            Path(f).write_text(bom.to_csv(self.lines), encoding="utf-8-sig")

    def _html(self):
        f, _ = QFileDialog.getSaveFileName(self, "Exporter la nomenclature (HTML)",
                                           f"{self.title}_bom.html", "HTML (*.html)")
        if f:
            Path(f).write_text(bom.to_html(self.lines, self.title), encoding="utf-8")
            import webbrowser
            webbrowser.open(Path(f).as_uri())        # aperçu direct dans le navigateur
