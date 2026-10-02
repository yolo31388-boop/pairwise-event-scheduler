"""限时活动调度修复的验证测试"""
import sys, os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from event.scheduler import EventScheduler, EventState, UTC


class TestProgressPersistence:
    def test_progress_survives_restart(self, tmp_path):
        db = str(tmp_path / "events.db")
        es = EventScheduler(db)
        es.start_event("e1", task_ids={"t1", "t2"})
        assert es.update_task("e1", "p1", "t1")
        es.close()

        es2 = EventScheduler(db)
        assert es2.events["e1"].status == "active"
        assert es2.events["e1"].player_progress["p1"] == {"t1": True}
        assert es2.events["e1"].task_ids == {"t1", "t2"}
        es2.close()

    def test_state_change_persisted(self, tmp_path):
        db = str(tmp_path / "events.db")
        es = EventScheduler(db)
        es.start_event("e2", task_ids={"t1"})
        es.end_event("e2", datetime(2026, 1, 1, tzinfo=UTC))
        es.close()

        es2 = EventScheduler(db)
        assert es2.events["e2"].status == "ended"
        es2.close()


class TestUnclaimedRewards:
    def test_end_keeps_progress_and_rewards(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t1"})
        es.update_task("e1", "p1", "t1")
        es.end_event("e1", datetime(2026, 1, 1, tzinfo=UTC))
        assert es.events["e1"].player_progress["p1"] == {"t1": True}
        assert es.events["e1"].rewards_claimed["p1"] is False

    def test_claim_within_grace_period(self):
        es = EventScheduler(reward_grace=timedelta(days=7))
        es.start_event("e1", task_ids={"t1"})
        es.update_task("e1", "p1", "t1")
        end = datetime(2026, 1, 1, tzinfo=UTC)
        es.end_event("e1", end)
        assert es.claim_reward("e1", "p1", end + timedelta(days=6)) is True

    def test_claim_after_grace_period_fails(self):
        es = EventScheduler(reward_grace=timedelta(days=7))
        es.start_event("e1", task_ids={"t1"})
        es.update_task("e1", "p1", "t1")
        end = datetime(2026, 1, 1, tzinfo=UTC)
        es.end_event("e1", end)
        assert es.claim_reward("e1", "p1", end + timedelta(days=8)) is False

    def test_unclaimed_reward_survives_restart(self, tmp_path):
        db = str(tmp_path / "events.db")
        es = EventScheduler(db)
        es.start_event("e1", task_ids={"t1"})
        es.update_task("e1", "p1", "t1")
        es.end_event("e1", datetime(2026, 1, 1, tzinfo=UTC))
        es.close()
        es2 = EventScheduler(db)
        assert es2.claim_reward("e1", "p1", datetime(2026, 1, 2, tzinfo=UTC)) is True
        es2.close()

    def test_cleanup_after_deadline(self):
        es = EventScheduler(reward_grace=timedelta(days=7))
        es.start_event("e1", task_ids={"t1"})
        es.end_event("e1", datetime(2026, 1, 1, tzinfo=UTC))
        removed = es.cleanup_expired(datetime(2026, 1, 10, tzinfo=UTC))
        assert removed == ["e1"]
        assert "e1" not in es.events


class TestTaskOwnership:
    def test_cannot_complete_other_event_task(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t1", "t2"})
        es.start_event("e2", task_ids={"t3"})
        assert es.update_task("e1", "p1", "t3") is False
        assert "p1" not in es.events["e1"].player_progress

    def test_own_event_task_allowed(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t1"})
        assert es.update_task("e1", "p1", "t1") is True

    def test_unknown_task_rejected_for_empty_event(self):
        es = EventScheduler()
        es.events["e1"] = EventState("e1", status="active")
        assert es.update_task("e1", "p1", "task_from_e2") is False


class TestDenseRanking:
    def test_same_score_same_rank(self):
        es = EventScheduler()
        ranking = es.calculate_ranking({"a": 100, "b": 100, "c": 50})
        assert ranking[0][2] == 1
        assert ranking[1][2] == 1
        assert ranking[2][2] == 2  # 密集排名，不跳号

    def test_three_way_tie(self):
        es = EventScheduler()
        ranking = es.calculate_ranking({"a": 10, "b": 10, "c": 10})
        assert [r[2] for r in ranking] == [1, 1, 1]

    def test_sorted_descending(self):
        es = EventScheduler()
        ranking = es.calculate_ranking({"a": 1, "b": 3, "c": 2})
        assert [r[0] for r in ranking] == ["b", "c", "a"]


class TestTimezoneAware:
    NY = ZoneInfo("America/New_York")

    def test_schedule_with_configured_timezone(self):
        es = EventScheduler()
        es.events["e1"] = EventState("e1", status="active")
        assert es.check_schedule("e1", datetime.now(), "Asia/Shanghai") is True

    def test_window_utc_comparison(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t"},
                       start_time=datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                       end_time=datetime(2026, 6, 1, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
        # 上海9:00 = UTC1:00
        assert es.check_schedule("e1", datetime(2026, 6, 1, 1, 30, tzinfo=UTC)) is True
        assert es.check_schedule("e1", datetime(2026, 6, 1, 0, 30, tzinfo=UTC)) is False
        assert es.check_schedule("e1", datetime(2026, 6, 1, 3, 30, tzinfo=UTC)) is False

    def test_naive_now_interpreted_in_configured_tz(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t"},
                       start_time=datetime(2026, 6, 1, 9, 0, tzinfo=self.NY),
                       end_time=datetime(2026, 6, 1, 10, 0, tzinfo=self.NY))
        assert es.check_schedule("e1", datetime(2026, 6, 1, 9, 30),
                                 "America/New_York") is True
        assert es.check_schedule("e1", datetime(2026, 6, 1, 8, 30),
                                 "America/New_York") is False

    def test_dst_handled(self):
        es = EventScheduler()
        # 2026-03-08 是纽约夏令时切换日；切换后纽约9点对应UTC13点，而非冬天的14点
        es.start_event("e1", task_ids={"t"},
                       start_time=datetime(2026, 3, 9, 9, 0, tzinfo=self.NY),
                       end_time=datetime(2026, 3, 9, 10, 0, tzinfo=self.NY))
        assert es.events["e1"].start_time == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
        assert es.check_schedule("e1", datetime(2026, 3, 9, 13, 30, tzinfo=UTC)) is True
        assert es.check_schedule("e1", datetime(2026, 3, 9, 14, 30, tzinfo=UTC)) is False

    def test_local_schedule_display(self):
        es = EventScheduler()
        es.start_event("e1", task_ids={"t"},
                       start_time=datetime(2026, 6, 1, 1, 0, tzinfo=UTC),
                       end_time=datetime(2026, 6, 1, 3, 0, tzinfo=UTC))
        start, end = es.get_local_schedule("e1", "Asia/Shanghai")
        assert start.hour == 9
        assert end.hour == 11
