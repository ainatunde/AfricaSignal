"""Exercise independent dispatch transactions against real PostgreSQL row locks."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete
from sqlalchemy.orm import sessionmaker

from africasignal.models import AppUser, LoginToken, Outbox
from africasignal.publish.email import FakeProvider
from africasignal.publish.login_tokens import hash_token
from africasignal.publish.outbox import dispatch_pending, enqueue_email


def test_concurrent_dispatch_does_not_send_prefetched_row_twice(engine: Engine) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        user = AppUser(email="concurrent@example.com")
        session.add(user)
        session.flush()
        session.add(
            LoginToken(
                user_id=user.id,
                token_sha256=hash_token("concurrent-token"),
                expires_at=now + timedelta(hours=1),
            )
        )
        for index in range(2):
            enqueue_email(
                session,
                "email_login",
                {
                    "user_id": user.id,
                    "link": "http://localhost:8000/signin/verify?token=concurrent-token",
                },
                f"concurrent:{index}",
            )
        session.commit()
    other = FakeProvider()

    class RacingProvider(FakeProvider):
        def send(self, message):
            if len(self.sent) == 1:
                with factory() as competitor:
                    assert (
                        dispatch_pending(
                            competitor, other, now + timedelta(seconds=1), limit=1
                        ).sent
                        == 0
                    )
            return super().send(message)

    provider = RacingProvider()
    try:
        with factory() as session:
            assert dispatch_pending(session, provider, now + timedelta(seconds=1)).sent == 2
        assert len(provider.sent) == 2 and other.sent == []
    finally:
        with factory() as session:
            session.execute(delete(Outbox).where(Outbox.dedupe_key.like("concurrent:%")))
            session.execute(delete(LoginToken).where(LoginToken.user_id == user.id))
            session.execute(delete(AppUser).where(AppUser.id == user.id))
            session.commit()
