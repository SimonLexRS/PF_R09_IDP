#!/usr/bin/env python3
"""Genera facturas SINTÉTICAS (empresas ficticias, sin valor fiscal) para las pruebas.

    python3 scripts/generar_facturas.py            # escribe en datos/muestras y datos/errores

Requiere Pillow y reportlab (pip install pillow reportlab). Las muestras ya generadas
están versionadas en el repositorio; este script sirve para regenerarlas o crear más.

Conjunto de prueba (cada archivo ejercita un comportamiento del sistema):
  01_factura_limpia.png              -> PROCESSED (todos los campos con alta confianza)
  02_factura_limpia.pdf              -> PROCESSED (PDF de 1 página)
  03_factura_baja_calidad.jpg        -> NEEDS_REVIEW (borrosa, rotada, comprimida: LOW_CONFIDENCE)
  04_factura_total_inconsistente.png -> NEEDS_REVIEW (TOTAL_MISMATCH / ITEMS_MISMATCH)
  05_factura_duplicada.png           -> NEEDS_REVIEW (DUPLICATE: mismo NIT + N° que 01)
  06_factura_sin_numero.png          -> NEEDS_REVIEW (MISSING numero_factura)
  errores/e1_documento.txt           -> REJECTED en la ingesta (extensión)
  errores/e2_corrupto.pdf            -> FAILED permanente (Textract UnsupportedDocument)
  errores/e3_reintentos.png          -> se sube como incoming/chaos-* -> reintentos -> DLQ
"""
import os
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "datos" / "muestras"
ERR = ROOT / "datos" / "errores"
W, H = 1240, 1754  # A4 a 150 dpi

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]


def font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def money(x):  # formato boliviano 1.234,50
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def words_amount(total):
    entero = int(total)
    cent = round((total - entero) * 100)
    return f"Son: {entero} {cent:02d}/100 Bolivianos"


INVOICES = {
    "A": dict(emisor="FERRETERÍA TORNILLO FELIZ S.R.L. (FICTICIA)", nit="1029384756", numero="1547", fecha="14/10/2026",
              direccion="Av. Ejemplo N° 123, Montero - Santa Cruz", cliente="Agropecuaria Ejemplo Ltda.", nit_cliente="4455667",
              cuf="7A3F9C21B0E45D88AA12CC90F1E6B3D47E2A", items=[(2, "Martillo de carpintero 16 oz", 85.00), (10, "Caja de clavos 2\"", 12.50),
                                                              (1, "Taladro percutor 650 W", 420.00), (4, "Guantes de cuero", 22.00)], descuento=20.00),
    "B": dict(emisor="INSUMOS AGRÍCOLAS EL PALMAR S.A. (FICTICIA)", nit="3344556677", numero="88213", fecha="02/10/2026",
              direccion="Calle Imaginaria 45, Santa Cruz de la Sierra", cliente="Juan Pérez Muestra", nit_cliente="6789012",
              cuf="B91C0D2E3F4A5B6C7D8E9F00112233445566", items=[(3, "Semilla de soya certificada 40 kg", 610.00), (5, "Fertilizante NPK 50 kg", 295.00)], descuento=0.0),
    "C": dict(emisor="LIBRERÍA PAPEL Y TINTA S.R.L. (FICTICIA)", nit="5566778899", numero="30422", fecha="20/09/2026",
              direccion="Av. Ficticia 900, Cochabamba", cliente="Universidad de Prueba", nit_cliente="1020304050",
              cuf="C0FFEE00112233445566778899AABBCCDDEE", items=[(20, "Cuaderno universitario 100 hojas", 14.00), (50, "Bolígrafo azul", 3.50),
                                                              (5, "Resma papel carta 75 g", 48.00)], descuento=15.00),
    "D": dict(emisor="SERVICIOS TÉCNICOS RED NORTE S.R.L. (FICTICIA)", nit="7788990011", numero="1203", fecha="10/10/2026",
              direccion="Calle de Muestra 12, Warnes", cliente="Cooperativa Demo", nit_cliente="2233445",
              cuf="D1D2D3D4D5D6D7D8D9DADBDCDDDEDFE0E1E2", items=[(1, "Mantenimiento de router", 250.00), (3, "Cable UTP Cat6 (m)", 4.00), (2, "Conector RJ45 (bolsa)", 18.00)], descuento=0.0),
    "E": dict(emisor="PANADERÍA EL TRIGAL DORADO (FICTICIA)", nit="9900112233", numero="", fecha="18/10/2026",
              direccion="Calle Prueba 7, Montero", cliente="Sin nombre", nit_cliente="0",
              cuf="E0E1E2E3E4E5E6E7E8E9EAEBECEDEEEFF0F1", items=[(30, "Pan de batalla", 0.50), (2, "Torta de chocolate", 95.00)], descuento=0.0),
}


def render(inv, total_override=None, show_number=True):
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_t, f_b, f_n, f_s = font(34, True), font(26, True), font(25), font(20)
    y = 70
    d.text((80, y), inv["emisor"], font=f_t, fill="black"); y += 55
    d.text((80, y), inv["direccion"], font=f_n, fill="black"); y += 45
    d.text((80, y), "Teléfono: 000-00000 (ficticio)", font=f_n, fill="black")
    # Recuadro derecho tipo factura boliviana
    d.rectangle((740, 180, 1185, 380), outline="black", width=3)
    d.text((760, 200), "NIT", font=f_b, fill="black"); d.text((960, 200), inv["nit"], font=f_n, fill="black")
    if show_number:
        d.text((760, 250), "Factura N°", font=f_b, fill="black"); d.text((960, 250), inv["numero"], font=f_n, fill="black")
    d.text((760, 300), "Cód. Autorización", font=f_s, fill="black"); d.text((760, 335), inv["cuf"], font=font(16), fill="black")
    y = 420
    d.text((W // 2 - 90, y), "FACTURA", font=font(44, True), fill="black"); y += 60
    d.text((W // 2 - 190, y), "(Con Derecho a Crédito Fiscal)", font=f_n, fill="black"); y += 70
    d.text((80, y), "Fecha:", font=f_b, fill="black"); d.text((300, y), inv["fecha"], font=f_n, fill="black"); y += 45
    d.text((80, y), "Señor(es):", font=f_b, fill="black"); d.text((300, y), inv["cliente"], font=f_n, fill="black"); y += 45
    d.text((80, y), "NIT/CI/CEX:", font=f_b, fill="black"); d.text((300, y), inv["nit_cliente"], font=f_n, fill="black"); y += 70

    cols = [80, 230, 820, 1000]
    d.rectangle((70, y - 10, 1170, y + 40), fill="#e6e6e6")
    for x, h in zip(cols, ["CANTIDAD", "DESCRIPCIÓN", "P. UNITARIO", "SUBTOTAL"]):
        d.text((x, y), h, font=font(22, True), fill="black")
    y += 60
    subtotal = 0.0
    for qty, desc, pu in inv["items"]:
        imp = round(qty * pu, 2)
        subtotal += imp
        d.text((cols[0] + 20, y), str(qty), font=f_n, fill="black")
        d.text((cols[1], y), desc, font=f_n, fill="black")
        d.text((cols[2], y), money(pu), font=f_n, fill="black")
        d.text((cols[3], y), money(imp), font=f_n, fill="black")
        y += 45
    d.line((70, y, 1170, y), fill="black", width=2); y += 25
    total = round(subtotal - inv["descuento"], 2) if total_override is None else total_override
    for label, val in [("SUBTOTAL Bs", subtotal), ("DESCUENTO Bs", inv["descuento"]), ("TOTAL Bs", total),
                       ("IMPORTE BASE CRÉDITO FISCAL Bs", total)]:
        d.text((430, y), label, font=f_b, fill="black"); d.text((cols[3], y), money(val), font=f_n, fill="black"); y += 45
    y += 20
    d.text((80, y), words_amount(total), font=f_n, fill="black"); y += 80
    d.text((80, y), "ESTA FACTURA CONTRIBUYE AL DESARROLLO DEL PAÍS (texto de ejemplo)", font=f_s, fill="black")
    # Marca de agua en el pie: deja claro que es material de prueba
    d.rectangle((0, H - 90, W, H), fill="#f3f3f3")
    d.text((80, H - 70), "MUESTRA SINTÉTICA PARA PRUEBAS · EMPRESA FICTICIA · SIN VALOR FISCAL", font=f_s, fill="#555")
    return img, subtotal, total


def degrade(img, seed=7):
    rnd = random.Random(seed)
    small = img.resize((W * 45 // 100, H * 45 // 100), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(1.1)).rotate(2.5, expand=True, fillcolor="white")
    px = small.load()
    for _ in range(small.size[0] * small.size[1] // 25):
        x, y = rnd.randrange(small.size[0]), rnd.randrange(small.size[1])
        g = rnd.randint(120, 220)
        px[x, y] = (g, g, g)
    return small


def render_pdf(inv, path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    img, _, _ = render(inv)
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(str(tmp), 0, 0, width=A4[0], height=A4[1])
    c.setFont("Helvetica", 6)
    c.drawString(20, 10, "Muestra sintetica - empresa ficticia - sin valor fiscal")
    c.save()
    tmp.unlink()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ERR.mkdir(parents=True, exist_ok=True)
    render(INVOICES["A"])[0].save(OUT / "01_factura_limpia.png", optimize=True)
    render_pdf(INVOICES["B"], OUT / "02_factura_limpia.pdf")
    degrade(render(INVOICES["C"])[0]).save(OUT / "03_factura_baja_calidad.jpg", quality=25)
    img, sub, _ = render(INVOICES["D"], total_override=356.00)   # real: 250 + 12 + 36 = 298,00
    img.save(OUT / "04_factura_total_inconsistente.png", optimize=True)
    dup = INVOICES["A"].copy(); dup["fecha"] = "15/10/2026"     # mismo NIT + N° que 01 (reenvío)
    render(dup)[0].rotate(0.8, expand=True, fillcolor="white").save(OUT / "05_factura_duplicada.png", optimize=True)
    render(INVOICES["E"], show_number=False)[0].save(OUT / "06_factura_sin_numero.png", optimize=True)

    (ERR / "e1_documento.txt").write_text("Esto no es una factura: tipo de archivo no permitido.\n", encoding="utf-8")
    rnd = random.Random(42)
    (ERR / "e2_corrupto.pdf").write_bytes(b"%PDF-1.4\n" + bytes(rnd.randrange(256) for _ in range(4096)))
    c = INVOICES["B"].copy(); c["numero"] = "99001"
    render(c)[0].save(ERR / "e3_reintentos.png", optimize=True)
    for p in sorted(list(OUT.iterdir()) + list(ERR.iterdir())):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    sys.exit(main())
