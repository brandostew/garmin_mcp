from datetime import date, timedelta

from sqlalchemy import func, select

from nutrition_server import admin, db
from nutrition_server.security import hash_secret

from .conftest import make_user


def test_create_invite_stores_only_a_hash(database):
    link = admin.create_invite("Alex")
    code = link.rsplit("/", 1)[1]
    with db.open_session() as s:
        invite = s.scalar(select(db.Invite))
        assert invite.name == "Alex"
        assert invite.code_hash == hash_secret(code)
        assert code not in invite.code_hash
        assert not invite.used
        assert invite.expires_at.date() == (db.utcnow() + timedelta(days=7)).date()


def test_disable_and_rotate(database):
    uid, key = make_user("Alex")
    assert admin.rotate_key(uid)
    with db.open_session() as s:
        assert db.find_user_by_key_hash(s, hash_secret(key)) is None
    assert admin.disable_user(uid)
    with db.open_session() as s:
        assert db.get_active_user(s, uid) is None
    assert not admin.disable_user(999)


def test_delete_user_removes_every_row(database):
    a, _ = make_user("Alex")
    b, _ = make_user("Bo")
    with db.open_session() as s:
        for uid in (a, b):
            s.add(db.Profile(user_id=uid, likes="eggs"))
            s.add(db.TrainingWeek(user_id=uid, week_start=date(2026, 11, 9), sessions=[]))
            s.add(db.Credential(user_id=uid, source="cronometer", secret=b"sealed"))
            s.add(db.WebSession(id=f"s{uid}", user_id=uid, expires_at=db.utcnow()))
            s.add(db.PendingLogin(id=f"p{uid}", user_id=uid, session_id=f"s{uid}",
                                  source="garmin", state=b"x", expires_at=db.utcnow()))
            s.add(db.AuditLog(user_id=uid, action="whoami", ok=True))
        s.commit()

    assert admin.delete_user(a)

    with db.open_session() as s:
        assert s.get(db.User, a) is None
        for table in db.USER_TABLES:
            assert s.scalar(select(func.count()).select_from(table).where(table.user_id == a)) == 0
            # Bo's data is untouched.
            assert s.scalar(select(func.count()).select_from(table).where(table.user_id == b)) >= 1


def test_cli_delete_needs_confirmation(database, capsys):
    uid, _ = make_user("Alex")
    assert admin.main(["delete-user", str(uid)]) == 1
    assert admin.main(["list-users"]) == 0
    assert "Alex" in capsys.readouterr().out
    assert admin.main(["delete-user", str(uid), "--yes"]) == 0
