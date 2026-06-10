"""
database.py — Initialisation de SQLAlchemy et gestion des sessions DB.

Fournit :
  - `engine`      : Moteur SQLAlchemy connecté à PostgreSQL
  - `SessionLocal` : Fabrique de sessions
  - `get_db()`    : Dépendance FastAPI (Depends) qui ouvre/ferme la session automatiquement
  - `init_db()`   : Crée toutes les tables au démarrage (si elles n'existent pas)
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from contextlib import contextmanager
from typing import Generator

from config import settings

# ──────────────────────────────────────────────
# Moteur SQLAlchemy
# ──────────────────────────────────────────────
# pool_pre_ping=True : vérifie que la connexion est vivante avant chaque requête
# (évite les erreurs après une déconnexion PostgreSQL prolongée)
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,       # Connexions permanentes dans le pool
    max_overflow=10,   # Connexions supplémentaires autorisées en pic
    echo=False,        # Passez à True pour logger toutes les requêtes SQL (debug)
)

# ──────────────────────────────────────────────
# Fabrique de sessions
# ──────────────────────────────────────────────
SessionLocal = sessionmaker(
    autocommit=False,  # Les transactions doivent être validées explicitement
    autoflush=False,
    bind=engine,
)

# Base déclarative pour les modèles ORM
Base = declarative_base()


# ──────────────────────────────────────────────
# Dépendance FastAPI — injectée via Depends(get_db)
# ──────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """
    Générateur de session de base de données.

    Utilisation dans un endpoint FastAPI :
        @router.get("/example")
        def my_endpoint(db: Session = Depends(get_db)):
            ...

    La session est automatiquement fermée à la fin de la requête HTTP,
    même en cas d'exception.
    """
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """
    Version context manager pour utilisation en dehors de FastAPI
    (ex: tâches de fond, scripts de migration).

    Utilisation :
        with get_db_context() as db:
            db.add(...)
            db.commit()
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """
    Crée toutes les tables définies dans les modèles SQLAlchemy
    si elles n'existent pas encore.

    Appelée au démarrage de l'application dans le lifespan FastAPI.
    """
    # L'import des modèles est nécessaire pour que Base.metadata les connaisse
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)