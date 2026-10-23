"""Lambda worker: consume la cola SQS, llama a Textract AnalyzeExpense y guarda
los campos, la confianza y las banderas de validación en DynamoDB.

Manejo de errores (clave para la defensa):
- Error PERMANENTE (documento inválido para Textract): estado FAILED, NO se reintenta.
- Error TRANSITORIO (throttling, error interno, fallo inyectado): estado RETRYING y se
  devuelve el mensaje como fallido (ReportBatchItemFailures). SQS lo reintenta;
  tras maxReceiveCount (3) pasa a la DLQ y el documento queda FAILED.
- Idempotencia: actualización condicional del estado; un mensaje repetido de un
  documento ya terminado se descarta.
"""
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

import extraction
from common import (FAILED, NEEDS_REVIEW, PROCESSED, PROCESSING, QUEUED, RETRYING, emit_metrics, log, now_iso,
                    to_dynamo)

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]
THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "90"))
MIN_CORROBORATED = float(os.environ.get("MIN_CORROBORATED_CONFIDENCE", "70"))
MAX_RECEIVE = int(os.environ.get("MAX_RECEIVE_COUNT", "3"))
FAULT_PREFIX = os.environ.get("FAULT_INJECTION_PREFIX", "")  # p. ej. "incoming/chaos-" para demostrar la DLQ
QUEUE_URL = os.environ.get("QUEUE_URL", "")
BACKOFF_BASE = int(os.environ.get("RETRY_BACKOFF_SECONDS", "10"))

PERMANENT_ERRORS = {
    "UnsupportedDocumentException", "BadDocumentException", "DocumentTooLargeException",
    "InvalidS3ObjectException", "InvalidParameterException",
}

textract = boto3.client("textract")
s3 = boto3.client("s3")
sqs = boto3.client("sqs")
table = boto3.resource("dynamodb").Table(TABLE)


class PermanentError(Exception):
    pass


def set_status(doc_id: str, status: str, detail: str, allowed_from: list[str] | None = None, extra: dict | None = None):
    ts = now_iso()
    names = {"#s": "status"}
    values = {":s": status, ":u": ts, ":h": [{"at": ts, "status": status, "by": "worker", "detail": detail}], ":e": []}
    sets = ["#s = :s", "updatedAt = :u", "history = list_append(if_not_exists(history, :e), :h)"]
    for i, (k, v) in enumerate((extra or {}).items()):
        names[f"#x{i}"] = k
        values[f":x{i}"] = to_dynamo(v)
        sets.append(f"#x{i} = :x{i}")
    kwargs = {"Key": {"docId": doc_id}, "UpdateExpression": "SET " + ", ".join(sets),
              "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}
    if allowed_from:
        values.update({f":a{i}": s for i, s in enumerate(allowed_from)})
        kwargs["ConditionExpression"] = "attribute_exists(docId) AND #s IN (" + ", ".join(f":a{i}" for i in range(len(allowed_from))) + ")"
    table.update_item(**kwargs)


def check_duplicate(doc_id: str, fields: dict) -> str | None:
    """Registra NIT+N° de factura; si ya existe en otro documento devuelve su docId."""
    nit = (fields.get("nit_emisor") or {}).get("normalized")
    num = (fields.get("numero_factura") or {}).get("normalized")
    if not (nit and num):
        return None
    key = f"DEDUP#{nit}#{num}"
    try:
        table.put_item(Item={"docId": key, "ownerDocId": doc_id, "createdAt": now_iso()},
                       ConditionExpression="attribute_not_exists(docId)")
        return None
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        owner = table.get_item(Key={"docId": key}).get("Item", {}).get("ownerDocId")
        return owner if owner and owner != doc_id else None


def process(msg: dict) -> dict:
    doc_id, bucket, key = msg["docId"], msg.get("bucket", BUCKET), msg["key"]

    if FAULT_PREFIX and key.startswith(FAULT_PREFIX):
        raise RuntimeError("Fallo transitorio inyectado (FAULT_INJECTION_PREFIX) para demostrar reintentos y DLQ")

    t0 = time.perf_counter()
    try:
        resp = textract.analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in PERMANENT_ERRORS:
            raise PermanentError(f"{code}: {e.response['Error'].get('Message', '')}") from e
        raise
    latency_ms = (time.perf_counter() - t0) * 1000

    parsed = extraction.parse_expense(resp)
    fields = parsed["fields"]
    flags = extraction.validate(fields, parsed["line_items"], THRESHOLD, min_corroborated=MIN_CORROBORATED)

    dup_of = check_duplicate(doc_id, fields)
    if dup_of:
        flags.append({"field": "numero_factura", "code": "DUPLICATE", "message": f"Misma factura (NIT + N°) que el documento {dup_of}"})
        fields["numero_factura"]["needsReview"] = True
        fields["numero_factura"].setdefault("reasons", []).append("DUPLICATE")

    conf = extraction.summarize_confidence(fields)
    status = NEEDS_REVIEW if flags else PROCESSED

    # JSON completo de Textract en S3 (trazabilidad; DynamoDB tiene límite de 400 KB por ítem)
    result_key = f"results/{doc_id}.json"
    s3.put_object(Bucket=bucket, Key=result_key, Body=json.dumps(resp).encode(), ContentType="application/json",
                  ServerSideEncryption="AES256")

    set_status(doc_id, status, f"{len(flags)} banderas; confianza media {conf['avg']}%", allowed_from=[PROCESSING], extra={
        "fields": fields,
        "lineItems": parsed["line_items"][:50],
        "flags": flags,
        "confidence": conf,
        "pages": parsed["pages"],
        "resultKey": result_key,
        "textractLatencyMs": round(latency_ms, 1),
        "threshold": THRESHOLD,
        "processedAt": now_iso(),
    })
    emit_metrics("worker", {
        "DocumentsProcessed": 1 if status == PROCESSED else 0,
        "DocumentsNeedsReview": 1 if status == NEEDS_REVIEW else 0,
        "TextractLatencyMs": latency_ms,
        "AvgFieldConfidence": conf["avg"],
        "FieldsNeedingReview": conf["lowCount"],
    }, unit_map={"TextractLatencyMs": "Milliseconds", "AvgFieldConfidence": "Percent"}, docId=doc_id)
    log("INFO", "documento procesado", docId=doc_id, status=status, flags=[f["code"] for f in flags],
        avgConfidence=conf["avg"], latencyMs=round(latency_ms, 1))
    return {"docId": doc_id, "status": status}


def handler(event, context):
    failures = []
    for rec in event.get("Records", []):
        msg = json.loads(rec["body"])
        doc_id = msg["docId"]
        attempt = int(rec.get("attributes", {}).get("ApproximateReceiveCount", "1"))
        try:
            set_status(doc_id, PROCESSING, f"intento {attempt}", allowed_from=[QUEUED, RETRYING, PROCESSING])
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            log("WARN", "mensaje repetido de un documento ya terminado; se descarta", docId=doc_id)
            continue
        try:
            process(msg)
        except PermanentError as e:
            set_status(doc_id, FAILED, str(e), extra={"error": str(e), "errorType": "PERMANENT"})
            emit_metrics("worker", {"DocumentsFailed": 1}, docId=doc_id)
            log("ERROR", "error permanente, no se reintenta", docId=doc_id, error=str(e))
        except Exception as e:  # transitorio -> reintento SQS / DLQ
            final = attempt >= MAX_RECEIVE
            set_status(doc_id, FAILED if final else RETRYING,
                       f"intento {attempt}/{MAX_RECEIVE}: {e}" + (" -> DLQ" if final else ""),
                       extra={"error": str(e), "errorType": "TRANSIENT", "attempts": attempt})
            emit_metrics("worker", {"ProcessingRetries": 1, "DocumentsFailed": 1 if final else 0}, docId=doc_id)
            log("ERROR", "error transitorio", docId=doc_id, attempt=attempt, toDLQ=final, error=repr(e))
            # Backoff exponencial: el mensaje reaparece en 10 s, 20 s, ... en vez de esperar
            # todo el VisibilityTimeout de la cola (360 s).
            if QUEUE_URL and rec.get("receiptHandle"):
                try:
                    sqs.change_message_visibility(QueueUrl=QUEUE_URL, ReceiptHandle=rec["receiptHandle"],
                                                  VisibilityTimeout=min(BACKOFF_BASE * 2 ** (attempt - 1), 300))
                except Exception as ve:  # no crítico
                    log("WARN", "no se pudo ajustar la visibilidad", error=repr(ve))
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
