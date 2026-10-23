#!/usr/bin/env python3
"""CASOS DE ERROR Y CONDICIONES LÍMITE (rúbrica: "un caso de error o condición límite").

    python3 scripts/caso_error.py --stack sars-r09 [--esperar-alarma]

 E1  Archivo con extensión no permitida (.txt)        -> REJECTED en la ingesta, nunca llega a Textract
 E2  Archivo de 6 MB (> límite de 5 MB)              -> REJECTED en la ingesta
 E3  PDF corrupto                                     -> Textract UnsupportedDocument -> FAILED (permanente, sin reintentos)
 E4  Fallo transitorio inyectado (incoming/chaos-*)   -> RETRYING x2 -> FAILED -> mensaje en la DLQ -> alarma CloudWatch -> SNS
 E5  API sin clave                                    -> HTTP 403 (API Gateway)
 E6  Corrección con valor inválido (fecha "31/02/xx") -> HTTP 400 (validación en la Lambda)
"""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from _comun import ROOT, Stack, header, save, stamp

ERR = ROOT / "datos" / "errores"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", default="sars-r09")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--esperar-alarma", action="store_true", help="espera (hasta 5 min) a que la alarma de la DLQ pase a ALARM")
    a = ap.parse_args()
    st = Stack(a.stack, a.region)
    run = stamp()
    rows, data, ok_all = [], {"run": run, "cases": {}}, True

    big = Path(tempfile.gettempdir()) / "e2_grande.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(6 * 1024 * 1024))

    uploads = {
        "E1": (ERR / "e1_documento.txt", f"incoming/{run}/e1_documento.txt", {"REJECTED"}),
        "E2": (big, f"incoming/{run}/e2_grande.png", {"REJECTED"}),
        "E3": (ERR / "e2_corrupto.pdf", f"incoming/{run}/e3_corrupto.pdf", {"FAILED"}),
        "E4": (ERR / "e3_reintentos.png", f"incoming/chaos-{run}-e4.png", {"FAILED"}),
    }
    ids = {c: st.upload(p, k) for c, (p, k, _) in uploads.items()}
    t0 = time.time()
    done = st.wait(ids, timeout=180, accept={"REJECTED", "FAILED", "PROCESSED", "NEEDS_REVIEW"})
    for case, (_, key, expected) in uploads.items():
        it = done.get(case)
        status = it["status"] if it else "TIMEOUT"
        ok = status in expected
        ok_all &= ok
        hist = " → ".join(h["status"] for h in (it or {}).get("history", []))
        rows.append(f"| {case} | `{key}` | {status} | {(it or {}).get('error', '—')} | {hist} | {'✅' if ok else '❌'} |")
        data["cases"][case] = {"key": key, "status": status, "error": (it or {}).get("error"), "history": (it or {}).get("history")}

    # DLQ: el mensaje de E4 debe estar ahí
    dlq_n = 0
    for _ in range(20):
        attrs = st.sqs.get_queue_attributes(QueueUrl=st.out["DLQUrl"], AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
        dlq_n = int(attrs["ApproximateNumberOfMessages"])
        if dlq_n > 0:
            break
        time.sleep(3)
    rows.append(f"| E4b | DLQ `{st.out['DLQUrl'].rsplit('/', 1)[-1]}` | {dlq_n} mensaje(s) | — | — | {'✅' if dlq_n > 0 else '❌'} |")
    ok_all &= dlq_n > 0

    alarm_state = "no consultada"
    if a.esperar_alarma:
        name = f"{a.stack}-dlq-con-mensajes"
        for _ in range(30):
            alarm_state = st.cw.describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]
            if alarm_state == "ALARM":
                break
            time.sleep(10)
        rows.append(f"| E4c | Alarma `{name}` | {alarm_state} | — | — | {'✅' if alarm_state == 'ALARM' else '❌'} |")

    code5, body5 = st.call_api("GET", "/api/documents", key=False)
    rows.append(f"| E5 | GET /api/documents sin x-api-key | HTTP {code5} | {body5.get('message', '')} | — | {'✅' if code5 == 403 else '❌'} |")
    ok_all &= code5 == 403

    target = next((c for c in done.values() if c["status"] in ("NEEDS_REVIEW", "PROCESSED")), None)
    if target is None:  # usa cualquier documento revisable existente
        code, lst = st.call_api("GET", "/api/documents?status=NEEDS_REVIEW&limit=1")
        target = (lst.get("items") or [None])[0]
    if target:
        code6, body6 = st.call_api("PUT", f"/api/documents/{target['docId']}", {"fields": {"fecha": "31/02/2026"}, "reviewer": "caso-error"})
        rows.append(f"| E6 | PUT fecha='31/02/2026' | HTTP {code6} | {body6.get('error', '')} | — | {'✅' if code6 == 400 else '❌'} |")
        ok_all &= code6 == 400
    else:
        rows.append("| E6 | (sin documentos revisables; ejecutar antes la prueba de aceptación) | — | — | — | ⏭ |")

    md = header(st, "Casos de error y condiciones límite · R09")
    md += ["| Caso | Entrada | Resultado | Detalle | Historial | OK |", "|---|---|---|---|---|---|", *rows, "",
           f"Tiempo total: {time.time() - t0:.0f} s", "", f"**Resultado global: {'APROBADO ✅' if ok_all else 'CON OBSERVACIONES ❌'}**"]
    data.update({"dlq_messages": dlq_n, "alarm": alarm_state})
    path = save(f"caso_error_{run}", md, data)
    print("\n".join(md))
    print(f"\nEvidencia: {path}")
    print("Nota: para vaciar la DLQ después de la demo: aws sqs purge-queue --queue-url " + st.out["DLQUrl"])
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
