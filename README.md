# Sistema de Análisis Diario BTTS (TheStatsAPI)

## Requisitos
- Python 3.11+
- API key de TheStatsAPI en la variable de entorno `THESTATSAPI_API_KEY`

## Instalación y ejecución
```bash
export THESTATSAPI_API_KEY="tu_api_key"
pip install -r requirements.txt
python main.py              # hoy (UTC)
python main.py --date 2026-10-22
```

## Estructura
| Archivo | Descripción |
|---|---|
| `thestatsapi_client.py` | Cliente HTTP: rate limit 900 req/min, retries (1s/2s/4s), backoff en 429 con Retry-After, timeout 30s, caché en memoria 5 min, logging |
| `btts_analyzer.py` | Lógica BTTS: forma por condición, H2H, alineaciones/bajas, cuotas, predicción |
| `daily_report.py` | Orquestación del pipeline + renderizado rich + reporte JSON |
| `main.py` | Punto de entrada |

## Salida
- Terminal (rich): Parte 1 partidos, Parte 2 análisis, Parte 3 verificación, Parte 4 resumen.
- JSON: `reportes/YYYY-MM-DD.json`

## Reglas de datos
- Nada de datos inventados: todo proviene de llamadas reales a la API.
- Si un dato no existe, se muestra "N/D" / "No disponible".
- Notas de la referencia oficial (llms.txt):
  - No hay endpoint H2H propio: el H2H se deriva del historial de un equipo filtrando por el ID del rival.
  - La posición solo distingue G/D/M/F: portero o delantero ausente => ALTO impacto; defensor => MEDIO.
  - xG solo existe para partidos jugados; las cuotas BTTS son strings `last_seen`.
  - Endpoints opcionales documentados que no se usan: `/football/teams/{id}/injuries-suspensions`,
    `/football/teams/{id}/top-scorers`, `/football/matches/{id}/events`. Pueden añadirse sin romper nada.
