"""
daily_report.py
---------------
Orquestación del pipeline diario de análisis BTTS.

Módulo 1: partidos del día (agrupados por competición)
Módulo 2: análisis BTTS por partido (forma, H2H, alineaciones, cuotas, predicción)
Módulo 3: verificación BTTS de partidos terminados
Módulo 4: resumen diario + estadísticas

Salida: terminal con rich + JSON en reportes/YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from btts_analyzer import MatchAnalysis, analyze_match
from thestatsapi_client import TheStatsAPIClient, TheStatsAPIError

logger = logging.getLogger("daily_report")
console = Console()

REPORT_DIR = "reportes"
STATUSES_VALIDOS = {"scheduled", "live", "finished"}


# ==========================================================================
# Modelos del reporte
# ==========================================================================
class MatchBasic(BaseModel):
    match_id: str
    utc_date: Optional[str] = None
    status: Optional[str] = None
    competition_id: str = ""
    competition_name: Optional[str] = None
    home: str = "?"
    away: str = "?"
    score: Optional[str] = None         # p.ej. "2 - 1" si está jugado/terminado


class BttsVerification(BaseModel):
    match_id: str
    match: str
    score: str
    real_btts: str                      # "BTTS YES" / "BTTS NO" / "N/D"
    prediction: Optional[str] = None    # predicción previa, si existía
    result: str                         # "Acertado" / "Fallado" / "Sin predicción" / "N/D"


class DaySummary(BaseModel):
    total_matches: int = 0
    analyzed: int = 0
    predicted_yes: int = 0
    predicted_no: int = 0
    verifiable: int = 0
    hits: int = 0
    misses: int = 0
    hit_pct: Optional[float] = None


class DailyReport(BaseModel):
    date: str
    generated_at: str
    matches_today: list[MatchBasic] = []
    analyses: list[MatchAnalysis] = []
    verifications: list[BttsVerification] = []
    summary: DaySummary = Field(default_factory=DaySummary)


# ==========================================================================
# Módulo 1 — Partidos del día
# ==========================================================================
def fetch_today_matches(client: TheStatsAPIClient) -> tuple[list[dict], dict[str, str]]:
    """Obtiene todos los partidos del día y un mapa competition_id -> nombre."""
    matches = client.get_matches(date_from=fecha_hoy(), date_to=fecha_hoy())
    comp_names: dict[str, str] = {}
    try:
        comp_names = {c["id"]: c["name"] for c in client.get_competitions()}
    except TheStatsAPIError as exc:
        logger.error("No se pudo obtener el listado de competiciones: %s", exc)
    return matches, comp_names


def fecha_hoy() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def to_basic(match: dict, comp_names: dict[str, str]) -> MatchBasic:
    home = match.get("home_team") or {}
    away = match.get("away_team") or {}
    score = match.get("score") or {}
    hg, ag = score.get("home"), score.get("away")
    return MatchBasic(
        match_id=match.get("id", ""),
        utc_date=match.get("utc_date"),
        status=match.get("status"),
        competition_id=match.get("competition_id", ""),
        competition_name=comp_names.get(match.get("competition_id", "")),
        home=home.get("name", "?"),
        away=away.get("name", "?"),
        score=f"{hg} - {ag}" if hg is not None and ag is not None else None,
    )


# ==========================================================================
# Módulo 2 — Análisis por partido
# ==========================================================================
def run_analyses(
    client: TheStatsAPIClient,
    matches: list[dict],
    comp_names: dict[str, str],
) -> list[MatchAnalysis]:
    analyses: list[MatchAnalysis] = []
    for m in matches:
        if m.get("status") not in STATUSES_VALIDOS:
            logger.info("Partido %s con status=%s: se omite del análisis",
                        m.get("id"), m.get("status"))
            continue
        try:
            console.print(f"[dim]Analizando {m.get('home_team',{}).get('name','?')} vs "
                          f"{m.get('away_team',{}).get('name','?')} ({m.get('id')})...[/dim]")
            analyses.append(analyze_match(client, m, comp_names.get(m.get("competition_id", ""))))
        except Exception as exc:  # nunca abortar el pipeline completo
            logger.exception("Error analizando el partido %s: %s", m.get("id"), exc)
            console.print(f"[red]Error en {m.get('id')}: {exc}. Se continúa con el siguiente.[/red]")
    return analyses


# ==========================================================================
# Módulo 3 — Verificación BTTS (partidos terminados)
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


def verify_finished_matches(
    matches: list[dict],
    analyses: list[MatchAnalysis],
) -> list[BttsVerification]:
    pred_by_id = {a.match_id: a.prediction for a in analyses}
    verifications: list[BttsVerification] = []
    for m in matches:
        if m.get("status") != "finished":
            continue
        home = m.get("home_team", {}).get("name", "?")
        away = m.get("away_team", {}).get("name", "?")
        hg, ag = regulation_score(m)
        if hg is None or ag is None:
            verifications.append(BttsVerification(
                match_id=m.get("id", ""), match=f"{home} vs {away}",
                score="N/D", real_btts="N/D", result="N/D",
            ))
            continue
        real = "BTTS YES" if hg > 0 and ag > 0 else "BTTS NO"
        pred = pred_by_id.get(m.get("id", ""))
        if pred is None:
            result = "Sin predicción"
        else:
            result = "Acertado" if pred.prediction == real else "Fallado"
        verifications.append(BttsVerification(
            match_id=m.get("id", ""),
            match=f"{home} vs {away}",
            score=f"{hg} - {ag}",
            real_btts=real,
            prediction=pred.prediction if pred else None,
            result=result,
        ))
    return verifications


# ==========================================================================
# Módulo 4 — Resumen diario
# ==========================================================================
def build_summary(
    matches_today: list[MatchBasic],
    analyses: list[MatchAnalysis],
    verifications: list[BttsVerification],
) -> DaySummary:
    s = DaySummary(
        total_matches=len(matches_today),
        analyzed=len(analyses),
    )
    for a in analyses:
        if a.prediction is None:
            continue
        if a.prediction.prediction == "BTTS YES":
            s.predicted_yes += 1
        else:
            s.predicted_no += 1
    for v in verifications:
        if v.real_btts == "N/D":
            continue
        s.verifiable += 1
        if v.result == "Acertado":
            s.hits += 1
        elif v.result == "Fallado":
            s.misses += 1
    decided = s.hits + s.misses
    if decided:
        s.hit_pct = round(100.0 * s.hits / decided, 1)
    return s


# ==========================================================================
# Pipeline completo
# ==========================================================================
def run(date: Optional[str] = None) -> DailyReport:
    date = date or fecha_hoy()
    client = TheStatsAPIClient()

    # Módulo 1
    console.rule(f"[bold]MÓDULO 1 — Partidos del día ({date} UTC)[/bold]")
    matches, comp_names = fetch_today_matches(client)
    matches_today = [to_basic(m, comp_names) for m in matches]
    render_part1(matches_today)

    if not matches:
        console.print("[yellow]No hay partidos hoy. Fin del pipeline.[/yellow]")
        report = DailyReport(date=date, generated_at=datetime.now(timezone.utc).isoformat())
        save_report(report)
        return report

    # Módulo 2
    console.rule("[bold]MÓDULO 2 — Análisis BTTS por partido[/bold]")
    analyses = run_analyses(client, matches, comp_names)
    for a in analyses:
        render_analysis(a)

    # Módulo 3
    console.rule("[bold]MÓDULO 3 — Verificación BTTS (partidos terminados)[/bold]")
    verifications = verify_finished_matches(matches, analyses)
    render_verifications(verifications)

    # Módulo 4
    console.rule("[bold]MÓDULO 4 — Resumen diario[/bold]")
    summary = build_summary(matches_today, analyses, verifications)
    render_summary(summary, analyses)

    report = DailyReport(
        date=date,
        generated_at=datetime.now(timezone.utc).isoformat(),
        matches_today=matches_today,
        analyses=analyses,
        verifications=verifications,
        summary=summary,
    )
    save_report(report)
    return report


def save_report(report: DailyReport) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{report.date}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
    console.print(f"[green]Reporte guardado en {path}[/green]")
    return path


# ==========================================================================
# Renderizado con rich
# ==========================================================================
_STATUS_STYLE = {
    "scheduled": "cyan",
    "live": "bold red",
    "finished": "green",
}


def render_part1(matches_today: list[MatchBasic]) -> None:
    if not matches_today:
        console.print("[yellow]No hay partidos hoy.[/yellow]")
        return
    by_comp: dict[str, list[MatchBasic]] = defaultdict(list)
    for m in matches_today:
        by_comp[m.competition_name or m.competition_id or "Desconocida"].append(m)
    for comp in sorted(by_comp):
        table = Table(title=comp, box=box.SIMPLE_HEAD, title_style="bold magenta")
        table.add_column("Hora (UTC)", style="cyan", no_wrap=True)
        table.add_column("Partido")
        table.add_column("Estado")
        for m in sorted(by_comp[comp], key=lambda x: x.utc_date or ""):
            hora = (m.utc_date or "")[11:16] if m.utc_date else "N/D"
            estado = m.status or "N/D"
            marcador = f"  [dim]({m.score})[/dim]" if m.score else ""
            table.add_row(
                hora or "N/D",
                f"{m.home} vs {m.away}{marcador}",
                f"[{_STATUS_STYLE.get(m.status, 'white')}]{estado}[/]",
            )
        console.print(table)


def _pct(value: Optional[float]) -> str:
    return "N/D" if value is None else f"{value:.1f}%"


def _form_table(form, title: str) -> Table:
    table = Table(title=title, box=box.SIMPLE, title_style="bold")
    table.add_column("Fecha", style="dim")
    table.add_column("Rival")
    table.add_column("Goles", justify="center")
    table.add_column("Marcó", justify="center")
    table.add_column("Encajó", justify="center")
    table.add_column("BTTS", justify="center")
    for r in form.matches:
        table.add_row(
            r.date[:10],
            r.opponent,
            f"{r.goals_for}-{r.goals_against}",
            "✅" if r.scored else "❌",
            "✅" if r.conceded else "❌",
            "✅" if r.btts else "❌",
        )
    table.add_section()
    table.add_row(
        "", f"[bold]{form.sample_size} partidos[/bold]",
        f"Media {form.avg_goals_for}/{form.avg_goals_against}",
        f"[bold]{_pct(form.scored_pct)}[/bold]",
        f"[bold]{_pct(form.conceded_pct)}[/bold]",
        f"[bold]{_pct(form.btts_pct)}[/bold]",
    )
    return table


def render_analysis(a: MatchAnalysis) -> None:
    title = (f"[bold]{a.home_team_name} vs {a.away_team_name}[/bold]  |  "
             f"{a.competition_name or a.competition_id}  |  {a.status or 'N/D'}")
    if a.errors:
        title += f"\n[red]Avisos: {'; '.join(a.errors)}[/red]"
    console.print(Panel(title, expand=False))

    # A) / B) Forma
    if a.home_form and a.home_form.sample_size:
        console.print(_form_table(a.home_form, f"A) Forma LOCAL de {a.home_team_name} en casa"))
    else:
        console.print(f"A) Forma local de {a.home_team_name}: [yellow]N/D[/yellow]")
    if a.away_form and a.away_form.sample_size:
        console.print(_form_table(a.away_form, f"B) Forma VISITANTE de {a.away_team_name} fuera"))
    else:
        console.print(f"B) Forma visitante de {a.away_team_name}: [yellow]N/D[/yellow]")

    # C) H2H
    if a.h2h and a.h2h.sample_size:
        h2h_tbl = Table(title="C) Head-to-head", box=box.SIMPLE, title_style="bold")
        h2h_tbl.add_column("Fecha", style="dim")
        h2h_tbl.add_column("Local")
        h2h_tbl.add_column("Resultado", justify="center")
        h2h_tbl.add_column("Visitante")
        for r in a.h2h.matches:
            h2h_tbl.add_row(r.date[:10], r.home_team, f"{r.home_goals}-{r.away_goals}", r.away_team)
        h2h_tbl.add_section()
        h2h_tbl.add_row("", "", f"[bold]BTTS {_pct(a.h2h.btts_pct)}[/bold]",
                        f"Media goles: {a.h2h.avg_total_goals}")
        console.print(h2h_tbl)
    else:
        console.print("C) Head-to-head: [yellow]N/D[/yellow]")

    # D) Alineaciones y bajas
    for info, label in ((a.home_lineup, "Local"), (a.away_lineup, "Visitante")):
        if info is None:
            console.print(f"D) Alineación {label}: [yellow]N/D[/yellow]")
            continue
        if info.lineup_type is None:
            console.print(f"D) {info.team_name}: [yellow]{info.note or 'Alineación no publicada aún'}[/yellow]")
            continue
        tipo = {"predicted": "PROVISIONAL", "team_sheet": "OFICIAL",
                "played": "JUGADA"}.get(info.lineup_type, info.lineup_type)
        txt = (f"D) {info.team_name} [{tipo}] formación {info.formation or 'N/D'} | "
               f"Portero: {info.goalkeeper or 'N/D'}")
        if info.note:
            txt += f"\n   [yellow]{info.note}[/yellow]"
        if info.absent_players:
            ausencias = "; ".join(
                f"{p.name} ({p.position or '?'}, {p.impact})" for p in info.absent_players
            )
            txt += f"\n   Ausentes (plantilla, fuera de XI y banquillo): {ausencias}"
        else:
            txt += "\n   Sin ausencias detectadas respecto a la plantilla."
        console.print(txt)

    # E) Cuotas
    if a.btts_odds:
        odds_tbl = Table(title="E) Cuotas BTTS", box=box.SIMPLE, title_style="bold")
        odds_tbl.add_column("Bookmaker")
        odds_tbl.add_column("BTTS Yes", justify="right")
        odds_tbl.add_column("BTTS No", justify="right")
        for row in a.btts_odds:
            odds_tbl.add_row(row.bookmaker, row.yes or "N/D", row.no or "N/D")
        console.print(odds_tbl)
    else:
        console.print("E) Cuotas BTTS: [yellow]N/D[/yellow]")

    # xG si existe
    if a.xg:
        console.print(f"xG del partido: local {a.xg['home']} — visitante {a.xg['away']}")

    # F) Predicción
    if a.prediction is None:
        console.print("F) Predicción: [yellow]No disponible (datos insuficientes)[/yellow]")
    else:
        conf_style = {"Alta": "bold green", "Media": "yellow", "Baja": "red"}[a.prediction.confidence]
        pred_style = "bold green" if a.prediction.prediction == "BTTS YES" else "bold red"
        console.print(f"F) Predicción: [{pred_style}]{a.prediction.prediction}[/{pred_style}] "
                      f"(confianza [{conf_style}]{a.prediction.confidence}[/{conf_style}])")
        for line in a.prediction.justification:
            console.print(f"   • {line}")
        if a.prediction.risk_factor:
            console.print(f"   [red]⚠ Riesgo: {a.prediction.risk_factor}[/red]")
    console.print()


def render_verifications(verifications: list[BttsVerification]) -> None:
    if not verifications:
        console.print("No hay partidos terminados hoy.")
        return
    table = Table(title="Verificación BTTS", box=box.SIMPLE_HEAD, title_style="bold")
    table.add_column("Partido")
    table.add_column("Marcador (90')", justify="center")
    table.add_column("BTTS real", justify="center")
    table.add_column("Predicción", justify="center")
    table.add_column("Resultado", justify="center")
    for v in verifications:
        style = {"Acertado": "green", "Fallado": "red"}.get(v.result, "white")
        table.add_row(
            v.match, v.score, v.real_btts,
            v.prediction or "—",
            f"[{style}]{v.result}[/]",
        )
    console.print(table)


def render_summary(summary: DaySummary, analyses: list[MatchAnalysis]) -> None:
    table = Table(title="Estadísticas del día", box=box.SIMPLE, title_style="bold")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    table.add_row("Partidos del día", str(summary.total_matches))
    table.add_row("Partidos analizados", str(summary.analyzed))
    table.add_row("BTTS YES predichos", str(summary.predicted_yes))
    table.add_row("BTTS NO predichos", str(summary.predicted_no))
    table.add_row("Partidos verificables", str(summary.verifiable))
    table.add_row("Aciertos", str(summary.hits))
    table.add_row("Fallos", str(summary.misses))
    table.add_row("% acierto", "N/D" if summary.hit_pct is None else f"{summary.hit_pct:.1f}%")
    console.print(table)

    con_pred = [a for a in analyses if a.prediction is not None]
    if con_pred:
        orden = {"Alta": 0, "Media": 1, "Baja": 2}
        con_pred.sort(key=lambda a: (orden[a.prediction.confidence], a.match_id))
        menor = con_pred[-1]
        mayor = con_pred[0]
        console.print(f"Mayor confianza: [bold]{mayor.home_team_name} vs {mayor.away_team_name}[/bold] "
                      f"-> {mayor.prediction.prediction} ({mayor.prediction.confidence})")
        console.print(f"Menor confianza: [bold]{menor.home_team_name} vs {menor.away_team_name}[/bold] "
                      f"-> {menor.prediction.prediction} ({menor.prediction.confidence})")
