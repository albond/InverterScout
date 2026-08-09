"""SubscriberManager tests: load/save, roundtrip, corrupt JSON."""

import pytest

from inverterscout.settings.runtime import SubscriberManager


@pytest.fixture
def mgr(data_dir):
    """Clean SubscriberManager (patch_paths is already active)."""
    return SubscriberManager()


class TestLoadEmpty:
    def test_load_all_no_files(self, mgr):
        """Uploading without files means empty collections."""
        mgr.load_all()
        assert mgr.subscribers == set()
        assert mgr.pending == []
        assert mgr.blocked == set()
        assert mgr.user_names == {}


class TestSaveLoadRoundtrip:
    def test_subscribers_roundtrip(self, mgr):
        mgr.subscribers = {100, 200, 300}
        mgr.save_subscribers()

        mgr2 = SubscriberManager()
        mgr2.load_all()
        assert mgr2.subscribers == {100, 200, 300}

    def test_pending_roundtrip(self, mgr):
        mgr.pending = [{"chat_id": 999, "username": "test", "first_name": "Test"}]
        mgr.save_pending()

        mgr2 = SubscriberManager()
        mgr2.load_all()
        assert len(mgr2.pending) == 1
        assert mgr2.pending[0]["chat_id"] == 999

    def test_blocked_roundtrip(self, mgr):
        mgr.blocked = {777, 888}
        mgr.save_blocked()

        mgr2 = SubscriberManager()
        mgr2.load_all()
        assert mgr2.blocked == {777, 888}

    def test_user_names_roundtrip(self, mgr):
        mgr.set_user_name(100, "Alice", "alice")
        mgr.set_user_name(200, "Bob", "")

        mgr2 = SubscriberManager()
        mgr2.load_all()
        assert mgr2.get_display_name(100) == "Alice"
        assert mgr2.get_username(100) == "@alice"
        assert mgr2.get_username(200) == "—"


class TestCorruptJson:
    def test_corrupt_subscribers(self, mgr, data_dir):
        """Broken JSON → graceful fallback to an empty set."""
        from inverterscout.settings import runtime as shared

        path = shared.SUBSCRIBERS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{invalid json")

        mgr.load_all()
        assert mgr.subscribers == set()

    def test_corrupt_pending(self, mgr, data_dir):
        from inverterscout.settings import runtime as shared

        path = shared.PENDING_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")

        mgr.load_all()
        assert mgr.pending == []
