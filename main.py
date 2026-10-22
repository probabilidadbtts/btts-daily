"""
main.py
-------
Punto de entrada del sistema de análisis diario BTTS.

Uso:
    export THESTATSAPI_API_KEY="tu_api_key"
    pip install -r requirements.txt
    python main.py            # partidos de hoy (UTC)
    python main.py --date 2026-10-22
"""

from __future__ import annotations

import argparse
import logging
import sys

from daily_report import run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Análisis diario BTTS (Both Teams To Score) con TheStatsAPI"
    )
    parser.add_argument(
        "--date", default=None,
        help="Fecha a analizar en formato YYYY-MM-DD (por defecto: hoy en UTC)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],   # la API no ensucia la salida rich
    )

    try:
        run(date=args.date)
    except RuntimeError as exc:      # p.ej. falta la API key
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
