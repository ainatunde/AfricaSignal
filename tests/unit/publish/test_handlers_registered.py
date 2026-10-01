from africasignal.jobs import handlers


def test_load_all_registers_the_email_job_kinds() -> None:
    handlers.load_all()
    for kind in ("dispatch_outbox", "notify_followers", "weekly_digest"):
        assert handlers.get_handler(kind) is not None


def test_load_all_registers_the_retention_job_kinds() -> None:
    handlers.load_all()
    for kind in ("apply_retention", "mirror_deletions"):
        assert handlers.get_handler(kind) is not None
