"""Lógica pura (sin AWS) para convertir la respuesta de Textract AnalyzeExpense
en campos de negocio, calcular la confianza y aplicar validaciones.

Separada del handler para poder probarla con pytest sin credenciales.
"""
import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Mapeo de campos: tipos normalizados de Textract + etiquetas en español
# (facturas bolivianas: "NIT", "Factura N°", "Código de Autorización", etc.)
# ---------------------------------------------------------------------------
FIELD_MAP = {
    "numero_factura": {"types": ["INVOICE_RECEIPT_ID"], "labels": [r"factura\s*n", r"n[°ºo.]*\s*(de\s*)?factura", r"^nro\.?$"], "required": True, "kind": "id"},
    "fecha": {"types": ["INVOICE_RECEIPT_DATE", "ORDER_DATE"], "labels": [r"^fecha"], "required": True, "kind": "date"},
    "proveedor": {"types": ["VENDOR_NAME"], "labels": [r"raz[oó]n social emisor"], "required": True, "kind": "text"},
    "nit_emisor": {"types": ["TAX_PAYER_ID", "VENDOR_VAT_NUMBER", "VENDOR_GST_NUMBER"], "labels": [r"^nit\s*:?$", r"nit emisor"], "required": True, "kind": "nit"},
    "cliente": {"types": ["RECEIVER_NAME"], "labels": [r"se[ñn]or", r"raz[oó]n social"], "required": False, "kind": "text"},
    "nit_cliente": {"types": ["RECEIVER_VAT_NUMBER", "RECEIVER_GST_NUMBER"], "labels": [r"nit\s*/\s*ci", r"ci\s*/\s*nit", r"nit.*cliente"], "required": False, "kind": "nit"},
    "codigo_autorizacion": {"types": [], "labels": [r"autorizaci[oó]n", r"\bcuf\b", r"c[oó]digo de control"], "required": False, "kind": "text"},
    "subtotal": {"types": ["SUBTOTAL"], "labels": [r"sub\s*-?\s*total"], "required": False, "kind": "amount"},
    "descuento": {"types": ["DISCOUNT"], "labels": [r"descuento"], "required": False, "kind": "amount"},
    # En facturas bolivianas "IMPORTE BASE CRÉDITO FISCAL" no es el impuesto: Textract lo tipifica como TAX.
    "impuesto": {"types": ["TAX"], "labels": [r"\biva\b", r"impuesto"], "exclude": r"base|cr[eé]dito", "required": False, "kind": "amount"},
    "base_credito_fiscal": {"types": [], "labels": [r"base.*cr[eé]dito", r"cr[eé]dito fiscal"], "required": False, "kind": "amount"},
    # Textract también tipifica "IMPORTE BASE CRÉDITO FISCAL" como TOTAL (dato real del Learner Lab)
    "total": {"types": ["TOTAL", "AMOUNT_DUE", "AMOUNT_PAID"], "labels": [r"^total", r"importe total", r"total a pagar"], "exclude": r"base|cr[eé]dito", "required": True, "kind": "amount"},
}

# Respaldo por expresiones regulares sobre las líneas de texto (si Textract no
# clasifica el campo). El resultado se marca con source="regex".
REGEX_FALLBACK = {
    "numero_factura": r"(?:factura\s*n[°ºo.]*|n[°ºo.]*\s*(?:de\s*)?factura|nro\.?\s*factura)\s*:?\s*([0-9]{1,12})",
    "nit_emisor": r"\bNIT\s*:?\s*([0-9]{5,15})\b(?!\s*/)",
    "fecha": r"fecha[^0-9]{0,15}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    "total": r"total[^0-9]{0,25}([0-9][0-9.,]*)",
}

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
          "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------
def parse_amount(text: str | None) -> float | None:
    """'Bs 1.234,50' -> 1234.5 ; '1,234.50' -> 1234.5 ; '350' -> 350.0"""
    if not text:
        return None
    s = re.sub(r"[^0-9,.\-]", "", str(text))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts[-1]) in (1, 2) and len(parts) == 2 else s.replace(",", "")
    elif s.count(".") >= 1:
        parts = s.split(".")
        if len(parts[-1]) == 3 and (len(parts) > 2 or len(parts[0]) <= 3):
            s = s.replace(".", "")  # separador de miles
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(text: str | None) -> str | None:
    """Devuelve la fecha en ISO (YYYY-MM-DD) o None."""
    if not text:
        return None
    t = text.strip().lower()
    m = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de|del)\s+(\d{4})", t)
    if m and m.group(2) in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = re.search(r"\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}", t)
    if not m:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(m.group(0), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def normalize(kind: str, text: str | None):
    if kind == "amount":
        return parse_amount(text)
    if kind == "date":
        return parse_date(text)
    if kind in ("id", "nit"):
        return digits(text) or None
    return (text or "").strip() or None


# ---------------------------------------------------------------------------
# Extracción
# ---------------------------------------------------------------------------
def _conf(d: dict | None) -> float:
    return float((d or {}).get("Confidence", 0.0))


def _text(d: dict | None) -> str:
    return ((d or {}).get("Text") or "").strip()


def parse_expense(response: dict) -> dict:
    """Convierte la respuesta de AnalyzeExpense en {fields, line_items, lines, pages}."""
    docs = response.get("ExpenseDocuments", [])
    summary, groups, lines, words = [], [], [], []
    for d in docs:
        summary.extend(d.get("SummaryFields", []))
        groups.extend(d.get("LineItemGroups", []))
        lines.extend([b for b in d.get("Blocks", []) if b.get("BlockType") == "LINE"])
        words.extend([b for b in d.get("Blocks", []) if b.get("BlockType") == "WORD"])

    # Candidatos por campo de negocio: (prioridad, confianza, idx, registro)
    candidates: dict[str, list] = {k: [] for k in FIELD_MAP}
    for idx, sf in enumerate(summary):
        ftype = _text(sf.get("Type")).upper()
        label = _text(sf.get("LabelDetection")).lower()
        value = _text(sf.get("ValueDetection"))
        if not value:
            continue
        vconf = _conf(sf.get("ValueDetection"))
        for name, cfg in FIELD_MAP.items():
            prio = None
            if cfg.get("exclude") and label and re.search(cfg["exclude"], label):
                continue
            if ftype in cfg["types"]:
                prio = cfg["types"].index(ftype)
                mconf = _conf(sf.get("Type"))
            elif label and any(re.search(p, label) for p in cfg["labels"]):
                prio = 10  # etiqueta: menor prioridad que el tipo normalizado
                mconf = _conf(sf.get("LabelDetection"))
            if prio is not None:
                # La confianza del campo es la del VALOR leído (calidad OCR). La confianza de la
                # etiqueta/tipo solo decide el mapeo y se guarda aparte (mapConfidence).
                candidates[name].append((prio, -vconf, idx, {
                    "value": value,
                    "label": _text(sf.get("LabelDetection")) or None,
                    "textractType": ftype,
                    "confidence": round(vconf, 2),
                    "mapConfidence": round(mconf, 2),
                    "page": sf.get("PageNumber", 1),
                    "source": "textract",
                }))

    fields, used = {}, set()
    # Primero los campos con tipo explícito para que no los "roben" etiquetas genéricas
    for name in sorted(FIELD_MAP, key=lambda n: (not FIELD_MAP[n]["types"], n)):
        for prio, _negconf, idx, rec in sorted(candidates[name]):
            if idx in used:
                continue
            used.add(idx)
            fields[name] = rec
            break

    # Respaldo por regex para campos obligatorios ausentes
    for name, pattern in REGEX_FALLBACK.items():
        if name in fields:
            continue
        for ln in lines:
            m = re.search(pattern, _text(ln), flags=re.IGNORECASE)
            if m:
                fields[name] = {"value": m.group(1), "label": None, "textractType": "LINE",
                                "confidence": round(_conf(ln), 2), "page": ln.get("Page", 1), "source": "regex"}
                break

    # Verificación cruzada con el OCR: la confianza de AnalyzeExpense mide la asociación
    # etiqueta->valor; la de los bloques WORD/LINE mide la lectura de los caracteres.
    ocr_blocks = [(re.sub(r"\s", "", _text(b)), _conf(b)) for b in words + lines]
    for name, rec in fields.items():
        rec["normalized"] = normalize(FIELD_MAP[name]["kind"], rec["value"])
        v = re.sub(r"\s", "", rec["value"])
        if len(v) >= 4:
            matches = [c for t, c in ocr_blocks if t == v] or [c for t, c in ocr_blocks if v in t]
            if matches:
                rec["ocrConfidence"] = round(max(matches), 2)

    items = []
    for g in groups:
        for li in g.get("LineItems", []):
            row, confs = {}, []
            for f in li.get("LineItemExpenseFields", []):
                t = _text(f.get("Type")).upper()
                v = _text(f.get("ValueDetection"))
                key = {"ITEM": "descripcion", "QUANTITY": "cantidad", "UNIT_PRICE": "precio_unitario", "PRICE": "importe"}.get(t)
                if key and v:
                    row[key] = v
                    confs.append(_conf(f.get("ValueDetection")))
            if row:
                row["importe_num"] = parse_amount(row.get("importe"))
                row["confidence"] = round(min(confs), 2) if confs else 0.0
                items.append(row)

    pages = max([d.get("ExpenseIndex", 1) for d in docs] + [response.get("DocumentMetadata", {}).get("Pages", 1)])
    return {"fields": fields, "line_items": items, "lines": [_text(l) for l in lines], "pages": pages}


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------
def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.05, 0.005 * abs(b))


def validate(fields: dict, line_items: list, threshold: float, today: date | None = None, max_age_years: int = 3,
             min_corroborated: float = 70.0) -> list[dict]:
    """Devuelve banderas {field, code, message} y marca needsReview en los campos.

    Regla de confianza en dos niveles (calibrada con Textract en el Learner Lab):
      - confianza >= threshold (90)                          -> aceptado
      - min_corroborated (70) <= confianza < threshold       -> aceptado SOLO si una regla de negocio
        independiente lo corrobora (formato estricto o cuadre aritmético); si no, LOW_CONFIDENCE
      - si el valor exacto aparece en el OCR (WORD/LINE) con confianza >= threshold y pasa la
        regla de formato/cuadre (o es un texto opcional)      -> aceptado (OCR_VERIFIED)
      - en otro caso                                          -> LOW_CONFIDENCE
    """
    today = today or date.today()
    flags = []
    corroborated: set[str] = set()

    def flag(field, code, message):
        flags.append({"field": field, "code": code, "message": message})
        if field in fields:
            fields[field]["needsReview"] = True
            fields[field].setdefault("reasons", []).append(code)

    for name, cfg in FIELD_MAP.items():
        f = fields.get(name)
        if f is None:
            if cfg["required"]:
                flag(name, "MISSING", f"Campo obligatorio '{name}' no encontrado")
            continue
        f.setdefault("needsReview", False)
        if f["normalized"] is None:
            flag(name, "FORMAT", f"No se pudo interpretar el valor '{f['value']}' como {cfg['kind']}")

    # --- Reglas de formato (corroboran identificadores) ---------------------------------
    for name in ("nit_emisor", "nit_cliente"):
        f = fields.get(name)
        if f and f["normalized"]:
            if re.fullmatch(r"[0-9\s]+", f["value"]) and 5 <= len(f["normalized"]) <= 15:
                corroborated.add(name)
            elif name == "nit_emisor":
                flag(name, "FORMAT", f"NIT con formato inesperado '{f['value']}'")
    f = fields.get("numero_factura")
    if f and f["normalized"] and re.fullmatch(r"[0-9\s-]+", f["value"]):
        corroborated.add("numero_factura")

    iso = (fields.get("fecha") or {}).get("normalized")
    if iso:
        d = date.fromisoformat(iso)
        if d > today:
            flag("fecha", "DATE_FUTURE", f"Fecha {iso} posterior a hoy")
        elif (today - d).days > 365 * max_age_years:
            flag("fecha", "DATE_TOO_OLD", f"Fecha {iso} con más de {max_age_years} años")
        else:
            corroborated.add("fecha")

    # --- Reglas aritméticas (corroboran montos) -----------------------------------------
    total = (fields.get("total") or {}).get("normalized")
    if total is not None and total <= 0:
        flag("total", "FORMAT", "Total no positivo")

    subtotal = (fields.get("subtotal") or {}).get("normalized")
    desc = (fields.get("descuento") or {}).get("normalized") or 0.0
    tax = (fields.get("impuesto") or {}).get("normalized")
    if total is not None and subtotal is not None:
        expected = [subtotal - desc]
        if tax is not None:
            expected.append(subtotal - desc + tax)  # facturas con impuesto no incluido
        if any(_close(e, total) for e in expected):
            corroborated.update({"subtotal", "descuento", "total", "impuesto"})
        else:
            flag("total", "TOTAL_MISMATCH", f"Subtotal {subtotal} - descuento {desc} no cuadra con total {total}")

    base = (fields.get("base_credito_fiscal") or {}).get("normalized")
    if base is not None and total is not None:
        if base > total + 0.05:
            flag("base_credito_fiscal", "BASE_MISMATCH", f"Base crédito fiscal {base} mayor que el total {total}")
        elif _close(base, total):
            corroborated.add("base_credito_fiscal")

    amounts = [li["importe_num"] for li in line_items if li.get("importe_num") is not None]
    if amounts and (subtotal is not None or total is not None):
        s = round(sum(amounts), 2)
        ref_name, ref = ("subtotal", subtotal) if subtotal is not None else ("total", total + desc)
        if _close(s, ref):
            corroborated.add(ref_name)
        else:
            flag(ref_name, "ITEMS_MISMATCH", f"Suma de ítems {s} distinta de {ref_name} {ref}")

    # --- Confianza ----------------------------------------------------------------------
    for name, f in fields.items():
        c = f["confidence"]
        f["corroborated"] = name in corroborated
        if c >= threshold:
            continue
        if c >= min_corroborated and name in corroborated:
            f.setdefault("reasons", []).append("CORROBORATED")
            continue
        optional_text = FIELD_MAP[name]["kind"] == "text" and not FIELD_MAP[name]["required"]
        if (name in corroborated or optional_text) and f.get("ocrConfidence", 0) >= threshold:
            f.setdefault("reasons", []).append("OCR_VERIFIED")  # formato válido + caracteres leídos con alta confianza
            continue
        extra = "" if c < min_corroborated else " y sin regla de negocio que lo corrobore"
        flag(name, "LOW_CONFIDENCE", f"Confianza {c:.1f}% < umbral {threshold:.0f}%{extra}")

    return flags


def summarize_confidence(fields: dict) -> dict:
    confs = [f["confidence"] for f in fields.values()]
    return {
        "avg": round(sum(confs) / len(confs), 2) if confs else 0.0,
        "min": round(min(confs), 2) if confs else 0.0,
        "lowCount": sum(1 for f in fields.values() if f.get("needsReview")),
    }
