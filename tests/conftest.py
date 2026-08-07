"""Fixtures partagées : chemins des cartes d'exemple et géométries synthétiques."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from shapely import box

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

GERBER = REPO / "examples" / "module_relais"
ODBPP = REPO / "examples" / "solar_pi_odb"


@pytest.fixture(scope="session")
def gerber_dir() -> Path:
    return GERBER / "GerberFiles"


@pytest.fixture(scope="session")
def drill_file() -> Path:
    return GERBER / "DrillFiles" / "drill_1_16.xln"


@pytest.fixture(scope="session")
def odb_root() -> Path:
    return ODBPP


@pytest.fixture
def two_pads():
    """Deux carrés cuivre séparés par un interstice de 0.4 mm (nets distincts)."""
    a = box(0, 0, 1, 1)
    b = box(1.4, 0, 2.4, 1)
    board = box(-1, -1, 3.4, 2)
    return a, b, board
