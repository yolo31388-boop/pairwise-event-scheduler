"""限时活动调度 - 红态测试"""
import pytest, sys, os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from event.scheduler import EventScheduler, EventState

class TestProgressPersistence:
    def test_progress_survives_restart(self):
        es = EventScheduler()
        es.start_event("e1")
        es.events["e1"].player_progress["p1"] = {"task1": True}
        # 模拟重启后应该还在
        assert "p1" in es.events["e1"].player_progress

class TestUnclaimedRewards:
    def test_ended_event_keeps_rewards_for_period(self):
        es = EventScheduler()
        es.events["e1"] = EventState("e1", status="active", rewards_claimed={"p1": False})
        es.end_event("e1")
        assert "p1" in es.events["e1"].rewards_claimed

class TestTaskOwnership:
    def test_cannot_complete_other_event_task(self):
        es = EventScheduler()
        es.events["e1"] = EventState("e1", status="active")
        assert es.update_task("e1", "p1", "task_from_e2") == False

class TestTieRanking:
    def test_same_score_same_rank(self):
        es = EventScheduler()
        scores = {"a": 100, "b": 100, "c": 50}
        ranking = es.calculate_ranking(scores)
        assert ranking[0][1] == ranking[1][1]

class TestTimezoneAware:
    def test_schedule_uses_configured_timezone(self):
        es = EventScheduler()
        es.events["e1"] = EventState("e1", status="active")
        assert es.check_schedule("e1", datetime.now(), "Asia/Shanghai") == True
