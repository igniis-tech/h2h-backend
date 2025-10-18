# pdf.py
from __future__ import annotations

import os
from io import BytesIO
from datetime import datetime
from typing import Optional, List, Dict, Tuple

from django.conf import settings
from django.contrib.staticfiles import finders

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .models import Order

# ---------------------------------------------------------------------
# Font utilities (₹ safe)
# ---------------------------------------------------------------------
_FONT_READY = False

def ensure_unicode_font() -> bool:
    """Register DejaVu (supports ₹). Return True if available."""
    global _FONT_READY
    if _FONT_READY:
        return True

    ttf_path = finders.find("DejaVuSans.ttf")
    if not ttf_path and getattr(settings, "STATIC_ROOT", None):
        cand = os.path.join(settings.STATIC_ROOT, "DejaVuSans.ttf")
        if os.path.exists(cand):
            ttf_path = cand
    if not ttf_path:
        for base in getattr(settings, "STATICFILES_DIRS", []):
            cand = os.path.join(base, "DejaVuSans.ttf")
            if os.path.exists(cand):
                ttf_path = cand
                break
    if not ttf_path:
        cand = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
        if os.path.exists(cand):
            ttf_path = cand

    if not ttf_path:
        return False

    try:
        pdfmetrics.registerFont(TTFont("DejaVu", ttf_path))
        _FONT_READY = True
        return True
    except Exception:
        return False


def money(value_rupees: float | int) -> str:
    """Format money with ₹ when font is available; otherwise Rs."""
    if ensure_unicode_font():
        return f"₹ {value_rupees:,.0f}"
    return f"Rs. {value_rupees:,.0f}"


# ---------------------------------------------------------------------
# Number → words (Indian system)
# ---------------------------------------------------------------------
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
         "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

def _two_digits(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return f"{_TENS[n//10]}{(' ' + _ONES[n%10]) if (n%10) else ''}"

def _three_digits(n: int) -> str:
    h, r = divmod(n, 100)
    s = ""
    if h:
        s += f"{_ONES[h]} Hundred"
        if r:
            s += " "
    if r:
        s += _two_digits(r)
    return s.strip()

def inr_to_words(n: int) -> str:
    """Convert integer rupees to words in Indian system (Crore/Lakh/Thousand/Hundred)."""
    if n == 0:
        return "Zero Rupees"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(f"{_two_digits(crore)} Crore")
    if lakh:
        parts.append(f"{_two_digits(lakh)} Lakh")
    if thousand:
        parts.append(f"{_two_digits(thousand)} Thousand")
    if n:
        parts.append(_three_digits(n))
    return (" ".join(parts) + " Rupees").strip()


# ---------------------------------------------------------------------
# Static lookup + drawing helpers
# ---------------------------------------------------------------------
def _static_path(filename: str) -> Optional[str]:
    """Return an absolute path for images.
    - If `filename` is an absolute path and exists, use it directly.
    - Otherwise fall back to Django staticfiles finders and common dirs.
    """
    if not filename:
        return None

    # Absolute path given (Windows or POSIX)
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename

    # Try Django finders / static roots
    p = finders.find(filename)
    if p:
        return p

    if getattr(settings, "STATIC_ROOT", None):
        cand = os.path.join(settings.STATIC_ROOT, filename)
        if os.path.exists(cand):
            return cand

    for base in getattr(settings, "STATICFILES_DIRS", []):
        cand = os.path.join(base, filename)
        if os.path.exists(cand):
            return cand

    # Last resort: try common case-variants
    for alt in (filename.lower(), filename.upper()):
        p = finders.find(alt)
        if p:
            return p
    return None


def _text_width(c: canvas.Canvas, text: str, font: str, size: float) -> float:
    c.saveState()
    c.setFont(font, size)
    w = c.stringWidth(text, font, size)
    c.restoreState()
    return w

def _wrap_text(c: canvas.Canvas, text: str, max_w: float, font: str, size: float) -> List[str]:
    """Simple word-wrap that avoids mid-word breaks."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if _text_width(c, cand, font, size) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def _sanitize_colors(node):
    """Force any None fill/stroke colors in a Drawing tree to black to avoid 'Unknown color None'."""
    try:
        if hasattr(node, "fillColor") and node.fillColor is None:
            node.fillColor = colors.black
        if hasattr(node, "strokeColor") and node.strokeColor is None:
            node.strokeColor = colors.black
    except Exception:
        pass
    for attr in ("contents", "children", "nodes"):
        kids = getattr(node, attr, None)
        if kids:
            for k in kids:
                _sanitize_colors(k)

def _draw_qr(c: canvas.Canvas, data: str, x: float, y: float, size: float = 35*mm):
    widget = qr.QrCodeWidget(data)
    bx, by, bw, bh = widget.getBounds()
    d = Drawing(size, size, transform=[size/(bw-bx), 0, 0, size/(bh-by), 0, 0])
    d.add(widget)
    _sanitize_colors(d)
    renderPDF.draw(d, c, x, y)

def _safe_img(c: canvas.Canvas, path: Optional[str], x: float, y: float,
              w: float, h: float, keep_aspect: bool = True) -> bool:
    if not path:
        return False
    try:
        img = ImageReader(path)
        if keep_aspect:
            iw, ih = img.getSize()
            r = min(w / iw, h / ih)
            rw, rh = iw * r, ih * r
            c.drawImage(img, x + (w - rw) / 2.0, y + (h - rh) / 2.0, rw, rh, mask='auto')
        else:
            c.drawImage(img, x, y, w, h, mask='auto')
        return True
    except Exception:
        return False

def _logo_or_placeholder(c: canvas.Canvas, filename: Optional[str], x: float, y: float, w: float, h: float):
    """Try draw logo; if not found, draw a subtle placeholder box with 'Highway to Heal'."""
    p = _static_path(filename) if filename else None
    ok = _safe_img(c, p, x, y, w, h, keep_aspect=True)
    if ok:
        return
    # Placeholder
    c.saveState()
    c.setStrokeColor(colors.Color(0.85, 0.87, 0.90))
    c.roundRect(x, y, w, h, 2.5*mm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.Color(0.30, 0.32, 0.36))
    c.drawCentredString(x + w/2.0, y + h/2.0 - 4, "Highway to Heal")
    c.restoreState()


# ---------------------------------------------------------------------
# PAGE 1: INVOICE (clean layout)
# ---------------------------------------------------------------------
def _draw_invoice_page(
    c,
    *,
    logo_filename: str | None,
    invoice_title: str,
    order_id: str,
    booking_date: datetime,
    billed_to_name: str,
    billed_to_email: str | None,
    contact_phone: str | None,
    items: List[Dict[str, float]],
    taxes_fees: List[Dict[str, float]],
    grand_total_rupees: int,
    meta_right: Dict[str, str],
    qr_payload: str,
):
    W, H = A4

    # Palette
    COL_BG_HEADER = colors.Color(0.972, 0.976, 0.984)    # #F8FAFC
    COL_PANEL_BORDER = colors.Color(0.898, 0.91, 0.925)  # #E5E7EB
    COL_TABLE_HEAD = colors.Color(0.953, 0.957, 0.965)   # #F3F4F6
    COL_TEXT = colors.black
    COL_MUTE = colors.Color(0.40, 0.44, 0.50)
    COL_ACCENT = colors.Color(0.15, 0.16, 0.20)

    # Font family
    unicode_ok = ensure_unicode_font()
    BODY = "DejaVu" if unicode_ok else "Helvetica"
    BOLD = BODY

    # Header band
    c.setFillColor(COL_BG_HEADER)
    c.rect(0, H - 36*mm, W, 36*mm, stroke=0, fill=1)

    # Logo (with placeholder fallback)
    _logo_or_placeholder(c, logo_filename, 14*mm, H - 30*mm, 42*mm, 22*mm)

    # Right meta box
    box_w, box_h = 70*mm, 26*mm
    box_x, box_y = W - 14*mm - box_w, H - 30*mm
    c.setStrokeColor(COL_PANEL_BORDER)
    c.setLineWidth(1)
    c.roundRect(box_x, box_y, box_w, box_h, 3*mm, stroke=1, fill=0)

    c.setFillColor(COL_ACCENT); c.setFont(BOLD, 15)
    c.drawRightString(box_x + box_w - 6*mm, box_y + box_h - 8*mm, invoice_title.upper())
    c.setFillColor(COL_TEXT); c.setFont(BODY, 9.2)
    c.drawRightString(box_x + box_w - 6*mm, box_y + box_h - 16*mm, f"Invoice Date: {booking_date:%d %b %Y}")
    c.drawRightString(box_x + box_w - 6*mm, box_y + 7*mm, f"Order ID: {order_id}")

    # Billed To / Booking Details panel
    top_panel_y = H - 54*mm
    c.setStrokeColor(COL_PANEL_BORDER)
    c.roundRect(14*mm, top_panel_y - 30*mm, W - 28*mm, 30*mm, 3*mm, stroke=1, fill=0)

    divider_x = 14*mm + (W - 28*mm) * 0.55
    c.line(divider_x, top_panel_y - 30*mm, divider_x, top_panel_y)

    # Left: billed to
    c.setFont(BOLD, 10.5); c.setFillColor(COL_ACCENT)
    c.drawString(20*mm, top_panel_y - 7*mm, "Billed To")
    c.setFont(BODY, 9.2); c.setFillColor(COL_TEXT)
    cursor = top_panel_y - 13*mm
    c.drawString(20*mm, cursor, billed_to_name); cursor -= 6*mm
    if billed_to_email:
        c.setFillColor(COL_MUTE); c.drawString(20*mm, cursor, billed_to_email); cursor -= 6*mm
    if contact_phone:
        c.setFillColor(COL_MUTE); c.drawString(20*mm, cursor, f"Phone: {contact_phone}")

    # Right: meta
    c.setFont(BOLD, 10.5); c.setFillColor(COL_ACCENT)
    c.drawRightString(W - 20*mm, top_panel_y - 7*mm, "Booking Details")
    c.setFont(BODY, 9.2); c.setFillColor(COL_TEXT)
    ry = top_panel_y - 13*mm
    for k, v in meta_right.items():
        c.setFillColor(COL_MUTE)
        c.drawRightString(W - 20*mm, ry, f"{k}: {v}")
        ry -= 6*mm

    # Items table
    tbl_top = top_panel_y - 40*mm
    tbl_h = 70*mm
    c.setStrokeColor(COL_PANEL_BORDER)
    c.roundRect(14*mm, tbl_top - tbl_h, W - 28*mm, tbl_h, 3*mm, stroke=1, fill=0)

    c.setFillColor(COL_TABLE_HEAD)
    c.rect(14*mm + 0.5*mm, tbl_top - 10*mm, W - 29*mm, 10*mm, stroke=0, fill=1)

    c.setFillColor(COL_ACCENT); c.setFont(BOLD, 10)
    c.drawString(20*mm, tbl_top - 6.5*mm, "Description")
    c.drawRightString(W - 20*mm, tbl_top - 6.5*mm, "Amount")

    c.setFillColor(COL_TEXT); c.setFont(BODY, 9.2)
    y = tbl_top - 16*mm
    row_gap = 8*mm
    for row in items:
        c.drawString(20*mm, y, row["label"])
        c.drawRightString(W - 20*mm, y, money(row["amount"]))
        y -= row_gap

    for row in taxes_fees:
        c.setFillColor(COL_MUTE)
        c.drawString(20*mm, y, row["label"])
        c.drawRightString(W - 20*mm, y, money(row["amount"]))
        c.setFillColor(COL_TEXT)
        y -= row_gap

    c.setStrokeColor(COL_PANEL_BORDER)
    c.line(20*mm, y - 2*mm, W - 20*mm, y - 2*mm)
    y -= 9*mm

    c.setFillColor(COL_ACCENT); c.setFont(BOLD, 11.5)
    c.drawString(20*mm, y, "GRAND TOTAL")
    c.drawRightString(W - 20*mm, y, money(grand_total_rupees))

    words = inr_to_words(int(grand_total_rupees)).upper()
    c.setFillColor(COL_BG_HEADER)
    c.roundRect(14*mm, y - 14*mm, W - 28*mm, 10*mm, 2*mm, stroke=0, fill=1)
    c.setFillColor(COL_MUTE); c.setFont(BODY, 8.6)
    c.drawString(20*mm, y - 11*mm, f"Grand Total (in words): {words} ONLY")

    _draw_qr(c, qr_payload, W - 20*mm - 28*mm, 14*mm, size=28*mm)
    c.setFillColor(COL_MUTE); c.setFont(BODY, 8)
    c.drawRightString(W - 20*mm, 12*mm, "Scan to verify booking")

    c.setStrokeColor(COL_PANEL_BORDER)
    c.roundRect(14*mm, 9*mm, W - 28*mm, 20*mm, 3*mm, stroke=1, fill=0)
    c.setFillColor(COL_ACCENT); c.setFont(BOLD, 9.8)
    c.drawString(20*mm, 25*mm, "Highway to Heal — Customer Support")
    c.setFillColor(COL_MUTE); c.setFont(BODY, 8.6)
    c.drawString(20*mm, 20*mm, "Email: support@highwaytoheal.example   •   Phone: +91-XXXXXXXXXX")
    c.drawString(20*mm, 15*mm, "Note: This is a system-generated invoice; signature not required.")


# ---------------------------------------------------------------------
# PAGE 2: PASS (portrait badge like mock, with wrapping)
# ---------------------------------------------------------------------

# def _draw_pass_page(
#     c: canvas.Canvas,
#     *,
#     bg_filename: Optional[str],
#     event_title: str,
#     pass_label: str,
#     attendee: str,
#     order_id: str,
#     amount_rupees: int,
#     dates: Optional[str],
#     venue: Optional[str],
#     qr_payload: str
# ):
#     W, H = A4

#     # Badge size
#     CARD_W = 100 * mm
#     CARD_H = 165 * mm
#     card_x = (W - CARD_W) / 2.0
#     card_y = (H - CARD_H) / 2.0
#     RADIUS = 10 * mm

#     COL_PINK = colors.Color(0.93, 0.45, 0.62)
#     COL_MUTE = colors.Color(0.82, 0.84, 0.86)
#     COL_WHITE = colors.white
#     COL_CYAN  = colors.Color(0.0, 0.66, 0.82)

#     # Full-bleed background clipped to rounded card
#     c.saveState()
#     p = c.beginPath(); p.roundRect(card_x, card_y, CARD_W, CARD_H, RADIUS)
#     c.clipPath(p, stroke=0, fill=0)
#     img_path = _static_path(bg_filename) if bg_filename else None
#     if img_path:
#         _safe_img(c, img_path, card_x, card_y, CARD_W, CARD_H, keep_aspect=False)
#     else:
#         c.setFillColor(colors.Color(0.059, 0.082, 0.090)); c.rect(card_x, card_y, CARD_W, CARD_H, 0, 1)
#     c.restoreState()
#     c.setLineWidth(1); c.setStrokeColor(colors.black)
#     c.roundRect(card_x, card_y, CARD_W, CARD_H, RADIUS, stroke=1, fill=0)

#     # Lanyard slot
#     slot_w, slot_h = 18*mm, 5*mm
#     slot_x = card_x + (CARD_W - slot_w) / 2.0
#     slot_y = card_y + CARD_H - 13*mm
#     c.setFillColor(colors.Color(0.15, 0.17, 0.18))
#     c.roundRect(slot_x, slot_y, slot_w, slot_h, 2*mm, stroke=0, fill=1)

#     # ---- Top stack: H2H (top), then Name, then Subtitle ----
#     cx = card_x + CARD_W/2.0
#     top_anchor = card_y + CARD_H - 30*mm  # below the slot

#     # H2H circle
#     c.setStrokeColor(COL_WHITE); c.setLineWidth(2)
#     c.circle(cx, top_anchor, 9*mm, stroke=1, fill=0)
#     c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 10)
#     c.drawCentredString(cx, top_anchor - 3*mm, "H2H")

#     # Name — add extra gap from H2H (was 14mm → 20mm)
#     c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 20)
#     c.drawCentredString(cx, top_anchor - 20*mm, attendee)

#     # Subtitle (push down a bit too)
#     c.setFillColor(COL_PINK); c.setFont("Helvetica-Bold", 10.5)
#     c.drawCentredString(cx, top_anchor - 30*mm, f"{event_title} – {pass_label}".upper())

#     # ---- Content area (drop ~6–10 mm lower) ----
#     left_x  = card_x + 12*mm
#     right_x = card_x + CARD_W - 12*mm
#     content_top = top_anchor - 40*mm   # (was 30–36mm) → lower

#     # DATE
#     c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
#     c.drawString(left_x, content_top, "DATE")
#     c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 12)
#     c.drawString(left_x, content_top - 6.5*mm, (dates or "TBA"))

#     # VENUE (wrap to max 3 lines)
#     c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
#     c.drawString(left_x, content_top - 15*mm, "VENUE")
#     c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 10.8)
#     venue_text = (venue or "Venue TBA")
#     max_w = (CARD_W * 0.58) - 12*mm
#     lines = _wrap_text(c, venue_text, max_w, "Helvetica-Bold", 10.8)
#     vy = content_top - 21*mm
#     for line in lines[:3]:
#         c.drawString(left_x, vy, line)
#         vy -= 6.2*mm

#     # SEAT
#     c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
#     c.drawString(left_x, vy - 3.5*mm, "SEAT")
#     c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 10.8)
#     c.drawString(left_x, vy - 10*mm, "VIP / Zone A")
#     c.drawString(left_x, vy - 16*mm, "A-17")

#     # ---- Smaller QR on the right, nudged further DOWN ----
#     tile = 32*mm       # was 34mm
#     tile_x = right_x - tile
#     left_bottom = vy - 18*mm
#     left_top    = content_top
#     tile_y = left_bottom + (left_top - left_bottom - tile) / 2.0
#     tile_y -= 4*mm     # extra push downward
#     tile_y = max(tile_y, card_y + 28*mm)
#     tile_y = min(tile_y, card_y + CARD_H - 28*mm - tile)

#     c.setFillColor(COL_WHITE)
#     c.roundRect(tile_x - 3*mm, tile_y - 3*mm, tile + 6*mm, tile + 6*mm, 4*mm, stroke=0, fill=1)
#     _draw_qr(c, qr_payload, tile_x, tile_y, size=tile)

#     # ---- Bottom divider + meta (unchanged) ----
#     bottom_safe = 22*mm
#     c.setStrokeColor(COL_MUTE); c.setDash(2, 3)
#     c.line(card_x + 10*mm, card_y + bottom_safe, card_x + CARD_W - 10*mm, card_y + bottom_safe)
#     c.setDash(1, 0)

#     c.setFillColor(COL_MUTE); c.setFont("Helvetica", 9)
#     c.drawString(card_x + 12*mm, card_y + bottom_safe - 8*mm, f"ORDER ID  {order_id}")

#     c.setStrokeColor(COL_CYAN); c.setLineWidth(1.2)
#     gx = card_x + CARD_W - 12*mm; gy = card_y + bottom_safe - 8*mm
#     c.circle(gx, gy, 4*mm, stroke=1, fill=0)
#     c.line(gx-2.5*mm, gy, gx+1.2*mm, gy)
#     c.line(gx-1.5*mm, gy+2*mm, gx+2.2*mm, gy+2*mm)
#     c.line(gx-1.5*mm, gy-2*mm, gx+2.2*mm, gy-2*mm)


def _draw_pass_page(
    c: canvas.Canvas,
    *,
    bg_filename: Optional[str],
    event_title: str,
    pass_label: str,
    attendee: str,
    order_id: str,
    amount_rupees: int,
    dates: Optional[str],
    venue: Optional[str],
    qr_payload: str,
    pass_logo_filename: Optional[str] = None,   # ← NEW
):
    W, H = A4

    # Badge size
    CARD_W = 100 * mm
    CARD_H = 165 * mm
    card_x = (W - CARD_W) / 2.0
    card_y = (H - CARD_H) / 2.0
    RADIUS = 10 * mm

    COL_PINK = colors.Color(0.93, 0.45, 0.62)
    COL_MUTE = colors.Color(0.82, 0.84, 0.86)
    COL_WHITE = colors.white
    COL_CYAN  = colors.Color(0.0, 0.66, 0.82)

    # Full-bleed background clipped to rounded card
    c.saveState()
    p = c.beginPath(); p.roundRect(card_x, card_y, CARD_W, CARD_H, RADIUS)
    c.clipPath(p, stroke=0, fill=0)
    img_path = _static_path(bg_filename) if bg_filename else None
    if img_path:
        _safe_img(c, img_path, card_x, card_y, CARD_W, CARD_H, keep_aspect=False)
    else:
        c.setFillColor(colors.Color(0.059, 0.082, 0.090)); c.rect(card_x, card_y, CARD_W, CARD_H, 0, 1)
    c.restoreState()
    c.setLineWidth(1); c.setStrokeColor(colors.black)
    c.roundRect(card_x, card_y, CARD_W, CARD_H, RADIUS, stroke=1, fill=0)

    # Lanyard slot
    slot_w, slot_h = 18*mm, 5*mm
    slot_x = card_x + (CARD_W - slot_w) / 2.0
    slot_y = card_y + CARD_H - 13*mm
    c.setFillColor(colors.Color(0.15, 0.17, 0.18))
    c.roundRect(slot_x, slot_y, slot_w, slot_h, 2*mm, stroke=0, fill=1)

    # ---- Top stack: LOGO -> Name -> Subtitle ----
    cx = card_x + CARD_W/2.0
    top_anchor = card_y + CARD_H - 30*mm  # below the slot

    # Logo centered at top (rectangle, aspect-fit)
    if pass_logo_filename:
        logo_path = _static_path(pass_logo_filename)
    else:
        logo_path = None
    # max area for the logo
    LOGO_W, LOGO_H = 36*mm, 14*mm
    if logo_path:
        # draw centered so the logo bottom baseline sits above the name
        _safe_img(c, logo_path, cx - LOGO_W/2.0, top_anchor - LOGO_H/2.0, LOGO_W, LOGO_H, keep_aspect=True)

    # Name — give more room under the logo
    c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 20)
    name_y = top_anchor - (logo_path and 16*mm or 10*mm)  # bigger gap when logo exists
    c.drawCentredString(cx, name_y, attendee)

    # Subtitle (event – pass)
    c.setFillColor(COL_PINK); c.setFont("Helvetica-Bold", 10.5)
    c.drawCentredString(cx, name_y - 9*mm, f"{event_title} – {pass_label}".upper())

    # ---- Content area (two columns, a bit lower) ----
    left_x  = card_x + 12*mm
    right_x = card_x + CARD_W - 12*mm
    content_top = name_y - 18*mm    # start the info block comfortably below subtitle

    # DATE
    c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
    c.drawString(left_x, content_top, "DATE")
    c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 12)
    c.drawString(left_x, content_top - 6.5*mm, (dates or "TBA"))

    # VENUE (wrap to max 3 lines)
    c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
    c.drawString(left_x, content_top - 15*mm, "VENUE")
    c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 10.8)
    venue_text = (venue or "Venue TBA")
    max_w = (CARD_W * 0.58) - 12*mm
    lines = _wrap_text(c, venue_text, max_w, "Helvetica-Bold", 10.8)
    vy = content_top - 21*mm
    for line in lines[:3]:
        c.drawString(left_x, vy, line)
        vy -= 6.2*mm

    # SEAT
    c.setFillColor(COL_MUTE); c.setFont("Helvetica-Bold", 8)
    c.drawString(left_x, vy - 3.5*mm, "SEAT")
    c.setFillColor(COL_WHITE); c.setFont("Helvetica-Bold", 10.8)
    c.drawString(left_x, vy - 10*mm, "VIP / Zone A")
    c.drawString(left_x, vy - 16*mm, "A-17")

    # ---- Smaller QR on the right ----
    tile = 32*mm
    tile_x = right_x - tile
    left_bottom = vy - 18*mm
    left_top    = content_top
    tile_y = left_bottom + (left_top - left_bottom - tile) / 2.0
    tile_y = max(tile_y, card_y + 30*mm)
    tile_y = min(tile_y, card_y + CARD_H - 30*mm - tile)

    c.setFillColor(COL_WHITE)
    c.roundRect(tile_x - 3*mm, tile_y - 3*mm, tile + 6*mm, tile + 6*mm, 4*mm, stroke=0, fill=1)
    _draw_qr(c, qr_payload, tile_x, tile_y, size=tile)

    # ---- Bottom divider + meta ----
    bottom_safe = 22*mm
    c.setStrokeColor(COL_MUTE); c.setDash(2, 3)
    c.line(card_x + 10*mm, card_y + bottom_safe, card_x + CARD_W - 10*mm, card_y + bottom_safe)
    c.setDash(1, 0)

    c.setFillColor(COL_MUTE); c.setFont("Helvetica", 9)
    c.drawString(card_x + 12*mm, card_y + bottom_safe - 8*mm, f"ORDER ID  {order_id}")

    c.setStrokeColor(COL_CYAN); c.setLineWidth(1.2)
    gx = card_x + CARD_W - 12*mm; gy = card_y + bottom_safe - 8*mm
    c.circle(gx, gy, 4*mm, stroke=1, fill=0)
    c.line(gx-2.5*mm, gy, gx+1.2*mm, gy)
    c.line(gx-1.5*mm, gy+2*mm, gx+2.2*mm, gy+2*mm)
    c.line(gx-1.5*mm, gy-2*mm, gx+2.2*mm, gy-2*mm)


# ---------------------------------------------------------------------
# MAIN: build from Order
# ---------------------------------------------------------------------
def build_invoice_and_pass_pdf_from_order(
    order: Order,
    *,
    verify_url_base: Optional[str] = None,   # e.g., "https://h2h.app/verify"
    logo_filename: str = "Logo.png",         # pass "Logo.png" (exact case)
    pass_bg_filename: Optional[str] = "back.png",  # pass "back.png"
    travel_dates: Optional[str] = None,      # e.g., "16 Nov 2025"
    venue: Optional[str] = "Highway to Heal"
) -> bytes:
    """
    Returns bytes of a PDF:
      - Page 1: Invoice
      - Page 2: Entry Pass (portrait badge)
    """
    user = order.user
    profile = getattr(user, "profile", None)

    billed_name = (profile.full_name or user.get_full_name() or user.username) if profile else (user.get_full_name() or user.username)
    billed_email = user.email or (profile.full_name if profile else "")
    phone = profile.phone_number if profile else ""

    # Convert paise → rupees
    total_rupees = int(round((order.amount or 0) / 100.0))

    pkg = order.package
    order_id = order.razorpay_order_id
    pay_id = order.razorpay_payment_id or ""

    # QR payloads
    verify_target = f"{verify_url_base.rstrip('/')}/{order_id}" if verify_url_base else None
    invoice_qr = verify_target or f'{{"type":"invoice","order_id":"{order_id}","paid":{str(order.paid).lower()},"amount":{total_rupees}}}'
    pass_qr    = verify_target or f'{{"type":"pass","order_id":"{order_id}","name":"{billed_name}","pkg":"{pkg.name}"}}'

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    # Page 1: Invoice
    _draw_invoice_page(
        c,
        logo_filename=logo_filename,
        invoice_title="Invoice",
        order_id=order_id,
        booking_date=order.created_at or datetime.now(),
        billed_to_name=billed_name,
        billed_to_email=billed_email,
        contact_phone=phone,
        items=[{"label": pkg.name, "amount": total_rupees}],
        taxes_fees=[],
        grand_total_rupees=total_rupees,
        meta_right={
            "Order ID": order_id,
            "Payment ID": pay_id if pay_id else "—",
            "Status": "PAID" if order.paid else "UNPAID",
        },
        qr_payload=invoice_qr,
    )
    c.showPage()

    # Page 2: Event Pass (badge)
    _draw_pass_page(
        c,
        bg_filename=pass_bg_filename,
        event_title="HIGHWAY TO HEAL",
        pass_label=pkg.name,
        attendee=billed_name,
        order_id=order_id,
        amount_rupees=total_rupees,
        dates=travel_dates,
        venue=venue,
        qr_payload=pass_qr,
        pass_logo_filename=logo_filename,
    )
    c.showPage()

    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------
# Backward-compatible simple single-page ticket (if other code calls it)
# ---------------------------------------------------------------------
def build_ticket_pdf(*, order_id: str, user_name: str, package_name: str, amount_inr: int) -> bytes:
    """Original minimal pass (kept for compatibility)."""
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    W, H = A4

    p.setFont("Helvetica-Bold", 20)
    p.drawString(30*mm, H - 40*mm, "Highway to Heal — Travel Pass")

    p.setFont("Helvetica", 12)
    y = H - 60*mm
    p.drawString(30*mm, y, f"Order: {order_id}"); y -= 10*mm
    p.drawString(30*mm, y, f"Name: {user_name}"); y -= 10*mm
    p.drawString(30*mm, y, f"Package: {package_name}"); y -= 10*mm
    p.drawString(30*mm, y, f"Amount Paid: {money(amount_inr)}")

    _draw_qr(p, f'{{"type":"pass","order_id":"{order_id}"}}', 30*mm, 20*mm, size=35*mm)

    p.setFont("Helvetica-Oblique", 10)
    p.drawString(30*mm, 20*mm, "Present this PDF at the event gate with a valid ID.")

    p.showPage()
    p.save()
    return buffer.getvalue()
