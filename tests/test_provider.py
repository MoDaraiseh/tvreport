"""TradingView provider: parsing recorded responses, retry/throttle policy."""

from __future__ import annotations

import pytest
import requests

from tests.conftest import load_payload
from tvreport.config import ProviderConfig, UniverseConfig
from tvreport.providers.base import INDEX_COLUMNS, STOCK_COLUMNS, ProviderError
from tvreport.providers.tradingview import (
    TradingViewProvider,
    index_query,
    parse_indices,
    parse_stocks,
    universe_query,
)


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class Script:
    """Transport that replays a list of outcomes (exceptions or payloads)."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, query, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def provider(transport, clock=None) -> TradingViewProvider:
    clock = clock or FakeClock()
    return TradingViewProvider(
        ProviderConfig(), transport=transport, sleep=clock.sleep, clock=clock.clock
    )


OK = {"totalCount": 0, "data": []}


def test_parse_recorded_universe():
    payload = load_payload("universe.json")
    frame = parse_stocks(payload)
    assert list(frame.columns) == list(STOCK_COLUMNS)
    assert len(frame) == payload["totalCount"] == len(payload["data"])
    assert set(frame["type"]) == {"stock"} and set(frame["subtype"]) == {"common"}
    assert set(frame["exchange"]) <= {"NYSE", "NASDAQ", "AMEX"}
    assert (frame["market_cap"] >= 2e9).all() and (frame["close"] >= 5).all()
    assert frame["rsi"].dropna().between(0, 100).all()
    assert frame["rec_all"].dropna().between(-1, 1).all()


def test_recorded_sample_units():
    frame = parse_stocks(load_payload("discover_stocks.json")).set_index("ticker")
    assert set(frame.index) == {"AAPL", "MSFT", "NVDA", "JPM", "XOM"}
    implied = 100 * (frame["close"] / frame["prev_close"] - 1)
    assert (implied - frame["change_pct"]).abs().max() < 0.05  # `change` is a percentage


def test_parse_recorded_indices():
    frame = parse_indices(load_payload("indices.json"))
    assert list(frame.columns) == list(INDEX_COLUMNS)
    assert {
        "SP:SPX",
        "NASDAQ:IXIC",
        "NASDAQ:NDX",
        "DJ:DJI",
        "FTSE:RUT",
        "CBOE:VIX",
        "TVC:RUT",
    } == set(frame["symbol"])
    assert set(frame["type"]) == {"index"}


def test_row_with_wrong_width_is_rejected():
    with pytest.raises(ProviderError, match="values"):
        parse_stocks({"totalCount": 1, "data": [{"s": "X:Y", "d": [1, 2]}]})


def test_retries_network_errors_then_succeeds():
    clock = FakeClock()
    transport = Script(requests.ConnectionError("boom"), requests.Timeout("slow"), OK)
    assert provider(transport, clock).execute(index_query(["SP:SPX"]), "t") == OK
    assert transport.calls == 3
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps  # exponential backoff 2s, 4s


def test_5xx_retried_three_times_then_fails():
    transport = Script(http_error(503), http_error(502), http_error(500))
    with pytest.raises(ProviderError, match="failed after 3 attempts"):
        provider(transport).execute(index_query(["SP:SPX"]), "t")
    assert transport.calls == 3


@pytest.mark.parametrize("status", [400, 403, 404, 429])
def test_4xx_not_retried(status):
    transport = Script(http_error(status), OK)
    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        provider(transport).execute(index_query(["SP:SPX"]), "t")
    assert transport.calls == 1


def test_throttle_spaces_requests_by_min_interval():
    clock = FakeClock()
    p = provider(Script(OK, OK), clock)
    p.execute(index_query(["SP:SPX"]), "a")
    p.execute(index_query(["SP:SPX"]), "b")
    assert clock.sleeps == [1.0]


def test_unexpected_shape_rejected():
    with pytest.raises(ProviderError, match="unexpected response shape"):
        provider(Script({"oops": 1})).execute(index_query(["SP:SPX"]), "t")


def test_truncated_universe_aborts():
    payload = {"totalCount": 9999, "data": []}
    with pytest.raises(ProviderError, match="truncated"):
        provider(Script(payload)).fetch_universe(UniverseConfig())


def test_missing_index_symbol_aborts():
    payload = load_payload("indices.json")
    with pytest.raises(ProviderError, match="NOT:THERE"):
        provider(Script(payload)).fetch_indices(["SP:SPX", "NOT:THERE"])


def test_query_shapes():
    uq = universe_query(UniverseConfig()).query
    assert "filter2" not in uq
    lefts = {f["left"] for f in uq["filter"]}
    assert {
        "is_primary",
        "exchange",
        "type",
        "typespecs",
        "market_cap_basic",
        "average_volume_10d_calc",
        "close",
    } <= lefts
    assert "country" not in lefts
    us_only = universe_query(UniverseConfig(include_foreign_domiciled=False)).query
    assert any(f["left"] == "country" for f in us_only["filter"])
    iq = index_query(["SP:SPX"]).query
    assert "filter" not in iq and "filter2" not in iq
