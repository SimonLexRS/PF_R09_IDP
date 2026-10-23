"""Lambda de la API de revisión (API Gateway REST, integración proxy).

Rutas:
  GET  /ui                       -> HTML de la vista de revisión (sin clave; no contiene datos)
  GET  /api/documents?status=X   -> lista de documentos por estado (requiere x-api-key)
  GET  /api/documents/{id}       -> detalle + URL prefirmada (5 min) del original
  PUT  /api/documents/{id}       -> guarda correcciones y marca REVIEWED
  POST /api/upload-url           -> URL prefirmada de subida a incoming/
  GET  /api/stats                -> conteo por estado
La clave de API y el plan de uso (límite de solicitudes) se aplican en API Gateway.
"""
import json
import os
import re
import uuid

import boto3
from boto3.dynamodb.conditions import Key

from common import NEEDS_REVIEW, PROCESSED, REVIEWED, emit_metrics, from_dynamo, log, now_iso, to_dynamo
from extraction import FIELD_MAP, normalize

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]
URL_TTL = int(os.environ.get("PRESIGNED_TTL_SECONDS", "300"))
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}

s3 = boto3.client("s3", config=boto3.session.Config(signature_version="s3v4"))
table = boto3.resource("dynamodb").Table(TABLE)

with open(os.path.join(os.path.dirname(__file__), "ui.html"), encoding="utf-8") as fh:
    UI_HTML = fh.read()


def resp(code: int, body, content_type="application/json"):
    return {
        "statusCode": code,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        "body": body if isinstance(body, str) else json.dumps(from_dynamo(body), ensure_ascii=False, default=str),
    }


def list_documents(status: str, limit: int):
    if status == "ALL":
        items = table.scan(Limit=500, FilterExpression="attribute_exists(#s)", ExpressionAttributeNames={"#s": "status"}).get("Items", [])
        items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        items = items[:limit]
    else:
        items = table.query(IndexName="StatusIndex", KeyConditionExpression=Key("status").eq(status),
                            ScanIndexForward=False, Limit=limit).get("Items", [])
    slim = [{k: it.get(k) for k in ("docId", "fileName", "s3Key", "status", "createdAt", "updatedAt", "confidence", "flags", "error", "textractLatencyMs")} for it in items]
    return {"items": slim, "count": len(slim)}


def get_document(doc_id: str):
    it = table.get_item(Key={"docId": doc_id}).get("Item")
    if not it or doc_id.startswith("DEDUP#"):
        return None
    it["originalUrl"] = s3.generate_presigned_url("get_object", Params={"Bucket": it["bucket"], "Key": it["s3Key"]}, ExpiresIn=URL_TTL)
    return it


def save_review(doc_id: str, body: dict):
    reviewer = re.sub(r"[^\w@.\- ]", "", str(body.get("reviewer", "")))[:64] or "anonimo"
    corrections = body.get("fields") or {}
    approve = body.get("approve", True)
    it = table.get_item(Key={"docId": doc_id}).get("Item")
    if not it:
        return 404, {"error": "documento no encontrado"}
    if it.get("status") not in (NEEDS_REVIEW, PROCESSED, REVIEWED):
        return 409, {"error": f"no se puede revisar un documento en estado {it.get('status')}"}

    fields = from_dynamo(it.get("fields", {}))
    changes = []
    for name, value in corrections.items():
        if name not in FIELD_MAP:
            return 400, {"error": f"campo desconocido: {name}"}
        value = str(value).strip()[:200]
        norm = normalize(FIELD_MAP[name]["kind"], value)
        if value and norm is None:
            return 400, {"error": f"valor inválido para {name}: '{value}'"}
        old = fields.get(name, {}).get("value")
        if old != value:
            changes.append({"field": name, "from": old, "to": value})
        fields[name] = {**fields.get(name, {}), "value": value, "normalized": norm, "source": "manual",
                        "confidence": 100.0, "needsReview": False, "reviewedBy": reviewer}
    if approve:
        for f in fields.values():
            f["needsReview"] = False

    ts = now_iso()
    table.update_item(
        Key={"docId": doc_id},
        UpdateExpression="SET #s = :s, #f = :f, updatedAt = :u, reviewedAt = :u, reviewedBy = :r, "
                         "history = list_append(if_not_exists(history, :e), :h)",
        ConditionExpression="#s IN (:a, :b, :c)",
        ExpressionAttributeNames={"#s": "status", "#f": "fields"},
        ExpressionAttributeValues={":s": REVIEWED if approve else it["status"], ":f": to_dynamo(fields), ":u": ts, ":r": reviewer,
                                   ":e": [], ":h": [to_dynamo({"at": ts, "status": REVIEWED if approve else it["status"], "by": reviewer,
                                                               "detail": f"{len(changes)} correcciones", "changes": changes})],
                                   ":a": NEEDS_REVIEW, ":b": PROCESSED, ":c": REVIEWED},
    )
    emit_metrics("review", {"DocumentsReviewed": 1 if approve else 0, "FieldsCorrected": len(changes)}, docId=doc_id)
    log("INFO", "revisión guardada", docId=doc_id, reviewer=reviewer, changes=changes)
    return 200, {"docId": doc_id, "status": REVIEWED if approve else it["status"], "changes": changes}


def upload_url(body: dict):
    name = os.path.basename(str(body.get("filename", "")))
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        return 400, {"error": f"extensión no permitida: {ext}"}
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
    key = f"incoming/{uuid.uuid4().hex[:8]}-{safe}"
    url = s3.generate_presigned_url("put_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=URL_TTL)
    return 200, {"key": key, "url": url, "expiresIn": URL_TTL}


def stats():
    counts = {}
    kwargs = {"ProjectionExpression": "#s", "ExpressionAttributeNames": {"#s": "status"}, "FilterExpression": "attribute_exists(#s)"}
    while True:
        page = table.scan(**kwargs)
        for it in page.get("Items", []):
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return counts


def handler(event, context):
    method = event.get("httpMethod", "GET")
    resource = event.get("resource", "")
    proxy = (event.get("pathParameters") or {}).get("proxy", "")
    qs = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "JSON inválido"})

    try:
        if resource == "/ui":
            return resp(200, UI_HTML, "text/html; charset=utf-8")
        parts = [p for p in proxy.split("/") if p]
        if parts == ["documents"] and method == "GET":
            status = qs.get("status", NEEDS_REVIEW).upper()
            limit = max(1, min(int(qs.get("limit", "50")), 100))
            return resp(200, list_documents(status, limit))
        if len(parts) == 2 and parts[0] == "documents":
            if method == "GET":
                it = get_document(parts[1])
                return resp(200, it) if it else resp(404, {"error": "documento no encontrado"})
            if method == "PUT":
                return resp(*save_review(parts[1], body))
        if parts == ["upload-url"] and method == "POST":
            return resp(*upload_url(body))
        if parts == ["stats"] and method == "GET":
            return resp(200, stats())
        return resp(404, {"error": f"ruta no encontrada: {method} /{proxy}"})
    except Exception as e:  # nunca exponer trazas al cliente
        log("ERROR", "error no controlado en la API", error=repr(e), path=proxy, method=method)
        return resp(500, {"error": "error interno"})
