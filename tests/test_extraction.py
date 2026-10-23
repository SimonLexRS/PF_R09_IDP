from datetime import date

import pytest
from conftest import CLEAN, expense_response

import extraction as ex


@pytest.mark.parametrize("txt,val", [("Bs 1.234,50", 1234.5), ("1,234.50", 1234.5), ("783,00", 783.0), ("350", 350.0),
                                     ("12.500", 12500.0), ("Bs. 0,50", 0.5), ("abc", None), (None, None)])
def test_parse_amount(txt, val):
    assert ex.parse_amount(txt) == val


@pytest.mark.parametrize("txt,val", [("14/10/2026", "2026-10-14"), ("2026-10-14", "2026-10-14"), ("14 de octubre de 2026", "2026-10-14"),
                                     ("31/02/2026", None), ("sin fecha", None)])
def test_parse_date(txt, val):
    assert ex.parse_date(txt) == val


def test_clean_invoice_has_no_flags():
    p = ex.parse_expense(CLEAN)
    f = p["fields"]
    assert f["numero_factura"]["normalized"] == "1547"
    assert f["nit_emisor"]["normalized"] == "1029384756"      # etiqueta "NIT" (tipo OTHER)
    assert f["nit_cliente"]["normalized"] == "4455667"        # etiqueta "NIT/CI/CEX" no se confunde con el emisor
    assert f["total"]["normalized"] == 783.0
    assert ex.validate(f, p["line_items"], 90, today=date(2026, 10, 21)) == []


def test_low_confidence_and_total_mismatch():
    r = expense_response([("INVOICE_RECEIPT_ID", "Factura N°", "1203", 62.0), ("INVOICE_RECEIPT_DATE", "Fecha", "10/10/2026", 99.0),
                          ("VENDOR_NAME", None, "SERVICIOS", 99.0), ("OTHER", "NIT", "7788990011", 99.0),
                          ("SUBTOTAL", "SUBTOTAL Bs", "298,00", 99.0), ("TOTAL", "TOTAL Bs", "356,00", 99.0)])
    p = ex.parse_expense(r)
    codes = {(fl["field"], fl["code"]) for fl in ex.validate(p["fields"], p["line_items"], 90, today=date(2026, 10, 21))}
    assert ("numero_factura", "LOW_CONFIDENCE") in codes
    assert ("total", "TOTAL_MISMATCH") in codes
    assert p["fields"]["numero_factura"]["needsReview"] is True


def test_missing_number_and_regex_fallback():
    r = expense_response([("VENDOR_NAME", None, "PANADERÍA", 99.0), ("TOTAL", "TOTAL Bs", "205,00", 99.0)],
                         lines=[("NIT 9900112233", 98.0), ("Fecha: 18/10/2026", 97.0)])
    p = ex.parse_expense(r)
    assert p["fields"]["nit_emisor"]["source"] == "regex"
    assert p["fields"]["fecha"]["normalized"] == "2026-10-18"
    codes = [fl["code"] for fl in ex.validate(p["fields"], p["line_items"], 90, today=date(2026, 10, 21))]
    assert codes == ["MISSING"]


def test_future_date_flag():
    r = expense_response([("INVOICE_RECEIPT_DATE", "Fecha", "01/01/2030", 99.0)])
    p = ex.parse_expense(r)
    codes = [fl["code"] for fl in ex.validate(p["fields"], [], 90, today=date(2026, 10, 21))]
    assert "DATE_FUTURE" in codes


def test_low_label_confidence_does_not_flag_and_base_cf_not_tax():
    """Caso real del Learner Lab: etiqueta 'NIT' leída con 50 % pero valor con 99 %;
    Textract tipifica 'IMPORTE BASE CRÉDITO FISCAL' como TAX."""
    r = expense_response([("INVOICE_RECEIPT_ID", "Factura N°", "1547", 98.6), ("INVOICE_RECEIPT_DATE", "Fecha:", "14/10/2026", 99.9),
                          ("VENDOR_NAME", None, "FERRETERÍA", 99.7), ("OTHER", "NIT", "1029384756", 99.2),
                          ("SUBTOTAL", "SUBTOTAL Bs", "803,00", 99.5), ("DISCOUNT", "DESCUENTO Bs", "20,00", 99.8),
                          ("TAX", "IMPORTE BASE CRÉDITO FISCAL Bs", "783,00", 99.0), ("TOTAL", "TOTAL Bs", "783,00", 99.9)])
    r["ExpenseDocuments"][0]["SummaryFields"][3]["LabelDetection"]["Confidence"] = 50.7
    p = ex.parse_expense(r)
    assert p["fields"]["nit_emisor"]["confidence"] == 99.2
    assert p["fields"]["nit_emisor"]["mapConfidence"] == 50.7
    assert "impuesto" not in p["fields"]
    assert p["fields"]["base_credito_fiscal"]["normalized"] == 783.0
    assert ex.validate(p["fields"], p["line_items"], 90, today=date(2026, 10, 21)) == []


def test_corroborated_medium_confidence_is_accepted():
    """Caso real (05/01 en el Learner Lab): NIT 76,4 % y base crédito fiscal 78,9 %.
    El NIT tiene formato estricto y la base cuadra con el total -> no se marcan."""
    r = expense_response([("INVOICE_RECEIPT_ID", "Factura N°", "1547", 99.0), ("INVOICE_RECEIPT_DATE", "Fecha:", "15/10/2026", 99.9),
                          ("VENDOR_NAME", None, "FERRETERÍA", 99.6), ("OTHER", "NIT", "1029384756", 76.4),
                          ("SUBTOTAL", "SUBTOTAL Bs", "803,00", 99.9), ("DISCOUNT", "DESCUENTO Bs", "20,00", 99.7),
                          ("TAX", "IMPORTE BASE CRÉDITO FISCAL Bs", "783,00", 78.9), ("TOTAL", "TOTAL Bs", "783,00", 99.8)])
    p = ex.parse_expense(r)
    assert ex.validate(p["fields"], p["line_items"], 90, today=date(2026, 10, 22)) == []
    assert p["fields"]["nit_emisor"]["corroborated"] is True
    assert "CORROBORATED" in p["fields"]["base_credito_fiscal"]["reasons"]


def test_medium_confidence_without_corroboration_is_flagged():
    r = expense_response([("OTHER", "NIT", "1O29384756", 80.0), ("TOTAL", "TOTAL Bs", "783,00", 80.0),
                          ("SUBTOTAL", "SUBTOTAL Bs", "800,00", 99.0)])   # 'O' en vez de 0 y total que no cuadra
    p = ex.parse_expense(r)
    codes = {(f["field"], f["code"]) for f in ex.validate(p["fields"], [], 90, today=date(2026, 10, 22))}
    assert ("nit_emisor", "FORMAT") in codes and ("nit_emisor", "LOW_CONFIDENCE") in codes
    assert ("total", "TOTAL_MISMATCH") in codes and ("total", "LOW_CONFIDENCE") in codes


def test_ocr_verified_identifier():
    """Caso real (01 en el Learner Lab): NIT con confianza de campo 50,7 % pero el OCR
    del número exacto ~99 % y formato válido -> OCR_VERIFIED, sin bandera."""
    r = expense_response([("INVOICE_RECEIPT_ID", "Factura N°", "1547", 98.6), ("INVOICE_RECEIPT_DATE", "Fecha:", "14/10/2026", 99.9),
                          ("VENDOR_NAME", None, "FERRETERÍA", 99.7), ("OTHER", "NIT", "1029384756", 50.7),
                          ("TOTAL", "TOTAL Bs", "783,00", 99.9)])
    r["ExpenseDocuments"][0]["Blocks"] = [{"BlockType": "WORD", "Text": "1029384756", "Confidence": 99.4}]
    p = ex.parse_expense(r)
    assert p["fields"]["nit_emisor"]["ocrConfidence"] == 99.4
    assert ex.validate(p["fields"], [], 90, today=date(2026, 10, 22)) == []
    assert "OCR_VERIFIED" in p["fields"]["nit_emisor"]["reasons"]
    # sin respaldo OCR el mismo caso sí se marca
    r["ExpenseDocuments"][0]["Blocks"] = [{"BlockType": "WORD", "Text": "1029384756", "Confidence": 60.0}]
    p = ex.parse_expense(r)
    assert [f["code"] for f in ex.validate(p["fields"], [], 90, today=date(2026, 10, 22))] == ["LOW_CONFIDENCE"]


def test_real_textract_response_02_clean_invoice():
    """Respuesta REAL de Textract AnalyzeExpense (Learner Lab, 22/10/2026) para 02_factura_limpia.pdf."""
    import json
    from pathlib import Path
    r = json.loads((Path(__file__).parent / "fixtures" / "textract_02_factura_limpia.json").read_text(encoding="utf-8"))
    p = ex.parse_expense(r)
    f = p["fields"]
    assert f["numero_factura"]["normalized"] == "88213"
    assert f["nit_emisor"]["normalized"] == "3344556677"
    assert f["fecha"]["normalized"] == "2026-10-02"
    assert f["total"]["label"] == "TOTAL Bs"                       # no "IMPORTE BASE CRÉDITO FISCAL"
    assert f["base_credito_fiscal"]["normalized"] == 3305.0
    assert ex.validate(f, p["line_items"], 90, today=date(2026, 10, 22)) == []   # -> PROCESSED
    assert "OCR_VERIFIED" in f["cliente"]["reasons"]
