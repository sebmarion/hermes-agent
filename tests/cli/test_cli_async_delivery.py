from cli import _settle_legacy_async_delivery


def test_legacy_async_settlement_retries_transient_failure():
    calls = []

    def complete(event, claim):
        calls.append((event, claim))
        return len(calls) == 3

    released = []
    requeued = []
    assert _settle_legacy_async_delivery(
        {"delegation_id": "deleg-retry"},
        "claim-retry",
        complete=complete,
        release=lambda event, claim: released.append((event, claim)),
        requeue=requeued.append,
        sleep=lambda _seconds: None,
    )
    assert len(calls) == 3
    assert released == []
    assert requeued == []


def test_legacy_async_settlement_releases_and_requeues_after_exhaustion():
    calls = []

    def complete(event, claim):
        calls.append((event, claim))
        raise OSError("database unavailable")

    released = []
    requeued = []
    assert not _settle_legacy_async_delivery(
        {"delegation_id": "deleg-release"},
        "claim-release",
        complete=complete,
        release=lambda event, claim: released.append((event, claim)) or True,
        requeue=requeued.append,
        max_attempts=2,
        sleep=lambda _seconds: None,
    )
    assert len(calls) == 2
    assert released == [({"delegation_id": "deleg-release"}, "claim-release")]
    assert requeued == [{"delegation_id": "deleg-release"}]
