"""Database tables and the only helpers allowed to touch per-user data.

Every table except `users` and `invites` has a `user_id` column. Code that
reads or writes per-user rows must go through the helpers at the bottom of
this file, which take the current user's id and filter on it.
"""

import os
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PendingLogin(Base):
    __tablename__ = "pending_logins"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    state: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tries_left: Mapped[int] = mapped_column(Integer, default=3)


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    secret: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    __tablename__ = "profile"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    likes: Mapped[str] = mapped_column(Text, default="")
    dislikes: Mapped[str] = mapped_column(Text, default="")
    dietary_notes: Mapped[str] = mapped_column(Text, default="")
    goals: Mapped[str] = mapped_column(Text, default="")


class TrainingWeek(Base):
    __tablename__ = "training_week"
    __table_args__ = (UniqueConstraint("user_id", "week_start"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    week_start: Mapped[datetime] = mapped_column(Date)
    sessions: Mapped[list] = mapped_column(JSON, default=list)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable so admin actions on a deleted user can still be recorded.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean)


# Every table that holds a user's own rows. delete-user clears all of them.
USER_TABLES = [WebSession, PendingLogin, Credential, Profile, TrainingWeek, AuditLog]


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///nutrition.db")


@lru_cache
def _sessionmaker(url: str) -> sessionmaker:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def open_session() -> Session:
    """A database session for one request. Holds no user identity."""
    return _sessionmaker(database_url())()


# --- Per-user helpers ------------------------------------------------------


def get_active_user(db: Session, user_id: int) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.active else None


def find_user_by_key_hash(db: Session, key_hash: str) -> User | None:
    return db.scalar(select(User).where(User.key_hash == key_hash, User.active.is_(True)))


def record_audit(db: Session, user_id: int | None, action: str, ok: bool) -> None:
    db.add(AuditLog(user_id=user_id, action=action, ok=ok))
    db.commit()


PROFILE_FIELDS = ("likes", "dislikes", "dietary_notes", "goals")


def get_profile(db: Session, user_id: int) -> dict:
    profile = db.get(Profile, user_id)
    return {f: getattr(profile, f) if profile else "" for f in PROFILE_FIELDS}


def update_profile(db: Session, user_id: int, changes: dict) -> dict:
    """Set only the fields given; leave the rest as they were."""
    profile = db.get(Profile, user_id)
    if profile is None:
        profile = Profile(user_id=user_id, **{f: "" for f in PROFILE_FIELDS})
        db.add(profile)
    for field, value in changes.items():
        if field in PROFILE_FIELDS and value is not None:
            setattr(profile, field, value)
    db.commit()
    return get_profile(db, user_id)


def save_training_week(db: Session, user_id: int, week_start, sessions: list) -> None:
    week = db.scalar(
        select(TrainingWeek).where(
            TrainingWeek.user_id == user_id, TrainingWeek.week_start == week_start
        )
    )
    if week is None:
        db.add(TrainingWeek(user_id=user_id, week_start=week_start, sessions=sessions))
    else:
        week.sessions = sessions
    db.commit()


def get_training_week(db: Session, user_id: int, week_start=None) -> dict | None:
    """The week starting on week_start, or the latest saved week if None."""
    query = select(TrainingWeek).where(TrainingWeek.user_id == user_id)
    if week_start is not None:
        query = query.where(TrainingWeek.week_start == week_start)
    week = db.scalar(query.order_by(TrainingWeek.week_start.desc()).limit(1))
    if week is None:
        return None
    return {"week_start": week.week_start.isoformat(), "sessions": week.sessions}


def get_credential(db: Session, user_id: int, source: str) -> dict | None:
    """This user's saved connection to a source, unsealed. Only ever called
    inside a request for that same user."""
    from nutrition_server.crypto import unseal

    row = db.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.source == source)
    )
    return unseal(row.secret) if row else None


def save_credential(db: Session, user_id: int, source: str, state: dict) -> None:
    from nutrition_server.crypto import seal

    row = db.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.source == source)
    )
    if row is None:
        db.add(Credential(user_id=user_id, source=source, secret=seal(state)))
    else:
        row.secret = seal(state)
        row.updated_at = utcnow()
    db.commit()


def delete_user_data(db: Session, user_id: int) -> None:
    """Remove every row belonging to this user, then the user."""
    for table in USER_TABLES:
        db.execute(delete(table).where(table.user_id == user_id))
    db.execute(delete(User).where(User.id == user_id))
    db.commit()
