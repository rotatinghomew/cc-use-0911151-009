from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from basin_warning.observations.hydro_event import DomainRecord, bounded, finite_number

EVIDENCE_TRUSTED = "trusted"
EVIDENCE_INSUFFICIENT = "insufficient"
EVIDENCE_LINK_FAILED = "link_failed"
USABLE_COVERAGE_FLOOR = 0.5


@dataclass(frozen=True)
class StationSignal:
    station_id: str
    risk: float
    freshness: float
    quality: float
    lead_time: float

    @property
    def reliability(self) -> float:
        """单站证据可靠度：新鲜度或质量任一归零，该站观测即不可作为证据。"""
        return bounded(self.freshness * self.quality)

    @property
    def usable(self) -> bool:
        return self.freshness > 0.0 and self.quality > 0.0


def station_signal(record: DomainRecord) -> StationSignal:
    rainfall = bounded(finite_number(record.payload.get("rainfall_mm", 0.0)) / 250.0)
    water = bounded(finite_number(record.payload.get("water_level_m", 0.0)) / 15.0)
    flow = bounded(finite_number(record.payload.get("flow_rate", 0.0)) / 2500.0)
    opening = bounded(finite_number(record.payload.get("gate_opening", 0.0)))
    age = max(0.0, finite_number(record.payload.get("sensor_age_min", 0.0)))
    quality_flag = bounded(finite_number(record.payload.get("quality_flag", 0.0)))
    freshness = bounded(1.0 - age / 180.0)
    risk = bounded(rainfall * 0.30 + water * 0.35 + flow * 0.25 + (1.0 - opening) * 0.10)
    lead = max(0.0, finite_number(record.payload.get("forecast_horizon_h", 0.0)) - finite_number(record.payload.get("upstream_delay", 0.0)))
    return StationSignal(str(record.payload.get("station_id", record.record_id)), risk, freshness, quality_flag, lead)


def evidence_status(total: int, usable: int) -> str:
    """区分可信证据、数据不足与采集链路失效，供值班员判读。"""
    if total <= 0:
        return EVIDENCE_INSUFFICIENT
    if usable <= 0:
        return EVIDENCE_LINK_FAILED
    if usable / total < USABLE_COVERAGE_FLOOR:
        return EVIDENCE_INSUFFICIENT
    return EVIDENCE_TRUSTED


def event_watermark(records: Iterable[DomainRecord]) -> dict[str, Any]:
    signals = [station_signal(row) for row in records]
    total = len(signals)
    if not signals:
        return {"risk": 0.0, "freshness": 0.0, "agreement": 0.0, "lead_time": 0.0,
                "confidence": 0.0, "stations": 0, "usable_stations": 0, "coverage": 0.0,
                "evidence": EVIDENCE_INSUFFICIENT}
    risks = [item.risk for item in signals]
    agreement = bounded(1.0 - (statistics.pstdev(risks) if total > 1 else 0.0))
    weights = sum(item.reliability for item in signals)
    usable = sum(1 for item in signals if item.usable)
    weighted = sum(item.risk * item.reliability for item in signals)
    # 保守原则：没有任何可靠观测时仍保留最高风险提示——既不把采集失效当成低风险，
    # 也不把它当作新的可信证据（confidence 归零、evidence 标记为链路失效）。
    fused_risk = weighted / weights if weights else max(risks)
    return {"risk": fused_risk,
            "freshness": statistics.fmean(item.freshness for item in signals),
            "agreement": agreement,
            "lead_time": min(item.lead_time for item in signals),
            "confidence": bounded(weights / total),
            "stations": total,
            "usable_stations": usable,
            "coverage": usable / total,
            "evidence": evidence_status(total, usable)}


def alert_level(watermark: Mapping[str, Any], previous: str = "observing") -> str:
    risk = watermark["risk"] * (0.7 + watermark["agreement"] * 0.3)
    thresholds = [(0.80, "critical"), (0.60, "warning"), (0.40, "watch")]
    target = next((name for threshold, name in thresholds if risk >= threshold), "observing")
    order = ["observing", "watch", "warning", "critical"]
    if previous in order and order.index(target) < order.index(previous) and watermark["freshness"] < 0.75:
        return previous
    return target


def alert_assessment(watermark: Mapping[str, Any], previous: str = "observing") -> dict[str, Any]:
    """值班员视角的告警意见：等级、证据状态、可信度与处置建议。

    证据不可信时等级按保守原则保留，但明确标注不得作为新的可信证据。
    """
    level = alert_level(watermark, previous)
    evidence = str(watermark.get("evidence", EVIDENCE_TRUSTED))
    confidence = bounded(watermark.get("confidence", 1.0))
    if evidence == EVIDENCE_LINK_FAILED:
        advisory = ("采集链路失效：观测全部过期或质量为零，已无可靠观测；"
                    "风险提示为保守保留，不得作为新的可信证据，请按链路故障处置并复核")
    elif evidence == EVIDENCE_INSUFFICIENT:
        advisory = ("数据不足：可用观测不足以支撑可信融合，当前风险仅供参考，"
                    "需补充观测后按同一依据重评")
    else:
        advisory = "证据可信：融合风险基于有效站点观测"
    return {"level": level, "evidence": evidence, "confidence": confidence,
            "risk": bounded(watermark.get("risk", 0.0)),
            "trustworthy": evidence == EVIDENCE_TRUSTED, "advisory": advisory}


def delivery_risk(record: DomainRecord) -> float:
    exposure = bounded(finite_number(record.payload.get("population_exposure", 0.0)) / 100000.0)
    channels = max(1.0, finite_number(record.payload.get("delivery_channels", 1.0)))
    acknowledgements = bounded(finite_number(record.payload.get("ack_rate", 0.0)))
    return bounded(exposure * 0.5 + (1.0 - acknowledgements) * 0.4 + 0.1 / channels)


def derive_context(record: DomainRecord, repository: Any) -> dict[str, Any]:
    rows = repository.list_records() or [record]
    watermark = event_watermark(rows)
    assessment = alert_assessment(watermark)
    level = assessment["level"]
    rank = {"observing": 0.2, "watch": 0.4, "warning": 0.7, "critical": 1.0}[level]
    delivery = delivery_risk(record)
    confidence = assessment["confidence"]
    return {"fusion_confidence_weight": (0.6 + watermark["agreement"] * 0.5) * confidence,
            "lead_time_weight": 0.6 + bounded(watermark["lead_time"] / 72.0) * 0.5,
            "miss_risk_weight": 0.7 + rank * 0.6, "delivery_reach_weight": 1.1 - delivery * 0.4,
            "station_disagreement_weight": 1.2 - watermark["agreement"] * 0.4,
            "evidence_status": assessment["evidence"],
            "evidence_confidence": confidence,
            "alert_level": level,
            "alert_advisory": assessment["advisory"]}
