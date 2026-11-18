"""Live chat tracking — Twitch IRC (anonymous, NO key) + YouTube note.

PeakCut's rule (2026-11-17): count chat PEOPLE, not messages — a bot
storm sends many messages from few accounts and must NOT fake a crowd.
So every signal below is unique chatters per time window, never raw
message counts.

Twitch IRC: anonymous login (justinfan) joins #channel and watches the
chat. No OAuth, no API key. Reconnects on drop. One background thread
per channel; the clip loop reads burst windows from it.

YouTube live chat: NOT tracked here — YouTube's live-chat API needs a
Google API key + OAuth. If the user adds a YouTube API key in
config.json (youtube_api_key), a future version can poll liveChatMessages.
Until then the dashboard says so plainly and picking uses speech +
loudness only for YouTube streams.
"""
import re
import socket
import threading
import time

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667
WINDOW_S = 60  # chat window length (seconds)
PRIVMSG_RE = re.compile(r"^:([^!]+)!.* PRIVMSG #\S+ :(.*)$")


class TwitchChatTracker:
    """One tracker per Twitch channel. Thread-safe."""

    def __init__(self, channel_url):
        name = channel_url.rstrip("/").split("/")[-1].split("?")[0].lower()
        self.channel = name
        self.lock = threading.Lock()
        # list of (timestamp, username) — pruned to the last 10 minutes
        self.events = []
        self.thread = None
        self.stop_event = threading.Event()
        self.connected = False
        self.last_error = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.thread is not None:
                self.thread.join(timeout=5)
        except Exception:
            pass

    def _run(self):
        backoff = 5
        while not self.stop_event.is_set():
            try:
                self._connect_loop()
                backoff = 5
            except Exception as e:
                self.last_error = str(e)
                self.connected = False
                # wait with backoff (max 60s) before reconnecting
                self.stop_event.wait(min(backoff, 60))
                backoff *= 2

    def _connect_loop(self):
        s = socket.create_connection((IRC_HOST, IRC_PORT), timeout=15)
        s.settimeout(30)
        f = s.makefile("r", encoding="utf-8", errors="ignore")
        try:
            s.sendall(b"PASS SCHMOOPIIE\r\n")
            s.sendall(b"NICK justinfan12345\r\n")
            s.sendall(f"JOIN #{self.channel}\r\n".encode())
            self.connected = True
            buf = ""
            while not self.stop_event.is_set():
                try:
                    chunk = f.readline()
                except socket.timeout:
                    continue
                if not chunk:
                    break
                line = chunk.strip()
                if line.startswith("PING"):
                    s.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    continue
                m = PRIVMSG_RE.match(line)
                if m:
                    user = m.group(1).lower()
                    now = time.time()
                    with self.lock:
                        self.events.append((now, user))
                        # prune to last 10 minutes
                        cutoff = now - 600
                        self.events = [e for e in self.events if e[0] >= cutoff]
        finally:
            self.connected = False
            try:
                s.close()
            except Exception:
                pass

    def unique_chatters(self, start_ts, end_ts):
        """Unique people chatting in [start_ts, end_ts] (epoch seconds)."""
        with self.lock:
            users = {u for t, u in self.events if start_ts <= t <= end_ts}
        return len(users)

    def chat_windows(self, start_ts, end_ts, step=30):
        """Per-window unique-chatter counts across [start_ts, end_ts].
        Returns [{start, end, unique, messages}] sorted by unique desc."""
        wins = []
        t = start_ts
        while t < end_ts:
            w_end = min(t + WINDOW_S, end_ts)
            with self.lock:
                evs = [(ts, u) for ts, u in self.events if t <= ts <= w_end]
            wins.append({"start": t, "end": w_end,
                         "unique": len({u for _, u in evs}),
                         "messages": len(evs)})
            t += step
        wins.sort(key=lambda w: -w["unique"])
        return wins

    def burst_score(self, start_ts, end_ts):
        """0..1: how much busier the busiest chat minute was vs the median
        minute in this range. 1.0 = big burst (crowd reacting)."""
        wins = self.chat_windows(start_ts, end_ts)
        if not wins:
            return 0.0
        uniques = sorted(w["unique"] for w in wins)
        med = uniques[len(uniques) // 2] if uniques else 0
        top = uniques[-1] if uniques else 0
        if med <= 0:
            return 1.0 if top >= 5 else 0.0
        return min(1.0, max(0.0, (top - med) / max(med, 1)))


class ChatManager:
    """Holds one TwitchChatTracker per Twitch channel being watched."""

    def __init__(self):
        self.lock = threading.Lock()
        self.trackers = {}

    def track(self, channel_url):
        if "twitch.tv" not in channel_url.lower():
            return None
        with self.lock:
            t = self.trackers.get(channel_url)
            if t is None:
                t = TwitchChatTracker(channel_url)
                self.trackers[channel_url] = t
                t.start()
            return t

    def get(self, channel_url):
        with self.lock:
            return self.trackers.get(channel_url)

    def stop_all(self):
        with self.lock:
            trackers = list(self.trackers.values())
        for t in trackers:
            t.stop()


# ---- picking helper (no-AI): chat-burst windows for one segment ----
def chat_burst_windows(tracker, seg_start_ts, seg_duration):
    """Best chat-burst windows inside a segment, in SEGMENT-relative seconds.
    seg_start_ts = epoch time the segment started recording.
    Returns [{start, end, unique, messages}] sorted by unique desc, or [].

    NOTE (fixed 2026-11-18): this used to return ABSOLUTE epoch times even
    though the docstring said segment-relative — so _adjust_for_chat()'s
    overlap check (burst centre vs the picked moment, both meant to be
    segment-relative) could never fire: the burst centre was ~1.7e9 while
    the moment was 0-180s. The re-centring feature was dead since it was
    written (verified with a fake tracker: 8 unique chatters + dense
    speech at 90s, moment at 10-50s -> no re-centre). Windows are now
    shifted to segment-relative here, so both _adjust_for_chat() and the
    chat-burst trigger in clip_live.py read them correctly."""
    if tracker is None:
        return []
    wins = tracker.chat_windows(seg_start_ts, seg_start_ts + seg_duration)
    # keep only windows with a real crowd (>= 3 unique people), and shift
    # to segment-relative seconds
    return [{"start": w["start"] - seg_start_ts, "end": w["end"] - seg_start_ts,
             "unique": w["unique"], "messages": w["messages"]}
            for w in wins if w["unique"] >= 3]
