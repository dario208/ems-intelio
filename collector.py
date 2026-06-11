"""
collector.py — Collecteur de fallback pour InteliNeo 5500.

En mode normal : Telegraf fait le polling Modbus → InfluxDB (1 s).
En mode dégradé : ce module prend le relais → Modbus → PostgreSQL (1 s).

Démarré / arrêté dynamiquement via POST /api/v1/admin/fallback depuis main.py.

Utilisation :
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_collector(stop_event))
    ...
    stop_event.set()   # arrêt propre
    await task
"""

import asyncio
import logging

from database import SessionLocal
from modbus_client import read_all_sources
from crud import save_genset_record, log_alarm

logger = logging.getLogger(__name__)

# Intervalle de polling en secondes (même cadence que Telegraf en mode normal)
POLL_INTERVAL = 1.0


async def run_collector(stop_event: asyncio.Event) -> None:
    """
    Boucle de collecte asynchrone qui tourne jusqu'à ce que stop_event soit déclenché.

    - Lit toutes les sources via Modbus TCP (read_all_sources)
    - Persiste en PostgreSQL via save_genset_record
    - En cas d'erreur Modbus, logue une alarme et continue (pas d'arrêt brutal)
    - En cas d'erreur DB, logue l'erreur et continue

    Le stop_event est testé à chaque itération pour permettre un arrêt propre
    sans attendre la fin du timeout Modbus.
    """
    logger.warning("Collecteur fallback démarré — polling PostgreSQL toutes les %.1f s", POLL_INTERVAL)

    consecutive_errors = 0

    while not stop_event.is_set():
        loop_start = asyncio.get_event_loop().time()

        try:
            # ── Lecture Modbus ────────────────────────────────────────
            data = await read_all_sources()
            consecutive_errors = 0  # réinitialise le compteur d'erreurs

            # ── Persistance PostgreSQL ────────────────────────────────
            db = SessionLocal()
            try:
                save_genset_record(data, db)
            except Exception as db_exc:
                logger.error("Collecteur fallback — erreur DB : %s", db_exc)
            finally:
                db.close()

        except ConnectionError as conn_exc:
            consecutive_errors += 1
            logger.error(
                "Collecteur fallback — Modbus injoignable (#%d) : %s",
                consecutive_errors, conn_exc,
            )
            # Logguer une alarme critique au bout de 3 échecs consécutifs
            if consecutive_errors == 3:
                db = SessionLocal()
                try:
                    log_alarm(
                        db,
                        severity="critical",
                        code="MODBUS_UNREACHABLE",
                        message=str(conn_exc),
                        source="system",
                    )
                except Exception:
                    pass
                finally:
                    db.close()

        except Exception as exc:
            consecutive_errors += 1
            logger.exception("Collecteur fallback — erreur inattendue : %s", exc)

        # ── Attente jusqu'à la prochaine itération ────────────────────
        elapsed = asyncio.get_event_loop().time() - loop_start
        sleep_time = max(0.0, POLL_INTERVAL - elapsed)
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.ensure_future(_wait_for_event(stop_event))),
                timeout=sleep_time,
            )
            # stop_event déclenché pendant le sleep → sortie propre
            break
        except asyncio.TimeoutError:
            # Timeout normal → on continue la boucle
            pass

    logger.warning("Collecteur fallback arrêté proprement.")


async def _wait_for_event(event: asyncio.Event) -> None:
    """Attend indéfiniment que l'event soit déclenché."""
    await event.wait()
