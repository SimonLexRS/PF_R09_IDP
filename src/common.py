"""Utilidades compartidas por las tres funciones Lambda (ingest, worker, review).

- Logs estructurados en JSON (una línea por evento) -> CloudWatch Logs Insights.
- Métricas con Embedded Metric Format (EMF): se publican en CloudWatch
  sin llamadas extra a PutMetricData (sin costo de API ni latencia).
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP/R09")

# Estados del ciclo de vida de un documento
QUEUED = "QUEUED"            # recibido y encolado en SQS
REJECTED = "REJECTED"        # rechazado en la ingesta (tipo/tamaño no válido)
PROCESSING = "PROCESSING"    # el worker lo está procesando
RETRYING = "RETRYING"        # error transitorio, SQS lo reintentará
PROCESSED = "PROCESSED"      # todos los campos con confianza >= umbral y validaciones OK
NEEDS_REVIEW = "NEEDS_REVIEW"  # al menos un campo con baja confianza o validación fallida
REVIEWED = "REVIEWED"        # corregido/aprobado por una persona
FAILED = "FAILED"            # error permanente o reintentos agotados (va a la DLQ)

TERMINAL = {REJECTED, PROCESSED, NEEDS_REVIEW, REVIEWED, FAILED}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(level: str, msg: str, **kw) -> None:
    rec = {"ts": now_iso(), "level": level, "msg": msg, "fn": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local")}
    rec.update(kw)
    print(json.dumps(rec, default=str, ensure_ascii=False))


def emit_metrics(service: str, metrics: dict, unit_map: dict | None = None, **props) -> None:
    """Publica métricas EMF. metrics = {"Nombre": valor}."""
    if not metrics:
        return
    unit_map = unit_map or {}
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": k, "Unit": unit_map.get(k, "Count")} for k in metrics],
            }],
        },
        "Service": service,
    }
    payload.update({k: float(v) for k, v in metrics.items()})
    payload.update(props)
    print(json.dumps(payload, default=str))


def to_dynamo(obj):
    """Convierte floats a Decimal (DynamoDB no acepta float)."""
    if isinstance(obj, float):
        return Decimal(str(round(obj, 4)))
    if isinstance(obj, dict):
        return {k: to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_dynamo(v) for v in obj]
    return obj


def from_dynamo(obj):
    """Convierte Decimal a int/float para serializar a JSON."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_dynamo(v) for v in obj]
    return obj
