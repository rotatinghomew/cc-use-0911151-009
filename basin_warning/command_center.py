from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from basin_warning.observations.hydro_event import ContractError, Decision, DomainRecord, records_from_fixture, stable_hash
from basin_warning.replay.timeline_store import Repository
from basin_warning.alerts.escalation import advance_for_outcome, decide
from basin_warning.review.incident_report import export_bundle, verify_bundle
from basin_warning.fusion.watermark import alert_assessment, derive_context, event_watermark


class Application:
    def __init__(self, database: str | Path, context: Mapping[str, Any] | None = None):
        self.repository = Repository(database)
        self.context = dict(context or {})

    def ingest(self, record: DomainRecord) -> DomainRecord:
        current = self.repository.get_record(record.record_id)
        if current is None:
            self.repository.save_record(record)
            return record
        if current.fingerprint == record.fingerprint:
            return current
        if current.payload == record.payload and current.source == record.source:
            # 重复上报/重放：观测内容一致，平台已管理的版本与状态保持不变
            return current
        replacement = DomainRecord(record.record_id, record.payload, current.state,
                                   current.version + 1, record.occurred_at, record.source)
        self.repository.save_record(replacement, expected_version=current.version)
        return replacement

    def process(self, record: DomainRecord) -> tuple[DomainRecord, Decision]:
        current = self.repository.get_record(record.record_id)
        stored = self.ingest(record)
        replayed = current is not None and stored.fingerprint == current.fingerprint
        if replayed:
            latest = self.repository.latest_decision(stored.record_id)
            if latest is not None:
                # 重放/重启：直接回放既有处置，不重复决定、不重复推进状态
                return stored, latest
        elif current is not None:
            # 迟到更正：仍绑定旧观测版本的待发通知按同一依据重评处理；
            # 已送达/已确认事实由台账保留
            self.repository.supersede_pending_notices(stored.record_id, stored.version)
        context = dict(self.context)
        context.update(derive_context(stored, self.repository))
        decision = decide(stored, context)
        advanced = advance_for_outcome(stored, decision)
        self.repository.save_disposition(decision, advanced, expected_version=stored.version)
        return advanced, decision

    def run_fixture(self, fixture: Mapping[str, Any], output: str | Path) -> dict[str, Any]:
        records = records_from_fixture(fixture)
        fixture_hash = stable_hash(fixture)
        run_id = self.repository.begin_run(fixture_hash, len(records))
        completed = self.repository.completed_run(run_id)
        if completed is not None:
            return completed
        decisions: dict[str, Decision] = {}
        final_records: list[DomainRecord] = []
        for record in records:
            final_record, decision = self.process(record)
            final_records.append(final_record)
            decisions[record.record_id] = decision
        snapshot = self.repository.snapshot()
        if snapshot["chain_errors"]:
            raise ContractError("audit chain verification failed")
        manifest = export_bundle(output, final_records, decisions, snapshot, self.repository.event_rows())
        result = {"run_id": run_id, "fixture_hash": fixture_hash, "records": len(final_records),
                  "decisions": len(decisions), "manifest": manifest, "snapshot": snapshot}
        self.repository.finish_run(run_id, result)
        return result

    def inspect(self) -> dict[str, Any]:
        return {"snapshot": self.repository.snapshot(), "records": [record.__dict__ for record in self.repository.list_records()],
                "decisions": self.repository.decision_rows()}

    def assess(self) -> dict[str, Any]:
        """当前全部观测的值班员视角告警意见（等级/证据状态/可信度/建议）。"""
        return alert_assessment(event_watermark(self.repository.list_records()))

    def reach_notices(self, reach_id: str) -> list[dict[str, Any]]:
        return self.repository.reach_notices(reach_id)


def load_fixture(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read fixture: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("fixture root must be an object")
    return value


def run(fixture_path: str | Path, database: str | Path, output: str | Path,
        context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return Application(database, context).run_fixture(load_fixture(fixture_path), output)


def verify(database: str | Path, output: str | Path) -> dict[str, Any]:
    repository = Repository(database)
    chain_errors = repository.verify_chain()
    bundle_errors = verify_bundle(output)
    return {"ok": not chain_errors and not bundle_errors, "chain_errors": chain_errors,
            "bundle_errors": bundle_errors, "snapshot": repository.snapshot()}
