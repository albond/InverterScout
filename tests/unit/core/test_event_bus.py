"""EventBus tests: pub/sub, wildcard, unsubscribe, request/response."""

import time

import pytest

from inverterscout.core.state import Event, EventBus


@pytest.fixture
def bus():
    return EventBus()


class TestSubscribeEmit:
    async def test_single_handler(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test_event", handler)
        await bus.emit(Event(type="test_event", timestamp=time.time()))
        assert len(received) == 1
        assert received[0].type == "test_event"

    async def test_multiple_handlers(self, bus):
        count = [0]

        async def h1(event):
            count[0] += 1

        async def h2(event):
            count[0] += 10

        bus.subscribe("ev", h1)
        bus.subscribe("ev", h2)
        await bus.emit(Event(type="ev", timestamp=time.time()))
        assert count[0] == 11

    async def test_no_handler_for_event(self, bus):
        """Emitting an event without subscribers does not crash."""
        await bus.emit(Event(type="unknown", timestamp=time.time()))

    async def test_wildcard_receives_all(self, bus):
        received = []

        async def handler(event):
            received.append(event.type)

        bus.subscribe("*", handler)
        await bus.emit(Event(type="a", timestamp=time.time()))
        await bus.emit(Event(type="b", timestamp=time.time()))
        assert received == ["a", "b"]

    async def test_wildcard_and_specific(self, bus):
        """Wildcard + specific subscriber - both called."""
        types = []

        async def specific(event):
            types.append(("specific", event.type))

        async def wildcard(event):
            types.append(("wildcard", event.type))

        bus.subscribe("ev", specific)
        bus.subscribe("*", wildcard)
        await bus.emit(Event(type="ev", timestamp=time.time()))
        assert ("specific", "ev") in types
        assert ("wildcard", "ev") in types


class TestUnsubscribe:
    async def test_unsubscribe_stops_receiving(self, bus):
        count = [0]

        async def handler(event):
            count[0] += 1

        bus.subscribe("ev", handler)
        await bus.emit(Event(type="ev", timestamp=time.time()))
        assert count[0] == 1

        bus.unsubscribe("ev", handler)
        await bus.emit(Event(type="ev", timestamp=time.time()))
        assert count[0] == 1  # hasn't changed

    async def test_unsubscribe_nonexistent_handler(self, bus):
        """Unsubscribing a non-existent handler does not fail."""

        async def handler(event):
            pass

        bus.unsubscribe("ev", handler)  # no error


class TestRequestResponse:
    async def test_request_response(self, bus):
        async def responder(event):
            event.respond({"answer": 42})

        bus.subscribe("query", responder)
        result = await bus.request("query")
        assert result == {"answer": 42}

    async def test_request_no_responder(self, bus):
        """Request without responder → RuntimeError."""
        with pytest.raises(RuntimeError, match="No handler responded"):
            await bus.request("nobody_listens")


class TestExceptionHandling:
    async def test_handler_exception_does_not_break_others(self, bus):
        results = []

        async def bad_handler(event):
            raise ValueError("boom")

        async def good_handler(event):
            results.append("ok")

        bus.subscribe("ev", bad_handler)
        bus.subscribe("ev", good_handler)
        await bus.emit(Event(type="ev", timestamp=time.time()))
        assert results == ["ok"]
