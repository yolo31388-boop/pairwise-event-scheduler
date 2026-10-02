"""限时活动调度

修复点：
1. 活动状态与玩家进度持久化到 SQLite，服务器重启后不丢失。
2. 活动结束后保留进度与奖励，进入领奖宽限期，宽限期内仍可领奖。
3. 完成任务时校验任务归属当前活动，且活动必须处于 active 状态。
4. 排名使用密集排名（dense rank），同分并列，下一名只递进一名。
5. 时间统一以 UTC 存储，按配置时区（zoneinfo）判定，自动正确处理夏令时。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

DEFAULT_GRACE_PERIOD = timedelta(days=7)


@dataclass
class EventState:
    eid: str
    status: str = "idle"  # idle/active/ended
    player_progress: dict = field(default_factory=dict)  # {pid: {task_id: True}}
    rewards_claimed: dict = field(default_factory=dict)  # {pid: bool}
    tasks: set = field(default_factory=set)  # 归属本活动的任务 id
    start_time: datetime | None = None  # UTC，活动窗口起点
    end_time: datetime | None = None  # UTC，活动窗口终点
    ended_at: datetime | None = None  # UTC，实际结束时间（领奖宽限期起点）


class EventScheduler:
    def __init__(self, db_path: str = ":memory:", default_timezone: str = "UTC",
                 grace_period: timedelta = DEFAULT_GRACE_PERIOD):
        self.db_path = db_path
        self.default_timezone = default_timezone
        self.grace_period = grace_period
        self.events: dict[str, EventState] = {}
        self._conn = sqlite3.connect(db_path)
        self._init_db()
        self._load()

    # ---------- persistence ----------

    def _init_db(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    eid TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS event_tasks (
                    eid TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    PRIMARY KEY (eid, task_id)
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
                """
            )

    @staticmethod
    def _fmt(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc).isoformat()

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        # 无时区信息的时间一律按 UTC 处理，不再依赖服务器本地时间
        if dt.tzinfo is None:
            return dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc)

    def _load(self) -> None:
        for eid, status, start, end, ended in self._conn.execute(
                "SELECT eid, status, start_time, end_time, ended_at FROM events"):
            self.events[eid] = EventState(
                eid=eid,
                status=status,
                start_time=self._parse(start),
                end_time=self._parse(end),
                ended_at=self._parse(ended),
            )
        for eid, task_id in self._conn.execute(
                "SELECT eid, task_id FROM event_tasks"):
            self.events.setdefault(eid, EventState(eid)).tasks.add(task_id)
        for eid, pid, task_id, completed in self._conn.execute(
                "SELECT eid, pid, task_id, completed FROM player_progress"):
            if completed:
                event = self.events.setdefault(eid, EventState(eid))
                event.player_progress.setdefault(pid, {})[task_id] = True
        for eid, pid, claimed in self._conn.execute(
                "SELECT eid, pid, claimed FROM rewards_claimed"):
            self.events.setdefault(eid, EventState(eid)).rewards_claimed[pid] = bool(claimed)

    def _save_event(self, event: EventState) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO events"
                " (eid, status, start_time, end_time, ended_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (event.eid, event.status, self._fmt(event.start_time),
                 self._fmt(event.end_time), self._fmt(event.ended_at)),
            )

    def _save_task(self, eid: str, task_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO event_tasks (eid, task_id) VALUES (?, ?)",
                (eid, task_id),
            )

    def _save_progress(self, eid: str, pid: str, task_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO player_progress"
                " (eid, pid, task_id, completed) VALUES (?, ?, ?, 1)",
                (eid, pid, task_id),
            )

    def _save_reward(self, eid: str, pid: str, claimed: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO rewards_claimed (eid, pid, claimed)"
                " VALUES (?, ?, ?)",
                (eid, pid, int(claimed)),
            )

    def close(self) -> None:
        self._conn.close()

    # ---------- event lifecycle ----------

    def start_event(self, eid: str, tasks=None,
                    start_time: datetime | None = None,
                    end_time: datetime | None = None) -> bool:
        event = self.events.setdefault(eid, EventState(eid))
        event.status = "active"
        if tasks:
            event.tasks.update(tasks)
        event.start_time = self._as_utc(start_time) if start_time else event.start_time
        event.end_time = self._as_utc(end_time) if end_time else event.end_time
        event.ended_at = None
        self._save_event(event)
        for task_id in event.tasks:
            self._save_task(eid, task_id)
        return True

    def register_task(self, eid: str, task_id: str) -> bool:
        event = self.events.get(eid)
        if event is None:
            return False
        event.tasks.add(task_id)
        self._save_task(eid, task_id)
        return True

    def end_event(self, eid: str) -> bool:
        # 结束只改变状态并记录结束时间：玩家进度与奖励记录全部保留，
        # 进入领奖宽限期，不再清空积分。
        event = self.events.get(eid)
        if event is None:
            return False
        event.status = "ended"
        event.ended_at = datetime.now(dt_timezone.utc)
        self._save_event(event)
        return True

    def claim_reward(self, eid: str, pid: str,
                     now: datetime | None = None) -> bool:
        event = self.events.get(eid)
        if event is None:
            return False
        current = self._as_utc(now) if now else datetime.now(dt_timezone.utc)
        if event.status == "ended":
            if event.ended_at is not None and current > event.ended_at + self.grace_period:
                return False  # 领奖宽限期已过
        elif event.status != "active":
            return False
        if pid not in event.player_progress and pid not in event.rewards_claimed:
            return False
        if event.rewards_claimed.get(pid, False):
            return False
        event.rewards_claimed[pid] = True
        self._save_reward(eid, pid, True)
        return True

    def cleanup_expired(self, now: datetime | None = None) -> int:
        # 删除已超过领奖宽限期的活动及其数据
        current = self._as_utc(now) if now else datetime.now(dt_timezone.utc)
        expired = [
            event.eid for event in self.events.values()
            if event.status == "ended" and event.ended_at is not None
            and current > event.ended_at + self.grace_period
        ]
        if not expired:
            return 0
        with self._conn:
            for eid in expired:
                self._conn.execute("DELETE FROM events WHERE eid = ?", (eid,))
                self._conn.execute("DELETE FROM event_tasks WHERE eid = ?", (eid,))
                self._conn.execute("DELETE FROM player_progress WHERE eid = ?", (eid,))
                self._conn.execute("DELETE FROM rewards_claimed WHERE eid = ?", (eid,))
                del self.events[eid]
        return len(expired)

    # ---------- tasks / ranking ----------

    def update_task(self, eid: str, pid: str, task_id: str) -> bool:
        event = self.events.get(eid)
        if event is None or event.status != "active":
            return False
        # 只能完成归属当前活动的任务，杜绝拿上一个活动的任务刷积分
        if task_id not in event.tasks:
            return False
        event.player_progress.setdefault(pid, {})[task_id] = True
        self._save_progress(eid, pid, task_id)
        return True

    def calculate_ranking(self, scores: dict) -> list:
        # 密集排名：同分并列同一名次，下一名次只 +1
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        ranking = []
        rank = 0
        previous_score = None
        for pid, score in ordered:
            if previous_score is None or score != previous_score:
                rank += 1
                previous_score = score
            ranking.append((pid, score, rank))
        return ranking

    # ---------- schedule ----------

    def check_schedule(self, eid: str, now: datetime,
                       timezone: str | None = None) -> bool:
        event = self.events.get(eid)
        if event is None or event.status != "active":
            return False
        tz_name = timezone or self.default_timezone
        # ZoneInfo 自带历史/未来时区规则与夏令时切换，本地时间永远不参与判断
        tz = ZoneInfo(tz_name)
        current_utc = self._as_utc(now)
        current_local = current_utc.astimezone(tz)
        if event.start_time is not None:
            start_local = event.start_time.astimezone(tz)
            if current_local < start_local:
                return False
        if event.end_time is not None:
            end_local = event.end_time.astimezone(tz)
            if current_local > end_local:
                return False
        return True

    # 描述性长名别名
    update_event_task = update_task
    calculate_event_ranking = calculate_ranking
    check_event_schedule = check_schedule
