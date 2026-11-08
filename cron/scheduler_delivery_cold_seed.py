"""Cold-start fallback for the cron delivery mirror (split out of ``cron.scheduler_delivery``).

``mirror_to_session`` only appends to an OPEN session. When the target chat was idle and its
session ended, the mirror used to be skipped ("cold start"), so the user's reply to the alert
opened a session that had never seen it. For a live-lane flat target whose reply key can be
rebuilt, the existing ``in_channel`` seed primitive creates (or resumes) the session that reply
resolves to and appends the delivery there.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def cold_session_seeder(t: Any) -> Optional[Callable[[str], bool]]:
    """``cold_seed`` for ``_maybe_mirror_cron_delivery``: a DM, or a group with the origin's real
    user id (``_inchannel_seed_allowed``), on an adapter with a session store; None otherwise
    (thread lanes have their own seeds; the standalone lane has no session store)."""
    from cron import scheduler_delivery as sd

    if t.thread_id or getattr(t.runtime_adapter, "_session_store", None) is None:
        return None
    if not sd._inchannel_seed_allowed(is_dm=t.is_dm_target, user_id=t.origin_user_id):
        return None

    def seed(text: str) -> bool:
        seeded = sd._seed_cron_channel_session(
            t.job, t.runtime_adapter, t.platform_name, t.chat_id, text, is_dm=t.is_dm_target,
            user_id=t.origin_user_id, chat_name=t.origin.get("chat_name"),
            scope_id=t.origin.get("scope_id"))
        if seeded:
            logger.info("Job '%s': no open session for %s:%s; seeded the one the next reply opens",
                        t.job.get("id", "?"), t.platform_name, t.chat_id)
        return seeded

    return seed
