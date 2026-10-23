"""Funciones compartidas por los scripts de prueba (se ejecutan en CloudShell o en tu PC
con las credenciales temporales del Learner Lab). Solo requiere boto3."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidencias"
TERMINAL = {"REJECTED", "PROCESSED", "NEEDS_REVIEW", "REVIEWED", "FAILED"}
REQUIRED = ["numero_factura", "fecha", "proveedor", "nit_emisor", "total"]


class Stack:
    def __init__(self, name: str, region: str):
        self.name, self.region = name, region
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        st = cfn.describe_stacks(StackName=name)["Stacks"][0]
        self.out = {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}
        self.s3 = self.session.client("s3")
        self.sqs = self.session.client("sqs")
        self.cw = self.session.client("cloudwatch")
        self.table = self.session.resource("dynamodb").Table(self.out["TableName"])
        self.bucket = self.out["BucketName"]
        self.api = self.out["ApiBaseUrl"]
        self._key = None

    @property
    def api_key(self) -> str:
        if self._key is None:
            ag = self.session.client("apigateway")
            self._key = ag.get_api_key(apiKey=self.out["ApiKeyId"], includeValue=True)["value"]
        return self._key

    def upload(self, path: Path, key: str) -> str:
        """Sube el archivo y devuelve el docId (misma fórmula que la Lambda de ingesta)."""
        r = self.s3.put_object(Bucket=self.bucket, Key=key, Body=path.read_bytes())
        ver = r.get("VersionId") or r["ETag"].strip('"')
        return hashlib.sha256(f"{self.bucket}/{key}/{ver}".encode()).hexdigest()[:20]

    def wait(self, doc_ids: dict, timeout: int = 240, accept=TERMINAL) -> dict:
        """Espera a que cada docId llegue a un estado en `accept`. Devuelve {nombre: item}."""
        t0, done = time.time(), {}
        while time.time() - t0 < timeout and len(done) < len(doc_ids):
            for name, did in doc_ids.items():
                if name in done:
                    continue
                it = self.table.get_item(Key={"docId": did}).get("Item")
                if it and it.get("status") in accept:
                    it["_e2e_s"] = round(time.time() - t0, 1)
                    done[name] = it
            time.sleep(2)
        return done

    def call_api(self, method: str, path: str, body=None, key: bool = True):
        req = urllib.request.Request(self.api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"x-api-key": self.api_key} if key else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return e.code, {}


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def header(stack: Stack, title: str) -> list[str]:
    ident = stack.session.client("sts").get_caller_identity()
    acct = ident["Account"]
    return [f"# {title}", "",
            f"- Fecha (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"- Región: {stack.region} · Stack: {stack.name} · Cuenta: ****{acct[-4:]}",
            f"- Bucket: {stack.bucket} · Tabla: {stack.out['TableName']}", ""]


def save(name: str, md_lines: list[str], data) -> Path:
    EVID.mkdir(exist_ok=True)
    md = EVID / f"{name}.md"
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    (EVID / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return md
