"""限时活动调度 - 含5个bug"""
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class EventState:
    eid: str
    status: str = "idle"  # idle/active/ended
    player_progress: dict = field(default_factory=dict)  # bug1: 内存存储
    rewards_claimed: dict = field(default_factory=dict)

class EventScheduler:
    def __init__(self):
        self.events: dict[str, EventState] = {}

    def start_event(self, eid: str) -> bool:
        # bug1: 状态存内存
        if eid not in self.events:
            self.events[eid] = EventState(eid, status="active")
        return True

    def end_event(self, eid: str) -> bool:
        # bug2: 不处理未领奖励
        if eid in self.events:
            self.events[eid].status = "ended"
            self.events[eid].player_progress.clear()
        return True

    def update_task(self, eid: str, pid: str, task_id: str) -> bool:
        # bug3: 不检查任务归属
        if eid not in self.events:
            return False
        if pid not in self.events[eid].player_progress:
            self.events[eid].player_progress[pid] = {}
        self.events[eid].player_progress[pid][task_id] = True
        return True

    def calculate_ranking(self, scores: dict) -> list:
        # bug4: 同分用行号
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def check_schedule(self, eid: str, now: datetime, timezone: str = "UTC") -> bool:
        # bug5: 用本地时间
        event = self.events.get(eid)
        if not event:
            return False
        return event.status == "active"
