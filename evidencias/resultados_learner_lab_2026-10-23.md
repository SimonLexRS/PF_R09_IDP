# Resultados en el Learner Lab · 22–23/10/2026

Pila `sars-r09` · región us-east-1 · tabla `sars-r09-documents` · bucket `ucb-proyecto-sars-docs-0039`.
Horas en UTC (Bolivia = UTC−4). Valores leídos de DynamoDB (consola → Explorar elementos).

## Prueba de aceptación (versión calibrada v5–v7)

| Documento | Estado | Conf. media | Conf. mín. | Banderas | Textract (ms) | Subida → final |
|---|---|---|---|---|---|---|
| 01_factura_limpia.png | PROCESSED | 93,38 % | 50,68 % (NIT, `OCR_VERIFIED`) | — | 2101,7 | 8,1 s |
| 02_factura_limpia.pdf | PROCESSED | 94,31 % | 57,48 % (cliente, `OCR_VERIFIED`) | — | 3172,0 | 4,3 s |
| 03_factura_baja_calidad.jpg | NEEDS_REVIEW | 99,62 % | 98,36 % | TOTAL_MISMATCH | 1795,2 | 8,3 s |
| 04_factura_total_inconsistente.png | NEEDS_REVIEW | 96,90 % | 71,44 % | TOTAL_MISMATCH | 1781,0 | 8,1 s |
| 05_factura_duplicada.png | NEEDS_REVIEW | 95,68 % | 76,39 % | DUPLICATE | 3327,6 | (*) |
| 06_factura_sin_numero.png | NEEDS_REVIEW | 94,46 % | 50,82 % (base CF, `OCR_VERIFIED`) | MISSING | 2198,8 | 3,6 s |

(*) La 05 se subió el 23/10 a las 20:05 UTC con el desencadenador SQS deshabilitado por el reinicio de la sesión
del laboratorio; quedó en `QUEUED` sin perderse y se procesó al reactivarlo (DLQ en 0).

Registros de deduplicación creados: `DEDUP#1029384756#1547` (01), `DEDUP#3344556677#88213` (02),
`DEDUP#7788990011#1203` (04), `DEDUP#5566778899#30422` (03).

## Casos de error y condiciones límite (23/10/2026)

| Caso | Clave S3 | Recibido (UTC) | Estado | Detalle |
|---|---|---|---|---|
| E1 | `incoming/e1_documento.txt` | 20:18:19.741 | REJECTED | Extensión no permitida: '.txt'. Permitidas: .jpeg, .jpg, .pdf, .png, .tif, .tiff |
| E2 | `incoming/e2_grande_6mb.png` (6 291 464 B) | 20:18:27.423 | REJECTED | Archivo de 6.0 MB supera el máximo de 5.0 MB |
| E3 | `incoming/e2_corrupto.pdf` | 20:18:19.757 | FAILED | UnsupportedDocumentException: Request has unsupported document format · errorType PERMANENT · 1 intento |
| E4 | `incoming/chaos-e4_reintentos.png` | 20:18:28.203 | FAILED | Fallo transitorio inyectado · errorType TRANSIENT · attempts 3 → DLQ |
| E5 | `GET /prod/api/documents` sin `x-api-key` | — | HTTP 403 | `{"message":"Forbidden"}` (API Gateway) |
| E6 | `PUT /api/documents/{06}` con fecha `31/02/2026` | 20:28 aprox. | HTTP 400 | «valor inválido para fecha: '31/02/2026'»; el documento sigue en NEEDS_REVIEW |

Control: `incoming/e3_reintentos.png` (misma imagen que E4 **sin** el prefijo `chaos-`) terminó en PROCESSED:
la inyección de fallos solo afecta a `incoming/chaos-*`.

### Alarma

`sars-r09-documentos-fallidos` (DocumentsFailed > 0 en 5 min): OK → **ALARM** el 2026-10-23 20:19:06 UTC
(≈ 38 s después de subir E4) y acción ejecutada sobre `arn:aws:sns:us-east-1:<cuenta>:sars-r09-alertas`.
