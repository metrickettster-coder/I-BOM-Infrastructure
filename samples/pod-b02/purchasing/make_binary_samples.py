"""Regenerate the binary sample files (invoice PDF, receiving XLSX). SAMPLE DATA ONLY.

Needs reportlab and openpyxl: pip install reportlab openpyxl
Run: python3 samples/pod-b02/purchasing/make_binary_samples.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent

INVOICE_LINES = [
    # line, part, description, qty, uom, unit, extended
    (1, "R760XA-CTO-L40S", "PowerEdge R760xa 4x L40S", 2, "EA", 66900.00),
    ("", "S/N: 7XK2Q34, 7XK2Q35"),
    (2, "DCS-7050CX3-32S", "Arista 7050CX3-32S switch", 1, "EA", 21400.00),
    ("", "S/N: JPE26210F1A"),
    (3, "QSFP-100G-SR4", "100G SR4 QSFP transceiver", 8, "EA", 375.00),
    (4, "CAB-Q28-DAC-3M", "100G DAC cable 3m", 4, "EA", 95.00),
    (5, "PDU-SW-0U-17K", "Switched PDU 0U 17.3kW", 2, "EA", 1790.00),
    (6, "PS-PLUS-3Y-NBD", "ProSupport Plus 3Y NBD", 2, "EA", 4200.00),
    (7, "NVAIE-SUB-1Y-GPU", "NVIDIA AI Enterprise per GPU 1Y", 8, "EA", 4500.00),
    (8, "NW-FRT", "Freight and handling", 1, "EA", 912.40),
]


def make_invoice(path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 750

    def line(text, font="Courier", size=8):
        nonlocal y
        c.setFont(font, size)
        c.drawString(40, y, text)
        y -= 13

    line("INVOICE", "Helvetica-Bold", 16)
    line("SAMPLE DATA - synthetic invoice for testing I-BOM ingestion. Not a real bill.", "Helvetica", 8)
    y -= 6
    line("From: Northwind IT Supply (sample)")
    line("Invoice No: INV-88213")
    line("Invoice Date: 2026-08-07")
    line("Customer PO: 4500018823")
    line("Currency: USD")
    y -= 6
    line(f"{'Ln':<4}{'Part':<18}{'Description':<34}{'Qty':>4} {'UOM':<4}{'Unit':>12}{'Amount':>13}")
    total = 0
    for row in INVOICE_LINES:
        if row[0] == "":
            line(f"    {row[1]}")
            continue
        ln, part, desc, qty, uom, unit = row
        amt = qty * unit
        total += amt
        line(f"{ln:<4}{part:<18}{desc:<34}{qty:>4} {uom:<4}{unit:>12,.2f}{amt:>13,.2f}")
    y -= 6
    line(f"{'Total due':<76}{total:>13,.2f}")
    line("Terms: Net 45", "Helvetica", 8)
    c.save()


def make_receipt(path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Goods receipt"
    ws.append(["SAMPLE DATA - synthetic receiving list for testing I-BOM ingestion."])
    ws.append(["Receipt No", "GR-5000044120"])
    ws.append(["Receipt Date", "2026-08-12"])
    ws.append(["Supplier", "Northwind IT Supply (sample)"])
    ws.append([])
    ws.append(["Customer PO", "Mfr Part Number", "Description", "Qty Received", "UOM", "Backorder",
               "Serial Numbers", "Storage Location"])
    rows = [
        ("4500018823", "R760XA-CTO-L40S", "PowerEdge R760xa 4x L40S", 2, "EA", 0, "7XK2Q34;7XK2Q35", "DAL1 receiving dock (sample)"),
        ("4500018823", "DCS-7050CX3-32S", "Arista 7050CX3-32S switch", 1, "EA", 0, "JPE26210F1A", "DAL1 receiving dock (sample)"),
        ("4500018823", "QSFP-100G-SR4", "100G SR4 QSFP transceiver", 8, "EA", 2, "", "DAL1 receiving dock (sample)"),
        ("4500018823", "CAB-Q28-DAC-3M", "100G DAC cable 3m", 4, "EA", 0, "", "DAL1 receiving dock (sample)"),
        ("4500018823", "PDU-SW-0U-17K", "Switched PDU 0U 17.3kW", 2, "EA", 0, "PDU24A0193;PDU24A0194", "DAL1 receiving dock (sample)"),
        ("4500018823", "NW-CON-CAGE", "Cage nuts and screws kit", 2, "KIT", 0, "", "DAL1 receiving dock (sample)"),
    ]
    for r in rows:
        ws.append(list(r))
    wb.save(path)


if __name__ == "__main__":
    make_invoice(HERE / "invoice-INV-88213.pdf")
    make_receipt(HERE / "receipt-GR-5000044120.xlsx")
    print("wrote invoice-INV-88213.pdf and receipt-GR-5000044120.xlsx")
