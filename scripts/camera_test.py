"""Verifie la camera de tete sans ouvrir la GUI.

    python scripts/camera_test.py                 # detection mDNS
    python scripts/camera_test.py lasercam.local  # cible imposee
    python scripts/camera_test.py 192.168.1.60 --frames 10 --out output/cam.jpg

Elle lit ``/status``, prend une image, puis compte les images du flux pendant
quelques secondes : c'est le test de recette du montage, et il dit tout de
suite si le probleme est le reseau, le firmware, ou la camera elle-meme.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import camera  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Test de la camera de tete")
    ap.add_argument("host", nargs="?", help="nom ou IP (sinon : detection mDNS)")
    ap.add_argument("--frames", type=int, default=20,
                    help="nombre d'images a lire dans le flux (defaut 20)")
    ap.add_argument("--out", default="output/camera.jpg",
                    help="ou ecrire l'image de /capture")
    ap.add_argument("--laser", action="store_true",
                    help="allume la croix laser 5 s au passage")
    args = ap.parse_args()

    host = args.host
    if not host:
        print("Detection mDNS…")
        found = camera.discover()
        if not found:
            print("Aucune camera trouvee. Verifiez le WiFi de la carte, ou "
                  "donnez l'IP en argument.")
            return 1
        for f in found:
            print(f"  trouvee : {f.label()}")
        host = found[0].ip

    cam = camera.CameraClient(host)

    print(f"\n/status sur {host}")
    try:
        st = cam.status()
    except Exception as e:  # noqa: BLE001
        print(f"  INJOIGNABLE : {e}")
        return 1
    for key in ("fw", "version", "host", "ip", "rssi", "psram",
                "framesize", "quality", "laser", "led", "uptime_s"):
        if key in st:
            print(f"  {key:10} {st[key]}")
    if not st.get("psram"):
        print("  ATTENTION : PSRAM absente — flux limite au QVGA.")
    rssi = st.get("rssi")
    if isinstance(rssi, int) and rssi < -75:
        print(f"  ATTENTION : signal faible ({rssi} dBm) — le flux va hacher.")

    print("\n/capture")
    try:
        img = cam.snapshot()
    except Exception as e:  # noqa: BLE001
        print(f"  ECHEC : {e}")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(img)
    print(f"  {len(img)} octets -> {out}")

    if args.laser:
        print("\n/laser : croix allumee 5 s")
        cam.laser(True, hold_ms=5000)
        time.sleep(5)
        cam.laser(False)

    print(f"\n/stream : lecture de {args.frames} images")
    t0 = time.monotonic()
    n = total = 0
    try:
        for frame in cam.stream():
            n += 1
            total += len(frame)
            if n >= args.frames:
                break
    except Exception as e:  # noqa: BLE001
        print(f"  ECHEC apres {n} images : {e}")
        return 1
    dt = time.monotonic() - t0
    if n and dt > 0:
        print(f"  {n} images en {dt:.1f} s -> {n / dt:.1f} img/s, "
              f"{total / dt / 1024:.0f} ko/s ({total // n} octets/image)")
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
