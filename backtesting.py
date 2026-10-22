"""
backtesting.py
--------------
Backtesting del modelo BTTS sobre partidos YA TERMINADOS.

Para cada partido finished del período elegido (por defecto los últimos 7
días), el modelo se reconstruye SOLO con información disponible ANTES del
partido (forma e H2H filtrados por fecha), se emite la predicción y se
compara con el resultado real (marcador a 90', campo `regulation` si existe).

REGLA ABSOLUTA: ningún dato inventado. Todo proviene de llamadas reales a
TheStatsAPI. Sin datos previos suficientes -> el partido se excluye de las
métricas (no se predice nada).

Uso:
    python backtesting.py                 # últimos 7 días
    python backtesting.py --days 14       # últimos 14 días
    python backtesting.py --days 7 --competition comp_id   # una competición
    python backtesting.py --days 30 --max 200              # limitar partidos

Salida: tablas rich + JSON en reportes/backtest_YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table

from btts_analyzer import (
    MatchAnalysis,
    compute_h2h,
    compute_team_form,
    predict_btts,
)
from thestatsapi_client import TheStatsAPIClient, TheStatsAPIError

logger = logging.getLogger("backtesting")
console = Console()

REPORT_DIR = "reportes"
DEFAULT_DAYS = 7


# ==========================================================================
# Modelos
# ==========================================================================
class BacktestMatch(BaseModel):
    match_id: str
    date: str
    competition_id: str = ""
    competition_name: Optional[str] = None
    home: str
    away: str
    score: str                          # marcador a 90'
    real_btts: str                      # "BTTS YES" / "BTTS NO"
    prediction: str                     # "BTTS YES" / "BTTS NO"
    confidence: str                     # Alta / Media / Baja
    hit: bool
    justification: list[str] = []


class BacktestReport(BaseModel):
    generated_at: str
    days: int
    period_from: str
    period_to: str
    competition_filter: Optional[str] = None
    matches: list[BacktestMatch] = []
    # Agregados
    total_finished: int = 0             # partidos finished encontrados en el período
    predicted: int = 0                  # con predicción emitida
    excluded_no_data: int = 0           # sin datos previos suficientes
    hits: int = 0
    misses: int = 0
    hit_pct: Optional[float] = None
    # Desglose por tipo de predicción
    pred_yes: int = 0
    pred_yes_hits: int = 0
    pred_no: int = 0
    pred_no_hits: int = 0
    # Desglose por confianza
    by_confidence: dict = {}
    # Desglose por competición
    by_competition: dict = {}


# ==========================================================================
# Lógica principal
# ==========================================================================
def regulation_score(match: dict) -> tuple[Optional[int], Optional[int]]:
    """Marcador a 90 minutos: regulation si existe, si no home/away."""
    score = match.get("score") or {}
    reg = score.get("regulation") or {}
    if reg.get("home") is not None and reg.get("away") is not None:
        return int(reg["home"]), int(reg["away"])
    if score.get("home") is not None and score.get("away") is not None:
        return int(score["home"]), int(score["away"])
    return None, None


def history_before(history: list[dict], before_date: str) -> list[dict]:
    """Filtra el historial de un equipo dejando SOLO partidos anteriores
    a `before_date` (backtesting sin fuga de información del futuro)."""
    return [m for m in history if (m.get("utc_date") or "") < before_date]


def backtest_match(
    match: dict,
    home_history: list[dict],
    away_history: list[dict],
    competition_name: Optional[str],
) -> Optional[BacktestMatch]:
    """Reconstruye la predicción de un partido finished usando solo datos
    anteriores a su fecha y la compara con el resultado real."""
    hg, ag = regulation_score(match)
    if hg is None or ag is None:
        return None

    home = match.get("home_team") or {}
    away = match.get("away_team") or {}
    home_id, away_id = home.get("id", ""), away.get("id", "")
    match_date = match.get("utc_date", "")

    # Punto en el tiempo: solo lo que se conocía ANTES del partido
    hist_home = history_before(home_history, match_date)
    hist_away = history_before(away_history, match_date)

    home_form = compute_team_form(hist_home, home_id, home.get("name", "?"), "home")
    away_form = compute_team_form(hist_away, away_id, away.get("name", "?"), "away")
    h2h = compute_h2h(hist_home, home_id, away_id)

    pred = predict_btts(home_form, away_form, h2h, None, None)
    if pred is None:
        return None

    real = "BTTS YES" if hg > 0 and ag > 0 else "BTTS NO"
    return BacktestMatch(
        match_id=match.get("id", ""),
        date=match_date,
        competition_id=match.get("competition_id", ""),
        competition_name=competition_name,
        home=home.get("name", "?"),
        away=away.get("name", "?"),
        score=f"{hg} - {ag}",
        real_btts=real,
        prediction=pred.prediction,
        confidence=pred.confidence,
        hit=pred.prediction == real,
        justification=pred.justification,
    )


def run_backtest(
    client: TheStatsAPIClient,
    days: int = DEFAULT_DAYS,
    competition_id: Optional[str] = None,
    max_matches: Optional[int] = None,
    comp_names: Optional[dict[str, str]] = None,
) -> BacktestReport:
    """Ejecuta el backtesting sobre los partidos finished de los últimos `days` días."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=days)).isoformat()
    date_to = today.isoformat()

    console.print(f"[bold]Backtesting BTTS: partidos finished del {date_from} al {date_to}[/bold]")

    # Nombres de competición (opcional)
    if comp_names is None:
        comp_names = {}
        try:
            comp_names = {c["id"]: c["name"] for c in client.get_competitions()}
        except TheStatsAPIError as exc:
            logger.error("No se pudieron obtener las competiciones: %s", exc)

    # Partidos finished del período (todas las competiciones salvo filtro)
    params: dict = {
        "date_from": date_from,
        "date_to": date_to,
        "status": "finished",
    }
    finished = client.get_matches(**params)

    report = BacktestReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        days=days,
        period_from=date_from,
        period_to=date_to,
        competition_filter=competition_id,
    )
    report.total_finished = len(finished)

    # Historiales por equipo (caché del cliente evita llamadas repetidas;
    # un mismo equipo puede jugar varios partidos en el período)
    team_history: dict[str, list[dict]] = {}

    def get_history(team_id: str) -> list[dict]:
        if team_id not in team_history:
            try:
                team_history[team_id] = client.get_matches(
                    team_id=team_id, status="finished", per_page=100
                )
            except TheStatsAPIError as exc:
                logger.error("Historial de %s no disponible: %s", team_id, exc)
                team_history[team_id] = []
        return team_history[team_id]

    done = 0
    for m in finished:
        if competition_id and m.get("competition_id") != competition_id:
            continue
        if max_matches is not None and done >= max_matches:
            break
        home_id = (m.get("home_team") or {}).get("id", "")
        away_id = (m.get("away_team") or {}).get("id", "")
        try:
            result = backtest_match(
                m,
                get_history(home_id),
                get_history(away_id),
                comp_names.get(m.get("competition_id", "")),
            )
        except Exception as exc:  # nunca abortar el backtest completo
            logger.exception("Error en backtest del partido %s: %s", m.get("id"), exc)
            continue
        done += 1
        if result is None:
            report.excluded_no_data += 1
            continue
        report.matches.append(result)

    _aggregate(report)
    return report


def _aggregate(report: BacktestReport) -> None:
    """Calcula los agregados del backtest."""
    report.predicted = len(report.matches)
    report.hits = sum(1 for m in report.matches if m.hit)
    report.misses = report.predicted - report.hits
    if report.predicted:
        report.hit_pct = round(100.0 * report.hits / report.predicted, 1)

    by_conf: dict[str, dict] = {}
    by_comp: dict[str, dict] = {}
    for m in report.matches:
        # Por tipo de predicción
        if m.prediction == "BTTS YES":
            report.pred_yes += 1
            report.pred_yes_hits += int(m.hit)
        else:
            report.pred_no += 1
            report.pred_no_hits += int(m.hit)
        # Por confianza
        c = by_conf.setdefault(m.confidence, {"n": 0, "hits": 0})
        c["n"] += 1
        c["hits"] += int(m.hit)
        # Por competición
        comp = m.competition_name or m.competition_id or "Desconocida"
        k = by_comp.setdefault(comp, {"n": 0, "hits": 0})
        k["n"] += 1
        k["hits"] += int(m.hit)

    for bucket in (by_conf, by_comp):
        for v in bucket.values():
            v["pct"] = round(100.0 * v["hits"] / v["n"], 1) if v["n"] else None
    report.by_confidence = by_conf
    report.by_competition = by_comp


# ==========================================================================
# Renderizado y persistencia
# ==========================================================================
_HIT_STYLE = {True: "green", False: "red"}


def render_backtest(report: BacktestReport) -> None:
    # Resumen general
    t = Table(title="Resumen del backtesting", box=box.SIMPLE, title_style="bold")
    t.add_column("Métrica")
    t.add_column("Valor", justify="right")
    t.add_row("Período", f"{report.period_from} → {report.period_to}")
    t.add_row("Partidos finished en el período", str(report.total_finished))
    t.add_row("Backtestados", str(report.predicted + report.excluded_no_data))
    t.add_row("Sin datos previos suficientes (excluidos)", str(report.excluded_no_data))
    t.add_row("Con predicción", str(report.predicted))
    t.add_row("Aciertos", f"[green]{report.hits}[/green]")
    t.add_row("Fallos", f"[red]{report.misses}[/red]")
    t.add_row("% acierto global", "N/D" if report.hit_pct is None else f"[bold]{report.hit_pct:.1f}%[/bold]")
    t.add_row("BTTS YES predichos", f"{report.pred_yes} ({report.pred_yes_hits} aciertos, "
              + (f"{100.0*report.pred_yes_hits/report.pred_yes:.1f}%)" if report.pred_yes else "N/D)"))
    t.add_row("BTTS NO predichos", f"{report.pred_no} ({report.pred_no_hits} aciertos, "
              + (f"{100.0*report.pred_no_hits/report.pred_no:.1f}%)" if report.pred_no else "N/D)"))
    console.print(t)

    # Por confianza
    if report.by_confidence:
        tc = Table(title="Acierto por nivel de confianza", box=box.SIMPLE, title_style="bold")
        tc.add_column("Confianza")
        tc.add_column("Partidos", justify="right")
        tc.add_column("Aciertos", justify="right")
        tc.add_column("% acierto", justify="right")
        for conf in ("Alta", "Media", "Baja"):
            v = report.by_confidence.get(conf)
            if not v:
                continue
            tc.add_row(conf, str(v["n"]), str(v["hits"]), "N/D" if v["pct"] is None else f"{v['pct']:.1f}%")
        console.print(tc)

    # Por competición (solo las que tienen >= 3 partidos)
    comps = [(k, v) for k, v in report.by_competition.items() if v["n"] >= 3]
    if comps:
        tk = Table(title="Acierto por competición (n ≥ 3)", box=box.SIMPLE, title_style="bold")
        tk.add_column("Competición")
        tk.add_column("Partidos", justify="right")
        tk.add_column("Aciertos", justify="right")
        tk.add_column("% acierto", justify="right")
        for name, v in sorted(comps, key=lambda kv: -(kv[1]["pct"] or 0)):
            tk.add_row(name, str(v["n"]), str(v["hits"]), f"{v['pct']:.1f}%")
        console.print(tk)

    # Detalle partido a partido (aciertos y fallos)
    if report.matches:
        td = Table(title="Detalle partido a partido", box=box.SIMPLE_HEAD, title_style="bold")
        td.add_column("Fecha", style="dim")
        td.add_column("Partido")
        td.add_column("Marcador", justify="center")
        td.add_column("BTTS real", justify="center")
        td.add_column("Predicción", justify="center")
        td.add_column("Conf.", justify="center")
        td.add_column("Resultado", justify="center")
        for m in sorted(report.matches, key=lambda x: x.date):
            style = _HIT_STYLE[m.hit]
            td.add_row(
                m.date[:10],
                f"{m.home} vs {m.away}",
                m.score,
                m.real_btts,
                m.prediction,
                m.confidence,
                f"[{style}]{'✅ Acertado' if m.hit else '❌ Fallado'}[/]",
            )
        console.print(td)


def save_backtest(report: BacktestReport) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    path = os.path.join(REPORT_DIR, f"backtest_{today}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
    console.print(f"[green]Backtest guardado en {path}[/green]")
    return path


# ==========================================================================
# Punto de entrada
# ==========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtesting del modelo BTTS sobre partidos finished recientes"
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Días hacia atrás a analizar (por defecto {DEFAULT_DAYS})")
    parser.add_argument("--competition", default=None,
                        help="Filtrar por competition_id (por defecto: todas)")
    parser.add_argument("--max", type=int, default=None,
                        help="Máximo de partidos a backtestar (por defecto: sin límite)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    try:
        client = TheStatsAPIClient()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = run_backtest(
        client,
        days=args.days,
        competition_id=args.competition,
        max_matches=args.max,
    )
    render_backtest(report)
    save_backtest(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
