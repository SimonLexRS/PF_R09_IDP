"""Prueba de integración local del flujo completo con AWS simulado (moto):
S3 event -> ingest -> SQS -> worker (Textract simulado) -> DynamoDB -> API de revisión."""
import json

from botocore.exceptions import ClientError
from conftest import BUCKET, CLEAN


def s3_event(key, size, version):
    return {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key, "size": size, "versionId": version}}}]}


def upload(aws, key, body=b"x" * 2048):
    r = aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=body)
    return s3_event(key, len(body), r["VersionId"])


def drain(aws):
    msgs = aws["sqs"].receive_message(QueueUrl=aws["queue"], MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])
    return {"Records": [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"], "attributes": m["Attributes"]} for m in msgs]}


class FakeTextract:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def analyze_expense(self, **kw):
        self.calls += 1
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "simulado"}}, "AnalyzeExpense")
        return self.response


def test_happy_path_and_review(aws):
    ev = upload(aws, "incoming/run/01.png")
    assert aws["ingest"].handler(ev, None)["received"] == 1
    assert aws["ingest"].handler(ev, None)["duplicates"] == 1          # evento S3 repetido: idempotente
    aws["worker"].textract = FakeTextract(CLEAN)
    batch = drain(aws)
    assert aws["worker"].handler(batch, None) == {"batchItemFailures": []}
    doc_id = json.loads(batch["Records"][0]["body"])["docId"]
    item = aws["table"].get_item(Key={"docId": doc_id})["Item"]
    assert item["status"] == "PROCESSED"
    assert [h["status"] for h in item["history"]] == ["QUEUED", "PROCESSING", "PROCESSED"]
    assert aws["s3"].get_object(Bucket=BUCKET, Key=f"results/{doc_id}.json")

    # Mensaje SQS repetido de un documento ya terminado: se descarta sin llamar a Textract
    aws["worker"].textract.calls = 0
    assert aws["worker"].handler(batch, None) == {"batchItemFailures": []}
    assert aws["worker"].textract.calls == 0

    # API de revisión
    api = aws["review"].handler
    r = api({"httpMethod": "GET", "resource": "/api/{proxy+}", "pathParameters": {"proxy": "documents"}, "queryStringParameters": {"status": "PROCESSED"}}, None)
    assert r["statusCode"] == 200 and json.loads(r["body"])["count"] == 1
    r = api({"httpMethod": "PUT", "resource": "/api/{proxy+}", "pathParameters": {"proxy": f"documents/{doc_id}"},
             "body": json.dumps({"fields": {"total": "790,00"}, "reviewer": "simon"})}, None)
    assert r["statusCode"] == 200 and json.loads(r["body"])["status"] == "REVIEWED"
    r = api({"httpMethod": "PUT", "resource": "/api/{proxy+}", "pathParameters": {"proxy": f"documents/{doc_id}"},
             "body": json.dumps({"fields": {"fecha": "31/02/2026"}})}, None)
    assert r["statusCode"] == 400
    r = api({"httpMethod": "GET", "resource": "/ui", "pathParameters": None}, None)
    assert r["statusCode"] == 200 and "text/html" in r["headers"]["Content-Type"]


def test_duplicate_invoice_flagged(aws):
    aws["worker"].textract = FakeTextract(CLEAN)
    aws["ingest"].handler(upload(aws, "incoming/a.png"), None)
    aws["worker"].handler(drain(aws), None)
    aws["ingest"].handler(upload(aws, "incoming/b.png"), None)
    batch = drain(aws)
    aws["worker"].handler(batch, None)
    item = aws["table"].get_item(Key={"docId": json.loads(batch["Records"][0]["body"])["docId"]})["Item"]
    assert item["status"] == "NEEDS_REVIEW"
    assert "DUPLICATE" in [f["code"] for f in item["flags"]]


def test_rejected_extension_and_size(aws):
    assert aws["ingest"].handler(upload(aws, "incoming/nota.txt"), None)["rejected"] == 1
    big = upload(aws, "incoming/grande.png", b"x" * (6 * 1024 * 1024))
    assert aws["ingest"].handler(big, None)["rejected"] == 1
    assert drain(aws)["Records"] == []                                   # nada llega a la cola


def test_permanent_error_no_retry(aws):
    aws["ingest"].handler(upload(aws, "incoming/corrupto.pdf"), None)
    aws["worker"].textract = FakeTextract(error="UnsupportedDocumentException")
    batch = drain(aws)
    assert aws["worker"].handler(batch, None) == {"batchItemFailures": []}
    item = aws["table"].get_item(Key={"docId": json.loads(batch["Records"][0]["body"])["docId"]})["Item"]
    assert item["status"] == "FAILED" and item["errorType"] == "PERMANENT"


def test_transient_error_retries_then_fails(aws):
    aws["ingest"].handler(upload(aws, "incoming/chaos-1.png"), None)
    batch = drain(aws)
    rec = batch["Records"][0]
    doc_id = json.loads(rec["body"])["docId"]
    for attempt, expected in ((1, "RETRYING"), (2, "RETRYING"), (3, "FAILED")):
        rec["attributes"]["ApproximateReceiveCount"] = str(attempt)
        out = aws["worker"].handler({"Records": [rec]}, None)
        assert out["batchItemFailures"] == [{"itemIdentifier": rec["messageId"]}]  # SQS reintenta -> DLQ
        assert aws["table"].get_item(Key={"docId": doc_id})["Item"]["status"] == expected
