"""A mirror-eligible cron delivery reaches the chat's session even when no session is open.

``mirror_to_session`` only APPENDS to an open session found by origin. When the chat was idle
(its session ended) the mirror was skipped ("no matching gateway session — cold start"), so the
user's next message started a session that had never seen the alert they were replying to. The
live lane now seeds the session the reply resolves to (same primitive as the in_channel seed).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler_delivery as sd


def cold_session_seeder(t):
    # Imported per call so the mirror and end-to-end tests below still run against a tree
    # without the fallback module (they then fail on the bug, not on collection).
    from cron.scheduler_delivery_cold_seed import cold_session_seeder as seeder
    return seeder(t)

JOB = {"id": "job1", "name": "Price watch"}


def _target(**overrides):
    base = dict(job=JOB, platform_name="telegram", chat_id="555", thread_id=None,
                runtime_adapter=SimpleNamespace(_session_store=MagicMock()), is_dm_target=True,
                origin_user_id="555", origin={"chat_name": "Alice"})
    base.update(overrides)
    return SimpleNamespace(**base)


class TestMirrorFallsBackToSeed:
    def test_cold_chat_is_seeded_with_the_delivery(self):
        cold_seed = MagicMock(return_value=True)
        with patch("gateway.mirror.mirror_to_session", return_value=False):
            sd._maybe_mirror_cron_delivery(JOB, "telegram", "555", " Price dropped ", enabled=True,
                                        cold_seed=cold_seed)
        cold_seed.assert_called_once_with("Price dropped")

    def test_open_session_is_mirrored_without_seeding(self):
        cold_seed = MagicMock(return_value=True)
        with patch("gateway.mirror.mirror_to_session", return_value=True):
            sd._maybe_mirror_cron_delivery(JOB, "telegram", "555", "Price dropped", enabled=True, cold_seed=cold_seed)
        cold_seed.assert_not_called()

    def test_disabled_mirror_never_seeds(self):
        cold_seed = MagicMock(return_value=True)
        with patch("gateway.mirror.mirror_to_session") as mirror:
            sd._maybe_mirror_cron_delivery(JOB, "telegram", "555", "Price dropped", enabled=False, cold_seed=cold_seed)
        mirror.assert_not_called()
        cold_seed.assert_not_called()

    def test_seed_failure_never_raises(self):
        with patch("gateway.mirror.mirror_to_session", return_value=False):
            sd._maybe_mirror_cron_delivery(JOB, "telegram", "555", "x", enabled=True,
                                        cold_seed=MagicMock(side_effect=RuntimeError("db locked")))


class TestColdSessionSeeder:
    def test_dm_target_seeds_the_flat_dm_session(self):
        t = _target()
        with patch("cron.scheduler_delivery._seed_cron_channel_session", return_value=True) as seed:
            assert cold_session_seeder(t)("Price dropped") is True
        args, kwargs = seed.call_args
        assert args[:5] == (JOB, t.runtime_adapter, "telegram", "555", "Price dropped")
        assert kwargs["is_dm"] is True and kwargs["user_id"] == "555" and kwargs["chat_name"] == "Alice"

    def test_group_target_with_origin_user_is_seedable(self):
        assert cold_session_seeder(_target(is_dm_target=False, origin_user_id="u1")) is not None

    @pytest.mark.parametrize("overrides", [
        {"is_dm_target": False, "origin_user_id": None},     # user-isolated group key cannot be rebuilt
        {"thread_id": "42"},                                  # thread lanes have their own seed
        {"runtime_adapter": SimpleNamespace()},               # no session store (standalone lane)
        {"runtime_adapter": None},
    ])
    def test_unseedable_targets_keep_the_old_behaviour(self, overrides):
        assert cold_session_seeder(_target(**overrides)) is None


def test_reply_after_a_cold_delivery_sees_it(tmp_path, monkeypatch):
    """End to end on a real session store + state.db: no open session for the DM, the cron
    delivery is mirrored, and the user's next message resolves to a session holding it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    t = _target(runtime_adapter=SimpleNamespace(_session_store=store), mirror_text="Price dropped to 199",
                mirror_this_target=True, opened_thread_id=None, in_channel_surface=False,
                inchannel_continuable=False, origin={"platform": "telegram", "chat_id": "555", "chat_type": "dm"})

    sd._seed_live_delivery_sessions(t, delivered_message_id="m1")

    reply = SessionSource(platform=Platform.TELEGRAM, chat_id="555", chat_type="dm", user_id="555")
    entry = store.get_or_create_session(reply)
    from hermes_state_registry import acquire, release_or_close
    db = acquire()
    try:
        contents = [m.get("content") for m in db.get_messages(entry.session_id)]
    finally:
        release_or_close(db)
    assert any("[Cron delivery: Price watch]" in (c or "") and "Price dropped to 199" in (c or "") for c in contents)
