from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from scat.infrastructure import database


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        FLIGHT_MONGO_URI=SimpleNamespace(get_secret_value=lambda: "mongodb://test"),
        FLIGHT_MONGO_DB="flight_data_test",
        FLIGHT_MONGO_CONNECT_TIMEOUT_MS=5000,
        FLIGHT_MONGO_SERVER_SELECTION_TIMEOUT_MS=5000,
        FLIGHT_MONGO_SOCKET_TIMEOUT_MS=15000,
        FLIGHT_MONGO_OPERATION_TIMEOUT_MS=30000,
    )


@pytest.mark.asyncio
async def test_connect_publishes_client_only_after_success(monkeypatch) -> None:
    fake_client = Mock()
    fake_client.admin.command = AsyncMock()
    fake_client.close = AsyncMock()

    monkeypatch.setattr(database, "settings", _fake_settings())
    client_factory = Mock(return_value=fake_client)
    monkeypatch.setattr(database, "AsyncMongoClient", client_factory)

    flight_database = database.FlightDatabase()
    await flight_database.connect()

    assert flight_database.client is fake_client
    fake_client.admin.command.assert_awaited_once_with("ping")
    client_factory.assert_called_once()
    _, kwargs = client_factory.call_args
    assert kwargs["connectTimeoutMS"] == 5000
    assert kwargs["serverSelectionTimeoutMS"] == 5000
    assert kwargs["socketTimeoutMS"] == 15000
    assert kwargs["timeoutMS"] == 30000
    assert len(kwargs["event_listeners"]) == 1
    assert isinstance(kwargs["event_listeners"][0], database.MongoCommandObserver)


@pytest.mark.asyncio
async def test_failed_connect_cleans_up_and_can_retry(monkeypatch) -> None:
    failed_client = Mock()
    failed_client.admin.command = AsyncMock(side_effect=RuntimeError("offline"))
    failed_client.close = AsyncMock()

    working_client = Mock()
    working_client.admin.command = AsyncMock()
    working_client.close = AsyncMock()

    monkeypatch.setattr(database, "settings", _fake_settings())
    monkeypatch.setattr(
        database,
        "AsyncMongoClient",
        Mock(side_effect=[failed_client, working_client]),
    )

    flight_database = database.FlightDatabase()

    with pytest.raises(RuntimeError, match="offline"):
        await flight_database.connect()

    failed_client.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="has not been initialized"):
        _ = flight_database.client

    await flight_database.connect()
    assert flight_database.client is working_client


@pytest.mark.asyncio
async def test_close_clears_client(monkeypatch) -> None:
    fake_client = Mock()
    fake_client.admin.command = AsyncMock()
    fake_client.close = AsyncMock()

    monkeypatch.setattr(database, "settings", _fake_settings())
    monkeypatch.setattr(database, "AsyncMongoClient", Mock(return_value=fake_client))

    flight_database = database.FlightDatabase()
    await flight_database.connect()
    await flight_database.close()

    fake_client.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="has not been initialized"):
        _ = flight_database.client


def test_analytics_client_has_a_finite_timeout(monkeypatch) -> None:
    fake_client = MagicMock()
    client_factory = Mock(return_value=fake_client)
    monkeypatch.setattr(database, "settings", _fake_settings())
    monkeypatch.setattr(database, "AsyncMongoClient", client_factory)

    flight_database = database.FlightDatabase()
    flight_database.get_analytics_collection("scat_flights")

    client_factory.assert_called_once()
    _, kwargs = client_factory.call_args
    assert kwargs["connectTimeoutMS"] == 5000
    assert kwargs["serverSelectionTimeoutMS"] == 5000
    assert kwargs["socketTimeoutMS"] == 210_000
    assert kwargs["timeoutMS"] == 210_000
    assert len(kwargs["event_listeners"]) == 1
