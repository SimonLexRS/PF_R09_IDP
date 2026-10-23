#!/usr/bin/env python3
"""PRUEBA DE ACEPTACIÓN del Reto 09:
"Procesar documentos de ejemplo, mostrar los campos extraídos y marcar los que requieren corrección."

    python3 scripts/prueba_aceptacion.py --stack sars-r09 [--demo-revision]

Pasos:
 1. Sube datos/muestras/* a s3://<bucket>/incoming/<run>/ (fase 1) y luego el duplicado (fase 2).
 2. Espera a que cada documento llegue a un estado terminal (S3 -> Lambda -> SQS -> Lambda -> Textract -> DynamoDB).
 3. Consulta la API de revisión (con clave) y muestra los campos, la confianza y las banderas.
 4. Compara con datos/muestras/esperado.json y escribe la evidencia en evidencias/.
 5. (--demo-revision) corrige por API un campo de un documento NEEDS_REVIEW -> REVIEWED.
"""
import argparse
import json
import sys

from _comun import REQUIRED, ROOT, Stack, header, save, stamp

MUESTRAS = ROOT / "datos" / "muestras"


def fmt_field(f):
    if not f:
        return "—"
    mark = " ⚠" if f.get("needsReview") else ""
    return f"{f.get('value')} ({float(f.get('confidence', 0)):.0f}%){mark}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", default="sars-r09")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--demo-revision", action="store_true")
    a = ap.parse_args()

    st = Stack(a.stack, a.region)
    esperado = {k: v for k, v in json.loads((MUESTRAS / "esperado.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
    run = stamp()

    # Reinicia solo los registros de deduplicación para que la prueba sea repetible
    for it in st.table.scan(FilterExpression="begins_with(docId, :p)", ExpressionAttributeValues={":p": "DEDUP#"}).get("Items", []):
        st.table.delete_item(Key={"docId": it["docId"]})

    results = {}
    for fase in (1, 2):
        ids = {}
        for name, exp in esperado.items():
            if exp["fase"] == fase:
                ids[name] = st.upload(MUESTRAS / name, f"incoming/{run}/{name}")
                print(f"[fase {fase}] subido {name} -> docId {ids[name]}")
        results.update(st.wait(ids, a.timeout))

    # Consulta por la API (el mismo camino que usa la vista de revisión)
    code_list, listing = st.call_api("GET", "/api/documents?status=NEEDS_REVIEW&limit=100")

    rows, ok_all, data = [], True, {"run": run, "documents": {}}
    for name, exp in esperado.items():
        it = results.get(name)
        if not it:
            rows.append(f"| {name} | TIMEOUT | — | — | — | ❌ |")
            ok_all = False
            continue
        code, doc = st.call_api("GET", f"/api/documents/{it['docId']}")
        fields = doc.get("fields", {}) if code == 200 else {}
        flags = [f["code"] for f in doc.get("flags", [])] if code == 200 else []
        n_req = sum(1 for r in REQUIRED if r in fields)
        checks = [
            it["status"] in exp["status_in"],
            (not exp["flags_any"]) or any(f in flags for f in exp["flags_any"]),
            n_req >= exp["min_campos"],
            code == 200,
        ]
        ok = all(checks)
        ok_all &= ok
        conf = doc.get("confidence", {})
        rows.append(f"| {name} | {it['status']} | {n_req}/5 | {conf.get('avg', '—')} | {', '.join(flags) or '—'} | {'✅' if ok else '❌'} |")
        data["documents"][name] = {"docId": it["docId"], "status": it["status"], "flags": doc.get("flags"), "fields": fields,
                                   "confidence": conf, "e2e_seconds": it["_e2e_s"], "textractLatencyMs": doc.get("textractLatencyMs"),
                                   "expected": exp, "pass": ok}

    md = header(st, "Prueba de aceptación · R09 Procesamiento inteligente de documentos")
    md += ["## Resultado por documento", "",
           "| Archivo | Estado | Obligatorios | Conf. media % | Banderas | OK |", "|---|---|---|---|---|---|", *rows, ""]
    md += ["## Campos extraídos (⚠ = requiere corrección)", "",
           "| Archivo | N° factura | Fecha | Proveedor | NIT | Total |", "|---|---|---|---|---|---|"]
    for name, d in data["documents"].items():
        f = d["fields"]
        md.append(f"| {name} | {fmt_field(f.get('numero_factura'))} | {fmt_field(f.get('fecha'))} | {fmt_field(f.get('proveedor'))} | "
                  f"{fmt_field(f.get('nit_emisor'))} | {fmt_field(f.get('total'))} |")
    e2e = [d["e2e_seconds"] for d in data["documents"].values()]
    lat = [float(d["textractLatencyMs"]) for d in data["documents"].values() if d.get("textractLatencyMs")]
    md += ["", "## Métricas", "",
           f"- Documentos: {len(results)}/{len(esperado)} terminados",
           f"- Tiempo extremo a extremo (subida → estado final): máx {max(e2e) if e2e else '—'} s",
           f"- Latencia Textract: media {sum(lat) / len(lat):.0f} ms" if lat else "- Latencia Textract: —",
           f"- API GET /api/documents?status=NEEDS_REVIEW -> HTTP {code_list}, {listing.get('count', 0)} documentos", ""]

    if a.demo_revision:
        target = next((d for d in data["documents"].values() if d["status"] == "NEEDS_REVIEW"), None)
        if target:
            corr = {"numero_factura": "30422"} if "numero_factura" not in target["fields"] else {}
            code, r = st.call_api("PUT", f"/api/documents/{target['docId']}", {"fields": corr, "reviewer": "prueba-aceptacion", "approve": True})
            md += ["## Revisión humana por API", "", f"- PUT /api/documents/{target['docId']} -> HTTP {code}: {json.dumps(r, ensure_ascii=False)}", ""]
            data["review_demo"] = {"http": code, "response": r}

    md += [f"**Resultado global: {'APROBADA ✅' if ok_all else 'CON OBSERVACIONES ❌'}**"]
    path = save(f"prueba_aceptacion_{run}", md, data)
    print("\n".join(md))
    print(f"\nEvidencia: {path}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
