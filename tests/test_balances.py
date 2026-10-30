import threading

import pytest
from sqlalchemy import func, select

from backend.models import BalanceActivity, User
from backend.money import AmountError, format_cents, parse_amount_to_cents
from backend.services import balance_service
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, adjust_balance, login, other_client, set_balance


def _balance(client, user_id):
    with client.app.state.session_factory() as db:
        return db.get(User, user_id).balance_cents


def _activity(client, user_id):
    with client.app.state.session_factory() as db:
        return db.scalars(
            select(BalanceActivity).where(BalanceActivity.user_id == user_id).order_by(BalanceActivity.id)
        ).all()


def _admin_id(client):
    with client.app.state.session_factory() as db:
        return db.scalar(select(User.id).where(User.username == ADMIN_USERNAME))


# --- money parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, cents",
    [("0", 0), ("125.50", 12550), ("25.5", 2550), ("-25", -2500), ("+3.07", 307), ("$1,250.00", 125000), ("1000000000.00", 100_000_000_000)],
)
def test_parse_amount_valid(raw, cents):
    assert parse_amount_to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["", "  ", "abc", "1.234", "1e5", "1,23", "NaN", "Infinity", "0x10", "1000000000.01", "12.", ".5"])
def test_parse_amount_invalid(raw):
    with pytest.raises(AmountError):
        parse_amount_to_cents(raw)


def test_format_cents():
    assert format_cents(0) == "$0.00"
    assert format_cents(2550) == "$25.50"
    assert format_cents(125000) == "$1,250.00"
    assert format_cents(-5) == "-$0.05"
    assert format_cents(150, signed=True) == "+$1.50"


# --- set / adjust ------------------------------------------------------------------


def test_set_balance(admin_client, alice):
    response = set_balance(admin_client, alice.id, 10_000, reason="Opening balance")
    assert response.status_code == 201
    body = response.json()
    assert (body["previous_cents"], body["amount_cents"], body["resulting_cents"]) == (0, 10_000, 10_000)
    response = set_balance(admin_client, alice.id, 25_000)
    body = response.json()
    assert (body["previous_cents"], body["amount_cents"], body["resulting_cents"]) == (10_000, 15_000, 25_000)
    assert body["user"]["balance_cents"] == 25_000
    assert _balance(admin_client, alice.id) == 25_000


def test_positive_and_negative_adjustments(admin_client, alice):
    assert adjust_balance(admin_client, alice.id, 10_000).json()["resulting_cents"] == 10_000
    body = adjust_balance(admin_client, alice.id, -2_500).json()
    assert (body["previous_cents"], body["amount_cents"], body["resulting_cents"]) == (10_000, -2_500, 7_500)
    assert adjust_balance(admin_client, alice.id, -7_500).json()["resulting_cents"] == 0


def test_activity_record_is_correct(admin_client, alice):
    adjust_balance(admin_client, alice.id, 5_000, reason="  first   credit ")
    set_balance(admin_client, alice.id, 1_234)
    records = _activity(admin_client, alice.id)
    assert len(records) == 2
    first, second = records
    assert (first.change_type, first.previous_cents, first.amount_cents, first.resulting_cents) == ("adjust", 0, 5_000, 5_000)
    assert first.reason == "first credit"
    assert (second.change_type, second.previous_cents, second.amount_cents, second.resulting_cents) == ("set", 5_000, -3_766, 1_234)
    assert second.reason is None
    assert first.admin_id == second.admin_id == _admin_id(admin_client)
    assert first.user_id == alice.id
    assert second.resulting_cents == _balance(admin_client, alice.id)


def test_activity_records_the_acting_admin(admin_client, alice):
    from tests.conftest import make_user

    make_user(admin_client, "auditor", "auditor-pass-123", role="admin")
    second = other_client(admin_client)
    second.headers.update(login(second, "auditor", "auditor-pass-123"))
    adjust_balance(second, alice.id, 100)
    record = _activity(admin_client, alice.id)[-1]
    with admin_client.app.state.session_factory() as db:
        assert db.get(User, record.admin_id).username == "auditor"


def test_negative_balance_rejected_by_default(admin_client, alice):
    adjust_balance(admin_client, alice.id, 1_000)
    response = adjust_balance(admin_client, alice.id, -1_001)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "negative_balance"
    assert set_balance(admin_client, alice.id, -1).status_code == 409
    assert _balance(admin_client, alice.id) == 1_000
    assert len(_activity(admin_client, alice.id)) == 1


def test_negative_balance_allowed_when_enabled(make_client):
    from tests.conftest import make_user

    client = make_client(allow_negative=True)
    client.headers.update(login(client, ADMIN_USERNAME, ADMIN_PASSWORD))
    user = make_user(client, "carol")
    response = adjust_balance(client, user.id, -5_000)
    assert response.status_code == 201
    assert response.json()["resulting_cents"] == -5_000
    assert set_balance(client, user.id, -7_000).json()["amount_cents"] == -2_000


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "adjust", "amount_cents": 0},
        {"mode": "adjust", "amount_cents": 12.5},
        {"mode": "adjust", "amount_cents": "100"},
        {"mode": "adjust", "amount_cents": True},
        {"mode": "adjust", "amount_cents": None},
        {"mode": "adjust"},
        {"mode": "set", "target_cents": 1.0},
        {"mode": "set", "target_cents": 100_000_000_001},
        {"mode": "adjust", "amount_cents": -100_000_000_001},
        {"mode": "set", "target_cents": 100, "amount_cents": 100},  # ambiguous
        {"mode": "adjust", "amount_cents": 100, "target_cents": 100},  # ambiguous
        {"mode": "multiply", "amount_cents": 2},
        {"amount_cents": 100},
        {"mode": "adjust", "amount_cents": 100, "reason": "x" * 201},
        {"mode": "adjust", "amount_cents": 100, "previous_cents": 0},  # client-supplied previous is not accepted
    ],
)
def test_invalid_balance_payloads_rejected(admin_client, alice, payload):
    response = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert _balance(admin_client, alice.id) == 0
    assert _activity(admin_client, alice.id) == []


def test_resulting_balance_out_of_range_rejected(admin_client, alice):
    assert set_balance(admin_client, alice.id, 100_000_000_000).status_code == 201
    response = adjust_balance(admin_client, alice.id, 1)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "out_of_range"
    assert len(_activity(admin_client, alice.id)) == 1


def test_set_to_same_value_rejected(admin_client, alice):
    response = set_balance(admin_client, alice.id, 0)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_change"


def test_balance_unknown_user_and_admin_account(admin_client):
    assert adjust_balance(admin_client, 99_999, 100).status_code == 404
    response = adjust_balance(admin_client, _admin_id(admin_client), 100)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_customer_account"


def test_unauthenticated_and_user_balance_requests_rejected(anon, user_client, alice):
    from tests.conftest import csrf_token

    response = anon.post(
        f"/api/admin/users/{alice.id}/balance/",
        json={"mode": "adjust", "amount_cents": 100},
        headers={"X-CSRF-Token": csrf_token(anon)},
    )
    assert response.status_code == 401
    assert adjust_balance(user_client, alice.id, 100).status_code == 403
    assert _balance(anon, alice.id) == 0


# --- atomicity ------------------------------------------------------------------------


def test_failed_activity_insert_rolls_back_balance(admin_client, alice, monkeypatch):
    adjust_balance(admin_client, alice.id, 1_000)

    class ExplodingActivity:
        def __init__(self, **kwargs):
            raise RuntimeError("simulated failure while writing activity")

    monkeypatch.setattr(balance_service, "BalanceActivity", ExplodingActivity)
    admin_client_raise = admin_client
    with pytest.raises(RuntimeError):
        adjust_balance(admin_client_raise, alice.id, 500)
    assert _balance(admin_client, alice.id) == 1_000
    assert len(_activity(admin_client, alice.id)) == 1


def test_failed_commit_rolls_back_everything(admin_client, alice, monkeypatch):
    from sqlalchemy.orm import Session

    original_flush = Session.flush

    def failing_flush(self, *args, **kwargs):
        if any(isinstance(obj, BalanceActivity) for obj in self.new):
            raise RuntimeError("simulated database failure")
        return original_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", failing_flush)
    with pytest.raises(RuntimeError):
        adjust_balance(admin_client, alice.id, 500)
    monkeypatch.setattr(Session, "flush", original_flush)
    assert _balance(admin_client, alice.id) == 0
    assert _activity(admin_client, alice.id) == []


def test_server_error_returns_safe_json(admin_client, alice, monkeypatch):
    from fastapi.testclient import TestClient

    class ExplodingActivity:
        def __init__(self, **kwargs):
            raise RuntimeError("secret internal detail")

    monkeypatch.setattr(balance_service, "BalanceActivity", ExplodingActivity)
    client = TestClient(admin_client.app, raise_server_exceptions=False, cookies=admin_client.cookies)
    response = client.post(
        f"/api/admin/users/{alice.id}/balance/",
        json={"mode": "adjust", "amount_cents": 100},
        headers=dict(admin_client.headers),
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_error"
    assert "secret internal detail" not in response.text


# --- idempotency -------------------------------------------------------------------------


def test_idempotent_retry_applies_once(admin_client, alice):
    headers = {"Idempotency-Key": "retry-key-0001"}
    first = adjust_balance(admin_client, alice.id, 2_500, headers=headers)
    second = adjust_balance(admin_client, alice.id, 2_500, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["activity"]["id"] == first.json()["activity"]["id"]
    assert _balance(admin_client, alice.id) == 2_500
    assert len(_activity(admin_client, alice.id)) == 1


def test_idempotency_key_reuse_with_different_payload_conflicts(admin_client, alice):
    headers = {"Idempotency-Key": "retry-key-0002"}
    adjust_balance(admin_client, alice.id, 2_500, headers=headers)
    response = adjust_balance(admin_client, alice.id, 9_999, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_mismatch"
    assert _balance(admin_client, alice.id) == 2_500


def test_invalid_idempotency_key_rejected(admin_client, alice):
    response = adjust_balance(admin_client, alice.id, 100, headers={"Idempotency-Key": "bad key!"})
    assert response.status_code == 422


# --- concurrency ---------------------------------------------------------------------------


def test_concurrent_adjustments_do_not_lose_updates(admin_client, alice):
    """Many simultaneous adjustments via separate DB sessions: none is lost and the
    activity chain is consistent (each previous == prior resulting)."""
    app = admin_client.app
    with app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == ADMIN_USERNAME))
        db.expunge(admin)

    workers, per_worker = 8, 5
    errors = []
    barrier = threading.Barrier(workers)

    def worker():
        barrier.wait()
        for _ in range(per_worker):
            with app.state.session_factory() as db:
                try:
                    balance_service.apply_change(
                        db, admin=db.get(User, admin.id),
                        user_id=alice.id, mode="adjust", value=100, reason=None, allow_negative=False,
                    )
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    total = workers * per_worker
    assert _balance(admin_client, alice.id) == total * 100
    records = _activity(admin_client, alice.id)
    assert len(records) == total
    chain = sorted(records, key=lambda r: r.id)
    running = 0
    for record in chain:
        assert record.previous_cents == running
        assert record.resulting_cents == record.previous_cents + record.amount_cents
        running = record.resulting_cents
    assert running == total * 100


def test_concurrent_set_and_adjust_history_matches_balance(admin_client, alice):
    app = admin_client.app
    with app.state.session_factory() as db:
        admin_id = db.scalar(select(User.id).where(User.username == ADMIN_USERNAME))

    ops = [("set", 50_000), ("adjust", 700), ("adjust", -300), ("set", 12_345), ("adjust", 55)] * 4

    def run(mode, value):
        with app.state.session_factory() as db:
            try:
                balance_service.apply_change(
                    db, admin=db.get(User, admin_id), user_id=alice.id, mode=mode, value=value,
                    reason=None, allow_negative=True,
                )
            except Exception:
                pass  # e.g. "no_change" for a duplicate set — must not write anything

    threads = [threading.Thread(target=run, args=op) for op in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = sorted(_activity(admin_client, alice.id), key=lambda r: r.id)
    running = 0
    for record in records:
        assert record.previous_cents == running
        running = record.resulting_cents
    assert running == _balance(admin_client, alice.id)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(BalanceActivity)) == len(records)
