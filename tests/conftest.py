import importlib
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

BUCKET = "idp-r09-test"
TABLE = "idp-r09-documents"


def expense_response(summary, items=(), lines=()):
    """Construye una respuesta mínima de AnalyzeExpense.
    summary: [(type, label, value, confidence)] ; items: [[(type, value, conf)]] ; lines: [(text, conf)]"""
    sf = [{"Type": {"Text": t, "Confidence": 99.0}, "LabelDetection": ({"Text": l, "Confidence": 99.0} if l else None),
           "ValueDetection": {"Text": v, "Confidence": c}, "PageNumber": 1} for t, l, v, c in summary]
    for f in sf:
        if f["LabelDetection"] is None:
            del f["LabelDetection"]
    groups = [{"LineItems": [{"LineItemExpenseFields": [{"Type": {"Text": t}, "ValueDetection": {"Text": v, "Confidence": c}} for t, v, c in row]} for row in items]}]
    blocks = [{"BlockType": "LINE", "Text": t, "Confidence": c, "Page": 1} for t, c in lines]
    return {"DocumentMetadata": {"Pages": 1}, "ExpenseDocuments": [{"ExpenseIndex": 1, "SummaryFields": sf, "LineItemGroups": groups, "Blocks": blocks}]}


CLEAN = expense_response(
    [("INVOICE_RECEIPT_ID", "Factura N°", "1547", 99.1), ("INVOICE_RECEIPT_DATE", "Fecha:", "14/10/2026", 98.7),
     ("VENDOR_NAME", None, "FERRETERÍA TORNILLO FELIZ S.R.L.", 97.5), ("OTHER", "NIT", "1029384756", 99.0),
     ("OTHER", "NIT/CI/CEX:", "4455667", 98.0), ("SUBTOTAL", "SUBTOTAL Bs", "803,00", 99.0),
     ("DISCOUNT", "DESCUENTO Bs", "20,00", 98.0), ("TOTAL", "TOTAL Bs", "783,00", 99.3)],
    items=[[("ITEM", "Martillo", 99.0), ("QUANTITY", "2", 99.0), ("PRICE", "170,00", 99.0)],
           [("ITEM", "Clavos", 99.0), ("QUANTITY", "10", 99.0), ("PRICE", "125,00", 99.0)],
           [("ITEM", "Taladro", 99.0), ("QUANTITY", "1", 99.0), ("PRICE", "420,00", 99.0)],
           [("ITEM", "Guantes", 99.0), ("QUANTITY", "4", 99.0), ("PRICE", "88,00", 99.0)]],
)


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TABLE_NAME", TABLE)
    monkeypatch.setenv("BUCKET_NAME", BUCKET)
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "90")
    monkeypatch.setenv("FAULT_INJECTION_PREFIX", "incoming/chaos-")
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("docId", "status", "createdAt")],
                         KeySchema=[{"AttributeName": "docId", "KeyType": "HASH"}],
                         GlobalSecondaryIndexes=[{"IndexName": "StatusIndex", "Projection": {"ProjectionType": "ALL"},
                                                  "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                                                                {"AttributeName": "createdAt", "KeyType": "RANGE"}]}])
        sqs = boto3.client("sqs")
        q = sqs.create_queue(QueueName="jobs")["QueueUrl"]
        monkeypatch.setenv("QUEUE_URL", q)
        mods = {}
        for m in ("common", "extraction", "ingest", "worker", "review"):
            if m in sys.modules:
                del sys.modules[m]
            mods[m] = importlib.import_module(m)
        yield {"s3": s3, "sqs": sqs, "queue": q, "table": boto3.resource("dynamodb").Table(TABLE), **mods}
