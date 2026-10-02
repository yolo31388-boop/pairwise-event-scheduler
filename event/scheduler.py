"""限时活动调度"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

UTC = dt_timezone.utc

DEFAULT_REWARD_GRACE = timedelta(days=7)


def _as_utc(dt: datetime | None, tz_name: str = "UTC") -> datetime | None:
    """把(可能naive的)时间统一成UTC aware。naive时间按配置时区解释，zoneinfo自动处理夏令时。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(UTC)


@dataclass
class EventState:
    eid: str
    status: str = "idle"  # idle/active/ended
    player_progress: dict = field(default_factory=dict)
    rewards_claimed: dict = field(default_factory=dict)
    task_ids: set = field(default_factory=set)
    start_time: datetime | None = None  # UTC
    end_time: datetime | None = None  # UTC
    reward_claim_deadline: datetime | None = None  # UTC，结束后领奖保留期截止


class EventScheduler:
    """活动调度器。状态经内存缓存同步持久化到SQLite，重启后自动恢复。"""

    def __init__(self, db_path: str | None = None,
                 reward_grace: timedelta = DEFAULT_REWARD_GRACE,
                 default_timezone: str = "UTC"):
        self.db_path = db_path or ":memory:"
        self.reward_grace = reward_grace
        self.default_timezone = default_timezone
        self.events: dict[str, EventState] = {}
        self._conn = sqlite3.connect(self.db_path)
        self._init_schema()
        self._load()

    # ---------- 持久化 ----------

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                eid TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                task_ids TEXT NOT NULL DEFAULT '[]',
                start_time TEXT,
                end_time TEXT,
                reward_claim_deadline TEXT
            );
            CREATE TABLE IF NOT EXISTS player_progress (
                eid TEXT NOT NULL,
                pid TEXT NOT NULL,
                task_id TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (eid, pid, task_id)
            );
            CREATE TABLE IF NOT EXISTS rewards_claimed (
                eid TEXT NOT NULL,
                pid TEXT NOT NULL,
                claimed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (eid, pid)
            );
        """)
        self._conn.commit()

    @staticmethod
    def _dt_to_str(dt: datetime | None) -> str | None:
        return dt.astimezone(UTC).isoformat() if dt else None

    @staticmethod
    def _dt_from_str(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    def _persist_event(self, state: EventState):
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
            (state.eid, state.status, json.dumps(sorted(state.task_ids)),
             self._dt_to_str(state.start_time), self._dt_to_str(state.end_time),
             self._dt_to_str(state.reward_claim_deadline)))
        self._conn.execute("DELETE FROM player_progress WHERE eid=?", (state.eid,))
        self._conn.executemany(
            "INSERT OR REPLACE INTO player_progress VALUES (?,?,?,?)",
            [(state.eid, pid, task_id, int(done))
             for pid, tasks in state.player_progress.items()
             for task_id, done in tasks.items()])
        self._conn.execute("DELETE FROM rewards_claimed WHERE eid=?", (state.eid,))
        self._conn.executemany(
            "INSERT OR REPLACE INTO rewards_claimed VALUES (?,?,?)",
            [(state.eid, pid, int(claimed))
             for pid, claimed in state.rewards_claimed.items()])
        self._conn.commit()

    def _load(self):
        for eid, status, task_ids, start, end, deadline in self._conn.execute(
                "SELECT eid,status,task_ids,start_time,end_time,reward_claim_deadline FROM events"):
            self.events[eid] = EventState(
                eid=eid, status=status, task_ids=set(json.loads(task_ids)),
                start_time=self._dt_from_str(start), end_time=self._dt_from_str(end),
                reward_claim_deadline=self._dt_from_str(deadline))
        for eid, pid, task_id, done in self._conn.execute(
                "SELECT eid,pid,task_id,completed FROM player_progress"):
            if eid in self.events:
                self.events[eid].player_progress.setdefault(pid, {})[task_id] = bool(done)
        for eid, pid, claimed in self._conn.execute(
                "SELECT eid,pid,claimed FROM rewards_claimed"):
            if eid in self.events:
                self.events[eid].rewards_claimed[pid] = bool(claimed)

    def close(self):
        self._conn.close()

    # ---------- 活动生命周期 ----------

    def start_event(self, eid: str, task_ids=None,
                    start_time: datetime | None = None,
                    end_time: datetime | None = None,
                    timezone: str | None = None) -> bool:
        tz = timezone or self.default_timezone
        if eid not in self.events:
            self.events[eid] = EventState(eid)
        state = self.events[eid]
        state.status = "active"
        if task_ids is not None:
            state.task_ids = set(task_ids)
        if start_time is not None:
            state.start_time = _as_utc(start_time, tz)
        if end_time is not None:
            state.end_time = _as_utc(end_time, tz)
        self._persist_event(state)
        return True

    def end_event(self, eid: str, now: datetime | None = None) -> bool:
        state = self.events.get(eid)
        if not state:
            return False
        state.status = "ended"
        # 保留玩家进度，并给有进度的玩家登记未领取奖励，保留期内可领取
        for pid in state.player_progress:
            state.rewards_claimed.setdefault(pid, False)
        state.reward_claim_deadline = _as_utc(now or datetime.now(UTC)) + self.reward_grace
        self._persist_event(state)
        return True

    def claim_reward(self, eid: str, pid: str, now: datetime | None = None) -> bool:
        state = self.events.get(eid)
        if not state:
            return False
        if pid not in state.rewards_claimed and pid in state.player_progress:
            state.rewards_claimed[pid] = False
        if pid not in state.rewards_claimed or state.rewards_claimed[pid]:
            return False
        if state.status == "ended":
            deadline = state.reward_claim_deadline
            if deadline is None or _as_utc(now or datetime.now(UTC)) > deadline:
                return False
        elif state.status != "active":
            return False
        state.rewards_claimed[pid] = True
        self._persist_event(state)
        return True

    def cleanup_expired(self, now: datetime | None = None) -> list[str]:
        """清除领奖保留期已过的已结束活动，返回被清理的eid列表。"""
        now_utc = _as_utc(now or datetime.now(UTC))
        removed = []
        for eid, state in list(self.events.items()):
            if (state.status == "ended" and state.reward_claim_deadline
                    and now_utc > state.reward_claim_deadline):
                del self.events[eid]
                self._conn.execute("DELETE FROM events WHERE eid=?", (eid,))
                self._conn.execute("DELETE FROM player_progress WHERE eid=?", (eid,))
                self._conn.execute("DELETE FROM rewards_claimed WHERE eid=?", (eid,))
                removed.append(eid)
        self._conn.commit()
        return removed

    # ---------- 任务 ----------

    def register_tasks(self, eid: str, task_ids) -> bool:
        state = self.events.get(eid)
        if not state:
            return False
        state.task_ids |= set(task_ids)
        self._persist_event(state)
        return True

    def update_task(self, eid: str, pid: str, task_id: str) -> bool:
        state = self.events.get(eid)
        if not state:
            return False
        # 任务必须属于当前活动，防止用其他活动的任务刷分
        if task_id not in state.task_ids:
            return False
        state.player_progress.setdefault(pid, {})[task_id] = True
        self._persist_event(state)
        return True

    # ---------- 排名 ----------

    def calculate_ranking(self, scores: dict) -> list:
        """密集排名：同分并列，名次不跳号。返回 [(pid, score, rank), ...]"""
        ranking = []
        rank = 0
        prev_score = None
        for pid, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
            if score != prev_score:
                rank += 1
                prev_score = score
            ranking.append((pid, score, rank))
        return ranking

    # ---------- 时间 ----------

    def check_schedule(self, eid: str, now: datetime, timezone: str = "UTC") -> bool:
        """判断活动是否处于开启状态。内部一律用UTC比较；naive的now按配置时区解释，
        时区转换走zoneinfo，夏令时切换不会偏移。"""
        event = self.events.get(eid)
        if not event or event.status != "active":
            return False
        now_utc = _as_utc(now, timezone)
        if event.start_time and now_utc < event.start_time:
            return False
        if event.end_time and now_utc > event.end_time:
            return False
        return True

    def get_local_schedule(self, eid: str, timezone: str):
        """把活动窗口转换成玩家所在时区的本地时间，供前端展示。"""
        event = self.events.get(eid)
        if not event:
            return None
        tz = ZoneInfo(timezone)
        start = event.start_time.astimezone(tz) if event.start_time else None
        end = event.end_time.astimezone(tz) if event.end_time else None
        return start, end
