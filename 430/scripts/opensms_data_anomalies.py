"""Record-level exclusions only; never catches transport or write failures."""
from collections import Counter
import re

DATA_ANOMALY_POLICY = "skip_row_data_anomalies_v1"


def source_anomaly(row_number, values, reason, phone):
    return {"sourceRow": row_number, "phone": phone,
            "slot": str(values.get("卡槽") or "").strip(),
            "sourceCarrierName": str(values.get("运营商") or "").strip(), "reason": reason}


def anomaly_fields(rows, evidence_path):
    return {"dataAnomalyPolicy": DATA_ANOMALY_POLICY,
            "dataAnomalySkippedCount": len(rows), "dataAnomalySkippedRows": rows,
            "dataAnomalyCountsByReason": dict(Counter(row["reason"] for row in rows)),
            "dataAnomalyEvidencePath": str(evidence_path.resolve())}


def anomaly_summary(payload):
    """Keep chat summaries compact; detailed records stay in bound evidence."""
    return {key: payload[key] for key in (
        "dataAnomalyPolicy", "dataAnomalySkippedCount", "dataAnomalyCountsByReason", "dataAnomalyEvidencePath"
    ) if key in payload}


def assert_anomaly_identity_separation(anomalies, candidates):
    """A bad duplicate cannot be hidden while its other row is being written."""
    def key(value):
        text = str(value or '').strip()
        return text[-10:] if re.fullmatch(r'1?\d{10}', text) else text
    actionable = {key(row.get('电话') or row.get('phone') or row.get('cardNo')) for row in candidates}
    anomalous = [key(row['phone']) for row in anomalies if key(row['phone'])]
    if actionable.intersection(anomalous) or len(anomalous) != len(set(anomalous)):
        raise ValueError('异常记录与其它来源出现重复号码身份，停止，不以异常跳过掩盖冲突')
