from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basin_warning.observations.hydro_event import DomainRecord, stable_hash
from basin_warning.fusion.conflict_resolution import RULE_INDEX, evaluate, summarize
from basin_warning.fusion.watermark import (EVIDENCE_INSUFFICIENT, EVIDENCE_LINK_FAILED, EVIDENCE_TRUSTED,
                                            alert_assessment, alert_level, derive_context, event_watermark,
                                            station_signal)
from basin_warning.alerts.escalation import decide
from basin_warning.command_center import Application
from basin_warning.replay.timeline_store import Repository


def hydro_record(record_id: str, *, station: str | None = None, age: float = 10.0,
                 quality: float = 0.9, water: float = 12.0, state: str = "observing",
                 version: int = 1, occurred_at: str = "2026-11-16T00:00:00+00:00") -> DomainRecord:
    return DomainRecord(record_id, {
        "station_id": station or record_id, "rainfall_mm": 210.0, "water_level_m": water,
        "flow_rate": 1700.0, "gate_opening": 0.25, "forecast_horizon_h": 36.0,
        "sensor_age_min": age, "quality_flag": quality, "upstream_delay": 2.0,
        "population_exposure": 60000, "delivery_channels": 3, "ack_rate": 0.8},
        state=state, version=version, occurred_at=occurred_at, source="test-harness")


def stale_record(record_id: str, **kwargs) -> DomainRecord:
    kwargs.setdefault("age", 240.0)
    kwargs.setdefault("quality", 0.0)
    return hydro_record(record_id, **kwargs)


class StaticRepository:
    def __init__(self, rows: list[DomainRecord]):
        self.rows = rows

    def list_records(self) -> list[DomainRecord]:
        return self.rows


class EvidenceStatusTests(unittest.TestCase):
    def test_all_expired_zero_quality_is_link_failure_with_zero_confidence(self):
        records = [stale_record(f"station-{index}") for index in range(3)]
        watermark = event_watermark(records)
        self.assertEqual(watermark["evidence"], EVIDENCE_LINK_FAILED)
        self.assertEqual(watermark["confidence"], 0.0)
        self.assertEqual(watermark["usable_stations"], 0)
        # 保守保留最高风险提示，不把失效当成低风险
        self.assertEqual(watermark["risk"], max(station_signal(row).risk for row in records))
        self.assertGreater(watermark["risk"], 0.0)

    def test_valid_stations_are_trusted(self):
        records = [hydro_record(f"station-{index}") for index in range(3)]
        watermark = event_watermark(records)
        self.assertEqual(watermark["evidence"], EVIDENCE_TRUSTED)
        self.assertGreater(watermark["confidence"], 0.5)
        self.assertEqual(watermark["usable_stations"], 3)

    def test_missing_or_sparse_observations_are_insufficient(self):
        self.assertEqual(event_watermark([])["evidence"], EVIDENCE_INSUFFICIENT)
        mixed = [hydro_record("ok-0")] + [stale_record(f"dead-{index}") for index in range(3)]
        watermark = event_watermark(mixed)
        self.assertEqual(watermark["evidence"], EVIDENCE_INSUFFICIENT)
        self.assertEqual(watermark["usable_stations"], 1)

    def test_assessment_distinguishes_three_situations_for_duty_officer(self):
        void = alert_assessment(event_watermark([stale_record("a"), stale_record("b")]))
        valid = alert_assessment(event_watermark([hydro_record("a"), hydro_record("b")]))
        empty = alert_assessment(event_watermark([]))
        self.assertFalse(void["trustworthy"])
        self.assertEqual(void["evidence"], EVIDENCE_LINK_FAILED)
        self.assertIn("无可靠观测", void["advisory"])
        self.assertTrue(valid["trustworthy"])
        self.assertEqual(empty["evidence"], EVIDENCE_INSUFFICIENT)
        # 保守：链路失效时等级不降级为低风险，但也不标记为可信
        self.assertEqual(void["level"], alert_level(event_watermark([stale_record("a"), stale_record("b")])))

    def test_fusion_confidence_weight_collapses_without_reliable_observations(self):
        valid_repo = StaticRepository([hydro_record(f"s{index}") for index in range(3)])
        void_repo = StaticRepository([stale_record(f"s{index}") for index in range(3)])
        valid_context = derive_context(hydro_record("target"), valid_repo)
        void_context = derive_context(stale_record("target"), void_repo)
        self.assertGreater(valid_context["fusion_confidence_weight"], 0.5)
        self.assertEqual(void_context["fusion_confidence_weight"], 0.0)
        self.assertEqual(void_context["evidence_status"], EVIDENCE_LINK_FAILED)
        self.assertEqual(void_context["evidence_confidence"], 0.0)

    def test_decide_never_approves_on_untrusted_evidence(self):
        void_repo = StaticRepository([stale_record(f"s{index}") for index in range(3)])
        context = derive_context(stale_record("target"), void_repo)
        context["approve_threshold"] = 0.0  # 即使分数门槛被人为放低也不得批准
        decision = decide(stale_record("target"), context)
        # 既不作为可信批准，也不当作低风险拒掉：保守保留在处置队列中
        self.assertIn(decision.outcome, {"review", "hold"})
        self.assertIn("evidence=link_failed", decision.reasons)
        self.assertTrue(any("无可靠观测" in reason for reason in decision.reasons))

    def test_decide_forces_review_when_evidence_untrusted_and_rules_clean(self):
        probe = hydro_record("probe")
        context: dict[str, object] = {}
        for name, rule in RULE_INDEX.items():
            key = f"{name}_threshold"
            context[key] = -1.0e9 if rule(probe, {key: -1.0e9}).passed else 1.0e9
        self.assertEqual(summarize(evaluate(probe, context))["critical"], 0)
        context.update(derive_context(probe, StaticRepository([stale_record(f"s{i}") for i in range(3)])))
        context["approve_threshold"] = 0.0
        decision = decide(probe, context)
        self.assertEqual(decision.outcome, "review")
        self.assertIn("evidence=link_failed", decision.reasons)

    def test_decide_defaults_to_trusted_without_fusion_context(self):
        decision = decide(hydro_record("plain"))
        self.assertIn("evidence=trusted", decision.reasons)


class ReevaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "state.sqlite3"
        self.application = Application(self.database)

    def test_late_correction_reevaluates_with_same_basis(self):
        first_record, first = self.application.process(stale_record("obs-1"))
        # 证据不可信：保守保留处置，但绝不批准
        self.assertIn(first.outcome, {"review", "hold"})
        self.assertIn("evidence=link_failed", first.reasons)
        # 站点恢复 + 迟到更正：同一 record_id、内容更新
        correction = hydro_record("obs-1", age=5.0, quality=0.95)
        record, second = self.application.process(correction)
        self.assertEqual(record.version, first_record.version + 1)
        self.assertIn("evidence=trusted", second.reasons)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        # 两次评估都留存，重评依据同一套融合上下文
        self.assertEqual(self.application.repository.snapshot()["counts"]["decisions"], 2)
        self.assertEqual(self.application.assess()["evidence"], EVIDENCE_TRUSTED)

    def test_partial_recovery_changes_evidence_status(self):
        for index in range(4):
            self.application.process(stale_record(f"obs-{index}"))
        self.assertEqual(self.application.assess()["evidence"], EVIDENCE_LINK_FAILED)
        for index in range(3):
            self.application.process(hydro_record(f"obs-{index}", age=5.0, quality=0.95))
        assessment = self.application.assess()
        self.assertEqual(assessment["evidence"], EVIDENCE_TRUSTED)
        self.assertGreater(assessment["confidence"], 0.0)

    def test_pending_notice_superseded_but_delivered_and_acknowledged_retained(self):
        repository = self.application.repository
        self.application.process(stale_record("obs-1"))
        repository.register_reach("downstream", None, 0.0)
        repository.assign_station("station-x", "downstream")
        repository.issue_notice("notice-pending", "downstream", "obs-1", 1)
        repository.issue_notice("notice-delivered", "downstream", "obs-1", 1)
        repository.update_notice("notice-delivered", "delivered")
        # 迟到更正产生版本 2：待发通知按同一依据重评处理，已送达事实保留
        self.application.process(hydro_record("obs-1", age=5.0, quality=0.95))
        notices = {row["notice_id"]: row for row in repository.reach_notices("downstream")}
        self.assertEqual(notices["notice-pending"]["status"], "cancelled")
        self.assertEqual(notices["notice-delivered"]["status"], "delivered")
        self.assertIsNotNone(notices["notice-delivered"]["delivered_at"])
        # 依据新版本重新签发，随后送达并确认；再次更正不会触碰已确认事实
        repository.issue_notice("notice-v2", "downstream", "obs-1", 2)
        repository.update_notice("notice-v2", "delivered")
        repository.update_notice("notice-v2", "acknowledged")
        repository.issue_notice("notice-v2-pending", "downstream", "obs-1", 2)
        self.application.process(hydro_record("obs-1", age=8.0, quality=0.9, water=11.0))
        notices = {row["notice_id"]: row for row in repository.reach_notices("downstream")}
        self.assertEqual(notices["notice-v2"]["status"], "acknowledged")
        self.assertIsNotNone(notices["notice-v2"]["acknowledged_at"])
        self.assertEqual(notices["notice-v2-pending"]["status"], "cancelled")
        self.assertEqual(repository.pending_notices("downstream"), [])


class ReplaySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "state.sqlite3"

    def test_duplicate_report_does_not_duplicate_disposition(self):
        application = Application(self.database)
        record = hydro_record("obs-1")
        first_record, first_decision = application.process(record)
        for _ in range(3):
            replayed_record, replayed_decision = application.process(record)
            self.assertEqual(replayed_record.fingerprint, first_record.fingerprint)
            self.assertEqual(replayed_decision.as_dict()["outcome"], first_decision.outcome)
        snapshot = application.repository.snapshot()
        self.assertEqual(snapshot["counts"]["decisions"], 1)
        self.assertEqual(snapshot["counts"]["records"], 1)
        self.assertEqual(application.repository.get_record("obs-1").version, first_record.version)

    def test_service_restart_replays_without_contradiction(self):
        first_state, first_decision = Application(self.database).process(hydro_record("obs-1"))
        restarted = Application(self.database)
        second_state, second_decision = restarted.process(hydro_record("obs-1"))
        self.assertEqual(second_state.state, first_state.state)
        self.assertEqual(second_state.version, first_state.version)
        self.assertEqual(second_decision.outcome, first_decision.outcome)
        snapshot = restarted.repository.snapshot()
        self.assertEqual(snapshot["counts"]["decisions"], 1)
        self.assertEqual(snapshot["counts"]["records"], 1)

    def test_unfinished_run_reprocessing_is_idempotent(self):
        fixture = {"records": [
            {"record_id": f"obs-{index}", "state": "observing", "source": "test-harness",
             "occurred_at": "2026-11-16T00:00:00+00:00",
             "payload": hydro_record(f"obs-{index}").payload}
            for index in range(3)]}
        records = [DomainRecord.from_dict(item) for item in fixture["records"]]
        # 模拟崩溃：run 已开始、首条记录已处置，随后服务重启
        crashed = Application(self.database)
        crashed.repository.begin_run(stable_hash(fixture), len(records))
        crashed.process(records[0])
        output = Path(self.temp.name) / "output"
        resumed = Application(self.database).run_fixture(fixture, output)
        # 与一次性干净跑完的结果完全一致：无重复处置、无矛盾推进
        clean_database = Path(self.temp.name) / "clean.sqlite3"
        clean = Application(clean_database).run_fixture(fixture, Path(self.temp.name) / "clean-output")
        self.assertEqual(resumed["run_id"], clean["run_id"])
        self.assertEqual(Application(self.database).repository.snapshot()["counts"],
                         Application(clean_database).repository.snapshot()["counts"])
        self.assertEqual(Application(self.database).repository.snapshot()["states"],
                         Application(clean_database).repository.snapshot()["states"])
        again = Application(self.database).run_fixture(fixture, output)
        self.assertEqual(again["run_id"], resumed["run_id"])

    def test_conflicting_replay_is_still_rejected(self):
        repository = Repository(self.database)
        repository.save_record(hydro_record("obs-1"))
        with self.assertRaisesRegex(Exception, "version"):
            repository.save_record(hydro_record("obs-1", version=1, water=1.0))


if __name__ == "__main__":
    unittest.main()
