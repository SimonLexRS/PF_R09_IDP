"""Lambda de ingesta: disparada por el evento s3:ObjectCreated en el prefijo incoming/.

1. Valida extensión y tamaño (Textract síncrono: JPEG, PNG, PDF/TIFF de 1 página).
2. Registra el documento en DynamoDB (escritura condicional => idempotente:
   S3 entrega eventos "al menos una vez").
3. Encola un trabajo en SQS para procesamiento asíncrono.
"""
import hashlib
import json
import os
import urllib.parse

import boto3

from common import QUEUED, REJECTED, emit_metrics, log, now_iso

TABLE = os.environ["TABLE_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]
MAX_SIZE_MB = float(os.environ.get("MAX_SIZE_MB", "5"))
ALLOWED_EXT = {e.strip().lower() for e in os.environ.get("ALLOWED_EXT", ".pdf,.png,.jpg,.jpeg,.tif,.tiff").split(",")}

s3 = boto3.client("s3")
sqs = boto3.client("sqs")
table = boto3.resource("dynamodb").Table(TABLE)


def make_doc_id(bucket: str, key: str, version_or_etag: str) -> str:
    return hashlib.sha256(f"{bucket}/{key}/{version_or_etag}".encode()).hexdigest()[:20]


def validate(key: str, size: int) -> str | None:
    ext = os.path.splitext(key)[1].lower()
    if ext not in ALLOWED_EXT:
        return f"Extensión no permitida: '{ext or 'sin extensión'}'. Permitidas: {sorted(ALLOWED_EXT)}"
    if size == 0:
        return "Archivo vacío"
    if size > MAX_SIZE_MB * 1024 * 1024:
        return f"Archivo de {size / 1048576:.1f} MB supera el máximo de {MAX_SIZE_MB} MB"
    return None


def handler(event, context):
    received = rejected = duplicates = 0
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        size = int(rec["s3"]["object"].get("size", 0))
        version = rec["s3"]["object"].get("versionId") or rec["s3"]["object"].get("eTag", "")
        doc_id = make_doc_id(bucket, key, version)
        reason = validate(key, size)
        ts = now_iso()
        item = {
            "docId": doc_id,
            "bucket": bucket,
            "s3Key": key,
            "fileName": os.path.basename(key),
            "sizeBytes": size,
            "status": REJECTED if reason else QUEUED,
            "createdAt": ts,
            "updatedAt": ts,
            "history": [{"at": ts, "status": REJECTED if reason else QUEUED, "by": "ingest", "detail": reason or "encolado"}],
        }
        if reason:
            item["error"] = reason
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(docId)")
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            duplicates += 1
            log("WARN", "evento S3 duplicado, se ignora", docId=doc_id, key=key)
            continue

        if reason:
            rejected += 1
            log("WARN", "documento rechazado", docId=doc_id, key=key, reason=reason)
            continue

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({"docId": doc_id, "bucket": bucket, "key": key}),
        )
        received += 1
        log("INFO", "documento encolado", docId=doc_id, key=key, sizeBytes=size)

    emit_metrics("ingest", {"DocumentsReceived": received, "DocumentsRejected": rejected, "DuplicateEvents": duplicates})
    return {"received": received, "rejected": rejected, "duplicates": duplicates}
