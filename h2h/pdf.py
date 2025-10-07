from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

def build_ticket_pdf(*, order_id: str, user_name: str, package_name: str, amount_inr: int) -> bytes:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setFont("Helvetica-Bold", 20)
    p.drawString(30*mm, (height - 40*mm), "Highway to Heal — Travel Pass")

    p.setFont("Helvetica", 12)
    y = height - 60*mm
    p.drawString(30*mm, y, f"Order: {order_id}")
    y -= 10*mm
    p.drawString(30*mm, y, f"Name: {user_name}")
    y -= 10*mm
    p.drawString(30*mm, y, f"Package: {package_name}")
    y -= 10*mm
    p.drawString(30*mm, y, f"Amount Paid: ₹{amount_inr}")

    p.setFont("Helvetica-Oblique", 10)
    p.drawString(30*mm, 20*mm, "Present this PDF at the event gate with a valid ID.")

    p.showPage()
    p.save()
    return buffer.getvalue()
