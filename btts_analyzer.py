"""
btts_analyzer.py
----------------
Lógica de análisis BTTS (Both Teams To Score / Ambos Equipos Marcan).

REGLA ABSOLUTA: ningún dato se inventa. Todo proviene de llamadas reales a
TheStatsAPI. Si un dato no está disponible, el valor es None y la salida
muestra "N/D".
"""

from __future__ import annotations

from typing import Literal, Optional

import requests
from pydantic import BaseModel

from thestatsapi_client import TheStatsAPIClient, TheStatsAPIError

FORM_LIMIT = 10          # últimos N partidos por condición (local/visitante)
H2H_LIMIT = 10           # últimos N enfrentamientos directos

Venue = Literal["home", "away"]
Impact = Literal["ALTO", "MEDIO", "BAJO"]
# La API solo expone posición G/D/M/F: no distingue central de lateral,
# ni mediocentro defensivo de mediapunta. Por eso:
#  - Portero (G) o delantero (F) ausente  -> ALTO IMPACTO
#  - Defensor (D) ausente                 -> MEDIO IMPACTO
#  - Mediocampista (M) ausente            -> BAJO IMPACTO
POSITION_IMPACT: dict[str, Impact] = {"G": "ALTO", "F": "ALTO", "D": "MEDIO", "M": "BAJO"}


# ==========================================================================
# Modelos de datos (pydantic)
# ==========================================================================
class FormMatch(BaseModel):
    match_id: str
    date: str
    opponent: str
    venue: Venue
    goals_for: int
    goals_against: int
    scored: bool
    conceded: bool
    btts: bool


class TeamForm(BaseModel):
    team_id: str
    team_name: str
    venue: Venue
    sample_size: int
    scored_pct: Optional[float] = None       # % partidos donde marcó
    conceded_pct: Optional[float] = None     # % partidos donde encajó
    btts_pct: Optional[float] = None         # % BTTS Yes
    avg_goals_for: Optional[float] = None
    avg_goals_against: Optional[float] = None
    matches: list[FormMatch] = []


class H2HMatch(BaseModel):
    match_id: str
    date: str
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    btts: bool


class H2HStats(BaseModel):
    sample_size: int
    btts_pct: Optional[float] = None
    avg_total_goals: Optional[float] = None
    matches: list[H2HMatch] = []


class PlayerAbsence(BaseModel):
    player_id: str
    name: str
    position: Optional[str] = None
    impact: Impact


class TeamLineupInfo(BaseModel):
    team_id: str
    team_name: str
    lineup_type: Optional[str] = None        # predicted / team_sheet / played
    confirmed: bool = False
    formation: Optional[str] = None
    goalkeeper: Optional[str] = None
    defenders: list[str] = []
    forwards: list[str] = []
    absent_players: list[PlayerAbsence] = []
    note: Optional[str] = None               # p.ej. "Alineación no publicada aún"


class BttsOddsRow(BaseModel):
    bookmaker: str
    yes: Optional[str] = None                # cuota BTTS Yes (string, como la API)
    no: Optional[str] = None                 # cuota BTTS No


class BttsPrediction(BaseModel):
    prediction: Literal["BTTS YES", "BTTS NO"]
    confidence: Literal["Alta", "Media", "Baja"]
    justification: list[str] = []            # máximo 3 líneas, con datos reales
    risk_factor: Optional[str] = None


class MatchAnalysis(BaseModel):
    match_id: str
    competition_id: str
    competition_name: Optional[str] = None
    utc_date: Optional[str] = None
    status: Optional[str] = None
    home_team_id: str
    home_team_name: str
    away_team_id: str
    away_team_name: str
    home_form: Optional[TeamForm] = None
    away_form: Optional[TeamForm] = None
    h2h: Optional[H2HStats] = None
    home_lineup: Optional[TeamLineupInfo] = None
    away_lineup: Optional[TeamLineupInfo] = None
    btts_odds: list[BttsOddsRow] = []
    xg: Optional[dict] = None                # {"home": float, "away": float} si hay xG
    prediction: Optional[BttsPrediction] = None
    errors: list[str] = []                   # fallos parciales (el pipeline continúa)


# ==========================================================================
# A) / B) Forma reciente por condición (local / visitante)
# ==========================================================================
def compute_team_form(
    team_matches: list[dict],
    team_id: str,
    team_name: str,
    venue: Venue,
    limit: int = FORM_LIMIT,
) -> TeamForm:
    """Calcula la forma reciente de un equipo SOLO en la condición indicada.

    `team_matches` debe ser el historial del equipo (solo status=finished).
    """
    selected: list[dict] = []
    for m in team_matches:
        if m.get("status") != "finished" or not m.get("score"):
            continue
        if venue == "home" and m.get("home_team", {}).get("id") != team_id:
            continue
        if venue == "away" and m.get("away_team", {}).get("id") != team_id:
            continue
        selected.append(m)

    selected.sort(key=lambda m: m.get("utc_date", ""), reverse=True)
    selected = selected[:limit]

    rows: list[FormMatch] = []
    for m in selected:
        is_home = m["home_team"]["id"] == team_id
        gf = m["score"].get("home" if is_home else "away")
        ga = m["score"].get("away" if is_home else "home")
        if gf is None or ga is None:
            continue
        opponent = m["away_team" if is_home else "home_team"].get("name", "?")
        rows.append(FormMatch(
            match_id=m["id"],
            date=m.get("utc_date", ""),
            opponent=opponent,
            venue="home" if is_home else "away",
            goals_for=int(gf),
            goals_against=int(ga),
            scored=gf > 0,
            conceded=ga > 0,
            btts=gf > 0 and ga > 0,
        ))

    n = len(rows)
    if n == 0:
        return TeamForm(team_id=team_id, team_name=team_name, venue=venue, sample_size=0)

    pct = lambda k: round(100.0 * sum(1 for r in rows if k(r)) / n, 1)
    return TeamForm(
        team_id=team_id,
        team_name=team_name,
        venue=venue,
        sample_size=n,
        scored_pct=pct(lambda r: r.scored),
        conceded_pct=pct(lambda r: r.conceded),
        btts_pct=pct(lambda r: r.btts),
        avg_goals_for=round(sum(r.goals_for for r in rows) / n, 2),
        avg_goals_against=round(sum(r.goals_against for r in rows) / n, 2),
        matches=rows,
    )


# ==========================================================================
# C) Head-to-head (derivado del historial, sin endpoint propio)
# ==========================================================================
def compute_h2h(
    team_matches: list[dict],
    home_id: str,
    away_id: str,
    limit: int = H2H_LIMIT,
) -> H2HStats:
    """Filtra el historial de un equipo quedándose con los enfrentamientos
    entre home_id y away_id."""
    games = [
        m for m in team_matches
        if m.get("status") == "finished"
        and m.get("score")
        and {m.get("home_team", {}).get("id"), m.get("away_team", {}).get("id")} == {home_id, away_id}
    ]
    games.sort(key=lambda m: m.get("utc_date", ""), reverse=True)
    games = games[:limit]

    rows: list[H2HMatch] = []
    for m in games:
        hg = m["score"].get("home")
        ag = m["score"].get("away")
        if hg is None or ag is None:
            continue
        rows.append(H2HMatch(
            match_id=m["id"],
            date=m.get("utc_date", ""),
            home_team=m.get("home_team", {}).get("name", "?"),
            away_team=m.get("away_team", {}).get("name", "?"),
            home_goals=int(hg),
            away_goals=int(ag),
            btts=hg > 0 and ag > 0,
        ))

    n = len(rows)
    if n == 0:
        return H2HStats(sample_size=0)
    return H2HStats(
        sample_size=n,
        btts_pct=round(100.0 * sum(1 for r in rows if r.btts) / n, 1),
        avg_total_goals=round(sum(r.home_goals + r.away_goals for r in rows) / n, 2),
        matches=rows,
    )


# ==========================================================================
# D) Alineaciones y bajas
# ==========================================================================
def analyze_team_lineup(
    client: TheStatsAPIClient,
    match_id: str,
    team_id: str,
    team_name: str,
) -> TeamLineupInfo:
    """Obtiene la alineación del equipo en `match_id` y cruza con la plantilla
    para detectar ausencias (jugadores en plantilla que no están ni en el
    once ni en el banquillo)."""
    try:
        data = client.get_match_lineups(match_id)
    except TheStatsAPIError as exc:
        if exc.status_code == 404:
            # No hay datos de alineación para este partido todavía
            return TeamLineupInfo(
                team_id=team_id, team_name=team_name,
                note="Alineación no publicada aún",
            )
        raise

    side = "home" if (data.get("home") or {}).get("id") == team_id else "away"
    team_lu = data.get(side) or {}
    starting = team_lu.get("starting_xi", []) or []
    bench = team_lu.get("substitutes", []) or []

    goalkeeper = next((p["name"] for p in starting if p.get("position") == "G"), None)
    defenders = [p["name"] for p in starting if p.get("position") == "D"]
    forwards = [p["name"] for p in starting if p.get("position") == "F"]

    absent: list[PlayerAbsence] = []
    note = None
    try:
        squad = client.get_team_players(team_id)
        xi_ids = {p.get("id") for p in starting}
        bench_ids = {p.get("id") for p in bench}
        for p in squad:
            pid = p.get("id")
            if pid and pid not in xi_ids and pid not in bench_ids:
                pos = p.get("position")
                absent.append(PlayerAbsence(
                    player_id=pid,
                    name=p.get("name", "?"),
                    position=pos,
                    impact=POSITION_IMPACT.get(pos or "", "BAJO"),
                ))
    except (TheStatsAPIError, requests.RequestException):
        note = "Plantilla no disponible; no se puede inferir bajas"

    lineup_type = data.get("type")
    confirmed = bool(data.get("confirmed"))
    if lineup_type == "predicted":
        note = (note + " | " if note else "") + "Alineación PROVISIONAL (predicted XI)"
    elif not confirmed:
        note = (note + " | " if note else "") + "Alineación no confirmada aún"

    return TeamLineupInfo(
        team_id=team_id,
        team_name=team_name,
        lineup_type=lineup_type,
        confirmed=confirmed,
        formation=team_lu.get("formation"),
        goalkeeper=goalkeeper,
        defenders=defenders,
        forwards=forwards,
        absent_players=absent,
        note=note,
    )


# ==========================================================================
# E) Cuotas BTTS
# ==========================================================================
def extract_btts_odds(odds_payload: dict) -> list[BttsOddsRow]:
    """Extrae cuota BTTS Yes/No por bookmaker del payload de /odds."""
    rows: list[BttsOddsRow] = []
    for bk in odds_payload.get("bookmakers", []) or []:
        btts = (bk.get("markets") or {}).get("btts")
        if not btts:
            continue
        yes = (btts.get("yes") or {}).get("last_seen")
        no = (btts.get("no") or {}).get("last_seen")
        rows.append(BttsOddsRow(bookmaker=bk.get("bookmaker", "?"), yes=yes, no=no))
    return rows


# ==========================================================================
# F) Predicción BTTS (solo con datos reales)
# ==========================================================================
def predict_btts(
    home_form: Optional[TeamForm],
    away_form: Optional[TeamForm],
    h2h: Optional[H2HStats],
    home_lineup: Optional[TeamLineupInfo],
    away_lineup: Optional[TeamLineupInfo],
) -> Optional[BttsPrediction]:
    """Emite la predicción BTTS. Devuelve None si no hay datos suficientes
    (nunca se inventa una predicción sin base real)."""
    if (
        home_form is None or home_form.sample_size == 0
        or away_form is None or away_form.sample_size == 0
    ):
        return None

    # Estimación heurística: probabilidad de que cada equipo marcar,
    # promediando "marca X" con "el rival encaja".
    p = (
        home_form.scored_pct + away_form.conceded_pct
        + away_form.scored_pct + home_form.conceded_pct
    ) / 4.0
    if h2h is not None and h2h.sample_size >= 3 and h2h.btts_pct is not None:
        p = 0.8 * p + 0.2 * h2h.btts_pct

    prediction = "BTTS YES" if p >= 50.0 else "BTTS NO"
    margin = abs(p - 50.0)
    coverage = min(home_form.sample_size, away_form.sample_size)
    if margin >= 20 and coverage >= 6:
        confidence = "Alta"
    elif margin >= 10 and coverage >= 4:
        confidence = "Media"
    else:
        confidence = "Baja"

    # Justificación: máximo 3 líneas, solo con números reales
    lines: list[str] = []
    hf, af = home_form, away_form
    lines.append(
        f"Local en casa: marcó {sum(1 for m in hf.matches if m.scored)}/{hf.sample_size} "
        f"y encajó {sum(1 for m in hf.matches if m.conceded)}/{hf.sample_size}."
    )
    lines.append(
        f"Visitante fuera: marcó {sum(1 for m in af.matches if m.scored)}/{af.sample_size} "
        f"y encajó {sum(1 for m in af.matches if m.conceded)}/{af.sample_size}."
    )
    if h2h is not None and h2h.sample_size > 0 and h2h.btts_pct is not None:
        lines.append(
            f"H2H: BTTS en {sum(1 for m in h2h.matches if m.btts)}/{h2h.sample_size} "
            f"enfrentamientos."
        )
    lines = lines[:3]

    # Factor de riesgo: bajas de alto/medio impacto y alineación sin confirmar
    risks: list[str] = []
    for info, label in ((home_lineup, "Local"), (away_lineup, "Visitante")):
        if info is None:
            continue
        if info.note and "no publicada" in info.note.lower():
            risks.append(f"{label}: alineación no publicada aún")
        alto = [a.name for a in info.absent_players if a.impact == "ALTO"]
        medio = [a.name for a in info.absent_players if a.impact == "MEDIO"]
        if alto:
            risks.append(f"{label}: baja de ALTO impacto -> {', '.join(alto)}")
        if medio:
            risks.append(f"{label}: baja de medio impacto -> {', '.join(medio)}")
    risk_factor = "; ".join(risks) if risks else None

    return BttsPrediction(
        prediction=prediction,
        confidence=confidence,
        justification=lines,
        risk_factor=risk_factor,
    )


# ==========================================================================
# Pipeline completo por partido
# ==========================================================================
def analyze_match(
    client: TheStatsAPIClient,
    match: dict,
    competition_name: Optional[str] = None,
) -> MatchAnalysis:
    """Ejecuta el pipeline A–F para un partido. Cada módulo aísla sus errores:
    si un endpoint falla tras los reintentos, se registra y se continúa."""
    home = match.get("home_team") or {}
    away = match.get("away_team") or {}
    home_id, away_id = home.get("id", ""), away.get("id", "")
    match_id = match.get("id", "")
    errors: list[str] = []

    analysis = MatchAnalysis(
        match_id=match_id,
        competition_id=match.get("competition_id", ""),
        competition_name=competition_name,
        utc_date=match.get("utc_date"),
        status=match.get("status"),
        home_team_id=home_id,
        home_team_name=home.get("name", "?"),
        away_team_id=away_id,
        away_team_name=away.get("name", "?"),
    )

    # -- A) + B) + C): un solo historial por equipo (caché evita duplicados) --
    home_history: list[dict] = []
    away_history: list[dict] = []
    try:
        home_history = client.get_matches(team_id=home_id, status="finished", per_page=100)
    except TheStatsAPIError as exc:
        errors.append(f"forma local: {exc}")
    try:
        away_history = client.get_matches(team_id=away_id, status="finished", per_page=100)
    except TheStatsAPIError as exc:
        errors.append(f"forma visitante: {exc}")

    if home_history:
        analysis.home_form = compute_team_form(home_history, home_id, home.get("name", "?"), "home")
        analysis.h2h = compute_h2h(home_history, home_id, away_id)
    if away_history:
        analysis.away_form = compute_team_form(away_history, away_id, away.get("name", "?"), "away")

    # -- D) Alineaciones y bajas --
    for team_id, team_name, attr in (
        (home_id, home.get("name", "?"), "home_lineup"),
        (away_id, away.get("name", "?"), "away_lineup"),
    ):
        try:
            setattr(analysis, attr, analyze_team_lineup(client, match_id, team_id, team_name))
        except TheStatsAPIError as exc:
            errors.append(f"alineación {team_name}: {exc}")

    # -- E) Cuotas BTTS (solo si el partido indica odds_available) --
    if match.get("odds_available"):
        try:
            analysis.btts_odds = extract_btts_odds(client.get_match_odds(match_id))
        except TheStatsAPIError as exc:
            errors.append(f"cuotas: {exc}")

    # -- xG del partido (solo si está jugado/terminado y xg_available) --
    if match.get("status") == "finished" and match.get("xg_available"):
        try:
            stats = client.get_match_stats(match_id)
            xg = (stats.get("overview") or {}).get("expected_goals") or {}
            all_xg = xg.get("all") or {}
            if all_xg.get("home") is not None and all_xg.get("away") is not None:
                analysis.xg = {"home": all_xg["home"], "away": all_xg["away"]}
        except TheStatsAPIError as exc:
            errors.append(f"stats/xG: {exc}")

    # -- F) Predicción --
    analysis.prediction = predict_btts(
        analysis.home_form, analysis.away_form, analysis.h2h,
        analysis.home_lineup, analysis.away_lineup,
    )
    analysis.errors = errors
    return analysis
