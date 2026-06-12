import logging
import threading
import time

from .conf import settings

log = logging.getLogger("discord_presence_manager")

discord_presence = False
if settings.discord_presence:
    try:
        from .rich_presence import (
            register_join_event,
            send_presence,
            clear_presence,
        )

        discord_presence = True
    except Exception:
        log.error("Could not enable Discord Rich Presence.", exc_info=True)


def _get_title_and_subtitle(item):
    """Extract title and subtitle from a Jellyfin item."""
    if (
        item.get("Type") == "Episode"
        and item.get("IndexNumber") is not None
        and item.get("ParentIndexNumber") is not None
    ):
        title = item.get("SeriesName", item.get("Name", "Unknown"))
        subtitle = "Season %s - Episode %s" % (
            item.get("ParentIndexNumber"),
            item.get("IndexNumber"),
        )
    else:
        title = item.get("Name", "Unknown")
        year = item.get("ProductionYear")
        subtitle = str(year) if year is not None else ""
    return title, subtitle


def _ticks_to_seconds(ticks):
    """Convert Jellyfin ticks (100ns units) to seconds."""
    if ticks is None:
        return None
    return ticks / 10000000


def _get_playback_time(play_state):
    """Get playback position in seconds from play state."""
    if play_state is None:
        return None
    position_ticks = play_state.get("PositionTicks")
    if position_ticks is None:
        return None
    return _ticks_to_seconds(position_ticks)


def _get_duration(item):
    """Get total duration in seconds from item metadata."""
    run_time_ticks = item.get("RunTimeTicks")
    if run_time_ticks is None:
        return None
    return _ticks_to_seconds(run_time_ticks)


def _find_best_session(sessions, shim_device_id):
    """Find the best session to display on Discord.

    Priority:
      1. This shim (matches our DeviceId) if actively playing
      2. Any other session that is actively playing (most recently active first)
    """
    shim_session = None
    best_other = None

    for session in sessions:
        now_playing = session.get("NowPlayingItem")
        if now_playing is None:
            continue

        play_state = session.get("PlayState", {})
        if play_state.get("IsPaused", False) and play_state.get("PositionTicks") is None:
            continue

        if session.get("DeviceId") == shim_device_id:
            shim_session = session
        else:
            if best_other is None:
                best_other = session
            else:
                this_date = session.get("LastActivityDate", "")
                best_date = best_other.get("LastActivityDate", "")
                if this_date > best_date:
                    best_other = session

    return shim_session or best_other


class DiscordPresenceManager(threading.Thread):
    def __init__(self, client_manager):
        self.halt = False
        self.trigger = threading.Event()
        self.client_manager = client_manager
        self._registered_join = False

        threading.Thread.__init__(self, daemon=True)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join()

    def run(self):
        if not discord_presence:
            return

        if not self._registered_join:
            try:
                from .player import playerManager

                register_join_event(playerManager.syncplay.discord_join_group)
                self._registered_join = True
            except Exception:
                log.error(
                    "Could not register Discord join callback.", exc_info=True
                )

        while not self.halt:
            try:
                self._poll_sessions()
            except Exception:
                log.error("Error polling Discord sessions.", exc_info=True)
            self.trigger.wait(5)

    def _poll_sessions(self):
        all_sessions = []
        for client in self.client_manager.clients.values():
            try:
                sessions = client.jellyfin._http(
                    "GET", "Sessions", {"params": None, "timeout": 10, "retry": 1}
                )
                if sessions:
                    all_sessions.extend(sessions)
            except Exception:
                log.debug(
                    "Could not fetch sessions from server.", exc_info=True
                )

        if not all_sessions:
            try:
                clear_presence()
            except Exception:
                log.debug("Could not clear Discord presence.", exc_info=True)
            return

        best = _find_best_session(all_sessions, settings.client_uuid)

        if best is None:
            try:
                clear_presence()
            except Exception:
                log.debug("Could not clear Discord presence.", exc_info=True)
            return

        try:
            item = best.get("NowPlayingItem", {})
            play_state = best.get("PlayState", {})

            title, subtitle = _get_title_and_subtitle(item)
            playback_time = _get_playback_time(play_state)
            duration = _get_duration(item)
            playing = not play_state.get("IsPaused", False)
            media_type = item.get("Type")

            send_presence(
                title,
                subtitle,
                playback_time,
                duration,
                playing,
                None,
                media_type,
            )
        except Exception:
            log.error("Could not send Discord Rich Presence.", exc_info=True)
