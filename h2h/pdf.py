# pdf.py (REVISED)
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

from .models import Order, Booking, Allocation


# =====================================================================
# DESIGN TOKENS & LAYOUT SYSTEM
# =====================================================================

# Page + margins
PAGE_W, PAGE_H = A4
MARGIN_L = 14 * mm
MARGIN_R = 14 * mm
MARGIN_T = 14 * mm
MARGIN_B = 12 * mm

LEFT   = MARGIN_L
RIGHT  = PAGE_W - MARGIN_R
TOP    = PAGE_H - MARGIN_T
BOTTOM = MARGIN_B

# Grid & rhythm
GRID   = 4 * mm     # baseline grid
INSET  = 6 * mm     # inner padding for panels
COL_GAP = 6 * mm
SECTION_GAP = 3 * GRID

# Radii
R_PANEL = 3 * mm
R_TILE  = 4 * mm

# Color palette (Tailwind-ish neutrals)
def _hex(rgb: str) -> colors.Color:
    rgb = rgb.lstrip("#")
    r, g, b = tuple(int(rgb[i:i+2], 16) / 255 for i in (0, 2, 4))
    return colors.Color(r, g, b)

BG_HEADER     = _hex("#F8FAFC")
PANEL_BORDER  = _hex("#E5E7EB")
TABLE_HEAD    = _hex("#F3F4F6")
TEXT          = _hex("#000000")
MUTE          = _hex("#4B5563")
ACCENT        = _hex("#262626")

# Type scale (pt)
T_8  = 8.5
T_9  = 9.2
T_10 = 10.5
T_12 = 12
T_14 = 14
T_16 = 16

# Fonts (registered in ensure_unicode_font)
_FONT_READY = False
_FONT_BODY = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_MONO = "Courier"


# =====================================================================
# FONT UTILITIES (₹ safe)
# =====================================================================

def _find_static(*filenames: str) -> Optional[str]:
    """Try multiple filenames via Django finders and common static dirs."""
    for name in filenames:
        if not name:
            continue
        # absolute
        if os.path.isabs(name) and os.path.exists(name):
            return name

        # finders
        p = finders.find(name)
        if p:
            return p

        # STATIC_ROOT
        sroot = getattr(settings, "STATIC_ROOT", None)
        if sroot:
            cand = os.path.join(sroot, name)
            if os.path.exists(cand):
                return cand

        # STATICFILES_DIRS
        for base in getattr(settings, "STATICFILES_DIRS", []):
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                return cand

        # relative to this file
        here = os.path.dirname(__file__)
        cand = os.path.join(here, name)
        if os.path.exists(cand):
            return cand
    return None


def ensure_unicode_font() -> bool:
    """
    Register DejaVu Sans Regular/Bold if available (supports ₹).
    Return True iff DejaVu regular is available (we'll still set fallbacks for bold).
    """
    global _FONT_READY, _FONT_BODY, _FONT_BOLD
    if _FONT_READY:
        return _FONT_BODY.startswith("DejaVu")

    reg = _find_static("DejaVuSans.ttf", "dejavu/DejaVuSans.ttf", "fonts/DejaVuSans.ttf")
    bold = _find_static("DejaVuSans-Bold.ttf", "dejavu/DejaVuSans-Bold.ttf", "fonts/DejaVuSans-Bold.ttf")

    ok = False
    try:
        if reg:
            pdfmetrics.registerFont(TTFont("DejaVuSans", reg))
            _FONT_BODY = "DejaVuSans"
            ok = True
        if bold:
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold))
            _FONT_BOLD = "DejaVuSans-Bold"
        else:
            if ok:
                _FONT_BOLD = "DejaVuSans"
    except Exception:
        ok = False

    _FONT_READY = True
    return ok


def money(value_rupees: float | int) -> str:
    """Format money with ₹ when font is available; otherwise Rs."""
    if ensure_unicode_font():
        return f"₹ {value_rupees:,.0f}"
    return f"Rs. {value_rupees:,.0f}"


# =====================================================================
# INR NUMBER → WORDS (Indian system)
# =====================================================================

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
    crore, n = divmod(n, 10_00_00_00)
    lakh, n = divmod(n, 1_00_000)
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


# =====================================================================
# ASSET HELPERS (images, qr)
# =====================================================================

def _static_path(filename: str | None) -> Optional[str]:
    return _find_static(filename) if filename else None


def _text_width(c: canvas.Canvas, text: str, font: str, size: float) -> float:
    return c.stringWidth(text or "", font, size)


def _wrap_text(c: canvas.Canvas, text: str, max_w: float, font: str, size: float) -> List[str]:
    """Simple word-wrap avoiding mid-word breaks."""
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


def _ellipsis(c: canvas.Canvas, text: str, max_w: float, font: str, size: float) -> str:
    """Truncate with ellipsis without mid-word clipping."""
    txt = (text or "").strip()
    if _text_width(c, txt, font, size) <= max_w:
        return txt
    dots = "…"
    words = txt.split()
    if not words:
        return ""
    out = ""
    for w in words:
        cand = (out + " " + w).strip()
        if _text_width(c, cand + dots, font, size) <= max_w:
            out = cand
        else:
            break
    return (out or (txt[:1])) + dots


def _sanitize_colors(node):
    """Force None fill/stroke colors in a Drawing tree to black to avoid errors."""
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
    widget = qr.QrCodeWidget(data or "")
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
    """Try drawing a logo; if not found, draw a labeled placeholder. Never crash."""
    p = _static_path(filename) if filename else None
    ok = _safe_img(c, p, x, y, w, h, keep_aspect=True)
    if ok:
        return
    c.saveState()
    c.setStrokeColor(PANEL_BORDER)
    c.roundRect(x, y, w, h, 2.5 * mm, stroke=1, fill=0)
    c.setFont(_FONT_BOLD, T_10)
    c.setFillColor(MUTE)
    c.drawCentredString(x + w/2.0, y + h/2.0 - 4, "LOGO")
    c.restoreState()


# =====================================================================
# PRIMITIVES
# =====================================================================

def draw_panel(c: canvas.Canvas, x: float, y: float, w: float, h: float,
               radius: float = R_PANEL, fill: colors.Color | None = None, stroke: colors.Color = PANEL_BORDER):
    """Rounded panel with optional fill."""
    c.saveState()
    c.setLineWidth(1)
    c.setStrokeColor(stroke)
    if fill is not None:
        c.setFillColor(fill)
        c.roundRect(x, y, w, h, radius, stroke=1, fill=1)
    else:
        c.roundRect(x, y, w, h, radius, stroke=1, fill=0)
    c.restoreState()


def draw_h_rule(c: canvas.Canvas, x1: float, y: float, x2: float):
    """1px horizontal rule with exact endpoints."""
    c.saveState()
    c.setStrokeColor(PANEL_BORDER)
    c.setLineWidth(1)
    c.line(x1, y, x2, y)
    c.restoreState()


# =====================================================================
# PAGE COMPOSERS
# =====================================================================

# def _invoice_qr_footer(c: canvas.Canvas, *, payload: str):
#     """
#     Fixed QR section at the very bottom of the page (footer).
#     - Panel spans from LEFT to RIGHT
#     - QR right-aligned to RIGHT - INSET
#     - Caption on left
#     Baseline grid note: top & bottom paddings align to GRID multiples.
#     """
#     ensure_unicode_font()
#     footer_h = 22 * mm
#     y0 = BOTTOM  # pinned to bottom margin
#     draw_panel(c, LEFT, y0, RIGHT - LEFT, footer_h, R_PANEL, fill=BG_HEADER)

#     # Left captions
#     c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, T_9)
#     c.drawString(LEFT + INSET, y0 + footer_h - INSET + 1, "Scan to verify booking")
#     c.setFillColor(MUTE); c.setFont(_FONT_BODY, T_8)
#     c.drawString(LEFT + INSET, y0 + INSET, "This QR links to your order verification.")

#     # QR tile (right-aligned, right edge = RIGHT - INSET)
#     tile = 20 * mm
#     tile_x = RIGHT - INSET - tile
#     tile_y = y0 + (footer_h - tile) / 2.0
#     c.setFillColor(colors.white)
#     c.roundRect(tile_x - 3*mm, tile_y - 3*mm, tile + 6*mm, tile + 6*mm, R_TILE, stroke=0, fill=1)
#     _draw_qr(c, payload, tile_x, tile_y, size=tile)
#     c.setFillColor(MUTE); c.setFont(_FONT_BODY, T_8)
#     c.drawRightString(RIGHT - INSET, y0 + 2.5*mm, "Verified by QR")


def _invoice_qr_footer(c: canvas.Canvas, *, payload: str):
    """
    Footer: QR only (top-right), plus support info at the right side.
    No background panel, no captions.
    """
    ensure_unicode_font()
    footer_h = 22 * mm
    y0 = BOTTOM  # reserved footer band

    # QR tile pinned to the right
    tile = 20 * mm
    tile_x = RIGHT - INSET - tile
    tile_y = y0 + (footer_h - tile) / 2.0
    _draw_qr(c, payload, tile_x, tile_y, size=tile)

    # Support info — right-aligned, to the left of the QR
    c.setFillColor(MUTE); c.setFont(_FONT_BODY, T_9)
    text_right = tile_x - 4 * mm         # small gap from QR
    line1_y    = y0 + footer_h/2 + 3 * mm
    line2_y    = line1_y - 5 * mm
    c.drawRightString(text_right, line1_y, "support@highwaytoheal.com")
    c.drawRightString(text_right, line2_y, "+91 9836007110")


def _invoice_page_header_and_meta(
    c: canvas.Canvas,
    *,
    logo_filename: Optional[str],
    invoice_title: str,
    order_id: str,
    booking_date: datetime,
    meta_right: Dict[str, str],
    billed_to_name: str,
    billed_to_email: str | None,
    contact_phone: str | None,
) -> float:
    """Top band + meta panel. Returns y below this block."""
    ensure_unicode_font()
    band_h = 24 * mm
    c.setFillColor(BG_HEADER)
    c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)

    _logo_or_placeholder(c, logo_filename, LEFT, PAGE_H - band_h + 4*mm, 40*mm, 16*mm)

    # Meta box right
    box_w, box_h = 70*mm, 22*mm
    box_x, box_y = RIGHT - box_w, PAGE_H - band_h + 2*mm
    draw_panel(c, box_x, box_y, box_w, box_h, R_PANEL, fill=None)

    c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, T_14)
    c.drawRightString(box_x + box_w - INSET, box_y + box_h - 7*mm, invoice_title.upper())

    c.setFillColor(TEXT); c.setFont(_FONT_BODY, T_9)
    c.drawRightString(box_x + box_w - INSET, box_y + INSET + 5*mm, f"Invoice Date: {booking_date:%d %b %Y}")
    c.drawRightString(box_x + box_w - INSET, box_y + INSET,               f"Order ID: {order_id}")


    y = PAGE_H - band_h - 2*GRID

    # Two-column panel (Billed To / Booking Details)
    panel_h = 34 * mm
    draw_panel(c, LEFT, y - panel_h, RIGHT - LEFT, panel_h, R_PANEL)
    mid_x = LEFT + (RIGHT - LEFT) * 0.55
    c.setStrokeColor(PANEL_BORDER); c.line(mid_x, y - panel_h, mid_x, y)

    # Left: Billed To
    c.setFont(_FONT_BOLD, T_10); c.setFillColor(ACCENT)
    c.drawString(LEFT + INSET, y - INSET, "Billed To")
    c.setFont(_FONT_BODY, T_9); c.setFillColor(TEXT)
    c.drawString(LEFT + INSET, y - INSET - GRID, billed_to_name or "")
    c.setFillColor(MUTE)
    if billed_to_email:
        c.drawString(LEFT + INSET, y - INSET - 2*GRID,
                     _ellipsis(c, billed_to_email, (mid_x - (LEFT + INSET) - 2*mm), _FONT_BODY, T_9))
    if contact_phone:
        c.drawString(LEFT + INSET, y - INSET - 3*GRID, f"Phone: {contact_phone}")

    # Right: Booking details short stack
    c.setFont(_FONT_BOLD, T_10); c.setFillColor(ACCENT)
    c.drawRightString(RIGHT - INSET, y - INSET, "Booking Details")
    c.setFont(_FONT_BODY, T_9); c.setFillColor(MUTE)
    ry = y - INSET - GRID
    for k, v in meta_right.items():
        val = f"{k}: {v}"
        c.drawRightString(RIGHT - INSET, ry,
                          _ellipsis(c, val, (RIGHT - INSET) - (mid_x + 2*mm), _FONT_BODY, T_9))
        ry -= GRID

    return y - panel_h - GRID


def _invoice_items_table(
    c: canvas.Canvas,
    *,
    y: float,
    items: List[Dict[str, float]],
    taxes_fees: List[Dict[str, float]],
    grand_total_rupees: int,
) -> float:
    """Items + taxes + grand total panel. Returns y below the block."""
    ensure_unicode_font()

    head_h = 10 * mm
    row_h  = 7.5 * mm
    rows_count = len(items) + len(taxes_fees)
    box_h = head_h + rows_count * row_h + (18 * mm)  # includes totals band

    draw_panel(c, LEFT, y - box_h, RIGHT - LEFT, box_h, R_PANEL)

    # Header
    c.setFillColor(TABLE_HEAD)
    c.rect(LEFT + 0.5*mm, y - head_h, (RIGHT - LEFT) - 1*mm, head_h, stroke=0, fill=1)
    c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, T_10)
    c.drawString(LEFT + INSET, y - head_h + (head_h/2 - 2), "Description")
    c.drawRightString(RIGHT - INSET, y - head_h + (head_h/2 - 2), "Amount")

    # Rows
    c.setFillColor(TEXT); c.setFont(_FONT_BODY, T_9)
    yy = y - head_h - (row_h/2 + 2)
    for row in items:
        c.drawString(LEFT + INSET, yy, str(row.get("label", "")))
        c.drawRightString(RIGHT - INSET, yy, money(row.get("amount", 0)))
        yy -= row_h
    for row in taxes_fees:
        c.setFillColor(MUTE)
        c.drawString(LEFT + INSET, yy, str(row.get("label", "")))
        c.drawRightString(RIGHT - INSET, yy, money(row.get("amount", 0)))
        c.setFillColor(TEXT)
        yy -= row_h

    # Totals
    # draw_h_rule(c, LEFT + INSET, yy - 2*mm, RIGHT - INSET)
    # yy -= (GRID + 1.5*mm)
    # c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, 11.5)
    # c.drawString(LEFT + INSET, yy, "GRAND TOTAL")
    # c.drawRightString(RIGHT - INSET, yy, money(grand_total_rupees))
    
    draw_h_rule(c, LEFT + INSET, yy - 2*mm, RIGHT - INSET)
    yy -= 2*GRID   # was (GRID + 1.5*mm); 2*GRID = 8 mm if GRID = 4 mm
    c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, 11.5)
    c.drawString(LEFT + INSET, yy, "GRAND TOTAL")
    c.drawRightString(RIGHT - INSET, yy, money(grand_total_rupees))

    # yy -= GRID
    # words = inr_to_words(int(grand_total_rupees)).upper()
    # c.setFillColor(BG_HEADER)
    # c.rect(LEFT + 0.5*mm, yy - 9*mm, (RIGHT - LEFT) - 1*mm, 9*mm, stroke=0, fill=1)
    # c.setFillColor(MUTE); c.setFont(_FONT_BODY, 8.6)
    # c.drawString(LEFT + INSET, yy - 6.5*mm, f"Grand Total (in words): {words} ONLY")

    return y - box_h - SECTION_GAP


def _booking_details_and_guests_fit_single_page(
    c: canvas.Canvas,
    *,
    start_y: float,
    bottom_limit: float,    # BOTTOM + FOOTER_H + breathing
    property_name: str,
    unit_type_name: str,
    category_name: str,
    unit_labels: str,
    event_dates: Tuple[str, str],
    guests_total: int,
    gender_mix: str,
    meal_mix: str,
    guest_rows: List[Dict[str, str]],
) -> float:
    """
    Draw BOOKING DETAILS & GUEST LIST BELOW invoice on the SAME PAGE.
    - Ensures nothing crosses into reserved footer area (bottom_limit)
    - Compresses guest rows if needed
    - Truncates and adds “… and N more” inside the last visible row if still overflowing
    Returns y below the section.
    """
    ensure_unicode_font()

    y = start_y
    title_h = GRID + 2*mm
    info_h  = 28 * mm

    # If we have very little space, shrink info panel a bit to 24mm (min)
    min_info_h = 24 * mm
    if y - (title_h + info_h + GRID) < bottom_limit:
        info_h = max(min_info_h, (y - bottom_limit) - (title_h + GRID))
        info_h = max(info_h, 18 * mm)  # hard floor to keep legibility

    # Title
    c.setFont(_FONT_BOLD, 12.5); c.setFillColor(ACCENT)
    c.drawString(LEFT, y - 2, "BOOKING DETAILS & GUEST LIST")
    y -= title_h

    # Two-column block (exact 50/50 split)
    draw_panel(c, LEFT, y - info_h, RIGHT - LEFT, info_h, R_PANEL)
    col_w = (RIGHT - LEFT) / 2.0
    c.setStrokeColor(PANEL_BORDER)
    c.line(LEFT + col_w, y - info_h, LEFT + col_w, y)

    # Left: Allocation
    c.setFont(_FONT_BOLD, T_10); c.setFillColor(ACCENT)
    c.drawString(LEFT + INSET, y - INSET, "Allocation")
    c.setFont(_FONT_BODY, T_9); c.setFillColor(TEXT)
    ly = y - INSET - GRID
    c.drawString(LEFT + INSET, ly, f"Property: {_ellipsis(c, property_name, col_w - 2*INSET, _FONT_BODY, T_9)}"); ly -= GRID
    c.drawString(LEFT + INSET, ly, f"Unit Type: {_ellipsis(c, unit_type_name, col_w - 2*INSET, _FONT_BODY, T_9)}"); ly -= GRID
    c.drawString(LEFT + INSET, ly, f"Category: {_ellipsis(c, category_name, col_w - 2*INSET, _FONT_BODY, T_9)}"); ly -= GRID
    c.drawString(LEFT + INSET, ly, f"Unit Labels: {_ellipsis(c, unit_labels, col_w - 2*INSET, _FONT_BODY, T_9)}")

    # Right: Event & Stats
    c.setFont(_FONT_BOLD, T_10); c.setFillColor(ACCENT)
    c.drawRightString(LEFT + col_w*2 - INSET, y - INSET, "Event")
    c.setFont(_FONT_BODY, T_9); c.setFillColor(MUTE)
    ry = y - INSET - GRID
    check_in, check_out = event_dates
    right_w = col_w - 2*INSET
    for line in [
        f"Check-in: {check_in}",
        f"Check-out: {check_out}",
        f"Guests: {guests_total}",
        f"Gender Mix: {gender_mix}",
        f"Meals: {meal_mix}",
    ]:
        c.drawRightString(RIGHT - INSET, ry, _ellipsis(c, line, right_w, _FONT_BODY, T_9))
        ry -= GRID

    y -= (info_h + GRID)

    # Guest table that FITS remaining space
    header_h = 10 * mm
    row_h    = 7.5 * mm
    min_row_h = 6.0 * mm  # compression limit
    EXTRA_PANEL_H = 8 * mm 

    # Column widths must sum exactly to table width
    table_x = LEFT
    table_w = RIGHT - LEFT
    w_name = 58 * mm
    w_gender = 18 * mm
    w_age = 16 * mm
    w_meal = 22 * mm
    w_blood = 22 * mm
    w_role = table_w - (w_name + w_gender + w_age + w_meal + w_blood)
    
    avail_h = max(0, (y - bottom_limit) - EXTRA_PANEL_H)
    if avail_h <= header_h + min_row_h:
        row_h = min_row_h
        max_rows = 1
    else:
        max_rows = int((avail_h - header_h) // row_h)
        if max_rows < 1:
            row_h = min_row_h
            max_rows = max(1, int((avail_h - header_h) // row_h))


    rows_data = guest_rows or []
    trunc_note = None
    truncated = False
    if len(rows_data) > max_rows:
        truncated = True
        overflow = len(rows_data) - (max_rows - 1) if max_rows > 1 else len(rows_data) - 1
        overflow = max(overflow, 0)
        # Prepare a note row for the last visible line
        trunc_note = f"… and {overflow} more guest{'s' if overflow != 1 else ''} on file"
        visible = rows_data[:max_rows - 1] if max_rows > 1 else rows_data[:1]
        rows_to_draw = list(visible)
        # Append a synthetic row for the note
        rows_to_draw.append({"name": trunc_note, "gender": "—", "age": "—", "meal": "—", "blood": "—", "role": "—"})
    else:
        rows_to_draw = rows_data

    # Table visual container
    total_table_h = header_h + row_h * max(1, len(rows_to_draw)) + EXTRA_PANEL_H
    draw_panel(c, table_x, y - total_table_h, table_w, total_table_h, R_PANEL)
    # total_table_h = header_h + row_h * max(1, len(rows_to_draw))
    # draw_panel(c, table_x, y - total_table_h, table_w, total_table_h, R_PANEL)

    # Header band
    c.setFillColor(TABLE_HEAD)
    c.rect(table_x + 0.5*mm, y - header_h, table_w - 1*mm, header_h, stroke=0, fill=1)
    c.setFillColor(ACCENT); c.setFont(_FONT_BOLD, T_9)
    hx = table_x
    pad = 2.5 * mm
    def _h(txt, wcol, align="L"):
        nonlocal hx
        if align == "R":
            c.drawRightString(hx + wcol - pad, y - header_h/2 - 2, txt)
        elif align == "C":
            c.drawCentredString(hx + wcol/2, y - header_h/2 - 2, txt)
        else:
            c.drawString(hx + pad, y - header_h/2 - 2, txt)
        hx += wcol

    _h("Name",   w_name, "L")
    _h("Gender", w_gender, "C")
    _h("Age",    w_age, "C")
    _h("Meal",   w_meal, "C")
    _h("Blood",  w_blood, "C")
    _h("Role",   w_role, "L")

    # Rows
    c.setFillColor(TEXT); c.setFont(_FONT_BODY, T_9)
    yy = y - header_h
    for idx, r in enumerate(rows_to_draw):
        yy -= row_h
        x = table_x
        # Name
        name_txt = str(r.get("name", "—"))
        if truncated and trunc_note and idx == len(rows_to_draw) - 1:
            c.setFillColor(MUTE); c.setFont(_FONT_BODY, T_9)
        c.drawString(x + pad, yy - row_h/2 + 2, _ellipsis(c, name_txt, w_name - 2*pad, _FONT_BODY, T_9)); x += w_name
        # Gender
        c.setFillColor(TEXT); c.setFont(_FONT_BODY, T_9)
        c.drawCentredString(x + w_gender/2, yy - row_h/2 + 2, _ellipsis(c, str(r.get("gender","—")), w_gender - 2*pad, _FONT_BODY, T_9)); x += w_gender
        # Age
        c.drawCentredString(x + w_age/2, yy - row_h/2 + 2, _ellipsis(c, str(r.get("age","—")), w_age - 2*pad, _FONT_BODY, T_9)); x += w_age
        # Meal
        c.drawCentredString(x + w_meal/2, yy - row_h/2 + 2, _ellipsis(c, str(r.get("meal","—")), w_meal - 2*pad, _FONT_BODY, T_9)); x += w_meal
        # Blood
        c.drawCentredString(x + w_blood/2, yy - row_h/2 + 2, _ellipsis(c, str(r.get("blood","—")), w_blood - 2*pad, _FONT_BODY, T_9)); x += w_blood
        # Role
        c.drawString(x + pad, yy - row_h/2 + 2, _ellipsis(c, str(r.get("role","—")), w_role - 2*pad, _FONT_BODY, T_9))

    return (y - total_table_h - GRID)


def _pass_page(
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
    pass_logo_filename: Optional[str] = None,
):
    """Entry Pass page (separate page)."""
    ensure_unicode_font()
    # Card geometry (portrait badge)
    CARD_W = 100 * mm
    CARD_H = 165 * mm
    card_x = (PAGE_W - CARD_W) / 2.0
    card_y = (PAGE_H - CARD_H) / 2.0

    # Background (clipped to rounded rect)
    c.saveState()
    p = c.beginPath()
    p.roundRect(card_x, card_y, CARD_W, CARD_H, 10 * mm)
    c.clipPath(p, stroke=0, fill=0)
    img_path = _static_path(bg_filename) if bg_filename else None
    if img_path:
        _safe_img(c, img_path, card_x, card_y, CARD_W, CARD_H, keep_aspect=False)
    else:
        c.setFillColor(colors.Color(0.06, 0.08, 0.10))
        c.rect(card_x, card_y, CARD_W, CARD_H, stroke=0, fill=1)
    c.restoreState()

    c.setLineWidth(1); c.setStrokeColor(colors.black)
    c.roundRect(card_x, card_y, CARD_W, CARD_H, 10 * mm, stroke=1, fill=0)

    # Lanyard slot
    slot_w, slot_h = 18 * mm, 5 * mm
    slot_x = card_x + (CARD_W - slot_w) / 2.0
    slot_y = card_y + CARD_H - 13 * mm
    c.setFillColor(colors.Color(0.15, 0.17, 0.18))
    c.roundRect(slot_x, slot_y, slot_w, slot_h, 2 * mm, stroke=0, fill=1)

    # Top stack
    cx = card_x + CARD_W / 2.0
    top_anchor = card_y + CARD_H - 30 * mm

    logo_path = _static_path(pass_logo_filename) if pass_logo_filename else None
    LOGO_W, LOGO_H = 36 * mm, 14 * mm
    if logo_path:
        _safe_img(c, logo_path, cx - LOGO_W / 2.0, top_anchor - LOGO_H / 2.0, LOGO_W, LOGO_H, keep_aspect=True)

    c.setFillColor(colors.white); c.setFont(_FONT_BOLD, 20)
    name_y = top_anchor - (16 * mm if logo_path else 10 * mm)
    c.drawCentredString(cx, name_y, _ellipsis(c, attendee, CARD_W - 24*mm, _FONT_BOLD, 20))

    c.setFillColor(_hex("#ED2F79")); c.setFont(_FONT_BOLD, 10.5)
    c.drawCentredString(cx, name_y - 9 * mm, f"{event_title} – {pass_label}".upper())

    # Content area
    left_x = card_x + 12 * mm
    right_x = card_x + CARD_W - 12 * mm
    content_top = name_y - 18 * mm

    c.setFillColor(_hex("#C6CDD5")); c.setFont(_FONT_BOLD, 8)
    c.drawString(left_x, content_top, "DATE")
    c.setFillColor(colors.white); c.setFont(_FONT_BOLD, 12)
    c.drawString(left_x, content_top - 6.5 * mm, (dates or "TBA"))

    c.setFillColor(_hex("#C6CDD5")); c.setFont(_FONT_BOLD, 8)
    c.drawString(left_x, content_top - 15 * mm, "VENUE")
    c.setFillColor(colors.white); c.setFont(_FONT_BOLD, 10.8)
    venue_text = (venue or "Venue TBA")
    max_w = (CARD_W * 0.58) - 12 * mm
    # wrap 2–3 lines
    lines = _wrap_text(c, venue_text, max_w, _FONT_BOLD, 10.8)
    vy = content_top - 21 * mm
    for line in lines[:3]:
        c.drawString(left_x, vy, line)
        vy -= 6.2 * mm

    c.setFillColor(_hex("#C6CDD5")); c.setFont(_FONT_BOLD, 8)
    c.drawString(left_x, vy - 3.5 * mm, "PAYMENT")
    c.setFillColor(colors.white); c.setFont(_FONT_BOLD, 10.8)
    c.drawString(left_x, vy - 10 * mm, "Amount")
    c.drawString(left_x + 20 * mm, vy - 10 * mm, money(amount_rupees))

    # QR tile
    tile = 32 * mm
    tile_x = right_x - tile
    left_bottom = vy - 18 * mm
    tile_y = max(left_bottom, card_y + 30 * mm)
    tile_y = min(tile_y, card_y + CARD_H - 30 * mm - tile)

    c.setFillColor(colors.white)
    c.roundRect(tile_x - 3 * mm, tile_y - 3 * mm, tile + 6 * mm, tile + 6 * mm, R_TILE, stroke=0, fill=1)
    _draw_qr(c, qr_payload, tile_x, tile_y, size=tile)

    # Footer stripe
    bottom_safe = 22 * mm
    c.setStrokeColor(_hex("#9BD7EA")); c.setDash(2, 3)
    c.line(card_x + 10 * mm, card_y + bottom_safe, card_x + CARD_W - 10 * mm, card_y + bottom_safe)
    c.setDash(1, 0)

    c.setFillColor(_hex("#C6CDD5")); c.setFont(_FONT_BODY, 9)
    c.drawString(card_x + 12 * mm, card_y + bottom_safe - 8 * mm, f"ORDER ID  {order_id}")


# =====================================================================
# PUBLIC API 1: Build full PDF (Invoice + Booking Details + Pass)
# =====================================================================

## version 1 of combined PDF builder (2 pages)

# def build_invoice_and_pass_pdf_from_order(
#     order: Order,
#     *,
#     verify_url_base: Optional[str] = None,
#     logo_filename: str = "Logo.png",
#     pass_bg_filename: Optional[str] = "back.png",
#     travel_dates: Optional[str] = None,
#     venue: Optional[str] = "Highway to Heal",
# ) -> bytes:
#     """
#     Page 1: Invoice + Booking Details & Guest List (SAME PAGE) with QR footer.
#     Page 2: Entry Pass.
#     """
#     ensure_unicode_font()

#     # --- Billing info
#     user = order.user
#     profile = getattr(user, "profile", None)
#     billed_name = (getattr(profile, "full_name", None) or user.get_full_name() or getattr(user, "username", "")).strip() or "Guest"
#     billed_email = (user.email or "") or (getattr(profile, "email", "") or "")
#     phone = getattr(profile, "phone_number", "") or ""

#     # --- Money & package
#     total_rupees = int(round((getattr(order, "amount", 0) or 0) / 100.0))
#     pkg = getattr(order, "package", None)
#     pkg_name = getattr(pkg, "name", "Package")

#     # --- IDs & verification
#     order_id = getattr(order, "razorpay_order_id", None) or str(getattr(order, "id", "NA"))
#     pay_id = getattr(order, "razorpay_payment_id", "") or ""
#     verify_target = f"{verify_url_base.rstrip('/')}/{order_id}" if verify_url_base else None
#     invoice_qr = verify_target or f'{{"type":"invoice","order_id":"{order_id}","paid":{str(getattr(order,"paid", False)).lower()},"amount":{total_rupees}}}'
#     pass_qr    = verify_target or f'{{"type":"pass","order_id":"{order_id}","name":"{billed_name}","pkg":"{pkg_name}"}}'

#     # --- Booking & allocations
#     booking = getattr(order, "booking", None) or Booking.objects.filter(order=order).first()

#     property_name = getattr(getattr(booking, "property", None), "name", "—")
#     unit_type_name = getattr(getattr(booking, "unit_type", None), "name", "—")
#     category_name = (getattr(booking, "category", None) or "—")
#     allocs = Allocation.objects.filter(booking=booking).select_related("unit") if booking else []
#     unit_labels = ", ".join((getattr(a.unit, "label", None) or f"Unit#{a.unit_id}") for a in allocs) or "—"

#     check_in = getattr(booking, "check_in", None)
#     check_out = getattr(booking, "check_out", None)
#     check_in_txt = check_in.strftime("%d %b %Y") if check_in else "—"
#     check_out_txt = check_out.strftime("%d %b %Y") if check_out else "—"
#     guests_total = int(getattr(booking, "guests", 1) or 1)

#     # Gender & meal mix + guest rows
#     comps = list(getattr(booking, "companions", []) or [])
#     primary_gender = (getattr(booking, "primary_gender", "O") or "O").upper()
#     primary_meal   = (getattr(booking, "primary_meal_preference", None) or getattr(booking, "primary_meal", None) or "—").upper()
#     primary_blood  = getattr(booking, "blood_group", "") or "—"
#     primary_age = getattr(booking, "primary_age", None) or "—"

#     count_m = 1 if primary_gender == "M" else 0
#     count_f = 1 if primary_gender == "F" else 0
#     count_o = 1 if primary_gender not in ("M", "F") else 0
#     count_veg = 1 if primary_meal == "VEG" else 0
#     count_nonveg = 1 if primary_meal in ("NONVEG", "NON-VEG", "NON VEG") else 0
#     count_vegan = 1 if primary_meal == "VEGAN" else 0

#     guest_rows = [{
#         "name": billed_name,
#         "gender": primary_gender,
#         "age": (str(primary_age).strip() if primary_age not in (None, "") else "—"),
#         "meal": (primary_meal if primary_meal != "—" else "—"),
#         "blood": primary_blood,
#         "role": "Primary",
#     }]

#     for cobj in comps:
#         g = (cobj.get("gender") or "O").upper()
#         a = str(cobj.get("age") or "—")
#         meal = (cobj.get("meal_preference") or cobj.get("meal") or "—").upper()
#         blood = cobj.get("blood_group") or "—"
#         name = cobj.get("name") or "—"
#         if g == "M": count_m += 1
#         elif g == "F": count_f += 1
#         else: count_o += 1
#         if meal == "VEG": count_veg += 1
#         elif meal in ("NONVEG", "NON-VEG", "NON VEG"): count_nonveg += 1
#         elif meal == "VEGAN": count_vegan += 1
#         guest_rows.append({
#             "name": name, "gender": g, "age": a, "meal": meal, "blood": blood, "role": "Companion",
#         })

#     gender_mix = f"M:{count_m} • F:{count_f} • O:{count_o}"
#     meal_bits = []
#     if count_veg: meal_bits.append(f"VEG: {count_veg}")
#     if count_nonveg: meal_bits.append(f"NON-VEG: {count_nonveg}")
#     if count_vegan: meal_bits.append(f"VEGAN: {count_vegan}")
#     meal_mix = " • ".join(meal_bits) if meal_bits else "—"

#     # Promo/discount (prefer booking snapshot)
#     promo_discount = 0
#     if booking and getattr(booking, "promo_discount_inr", None):
#         try:
#             promo_discount = int(booking.promo_discount_inr or 0)
#         except Exception:
#             promo_discount = 0

#     taxes_fees: List[Dict[str, float]] = []
#     if promo_discount:
#         taxes_fees.append({"label": "Promotion / Discount", "amount": -abs(promo_discount)})

#     # === Canvas
#     buf = BytesIO()
#     c = canvas.Canvas(buf, pagesize=A4)

#     # ==== PAGE 1 (Single page for invoice + booking details + footer QR)
#     # Header + meta + billed-to panel
#     y_after_header = _invoice_page_header_and_meta(
#         c,
#         logo_filename=logo_filename,
#         invoice_title="Invoice",
#         order_id=order_id,
#         booking_date=getattr(order, "created_at", None) or datetime.now(),
#         meta_right={
#             # "Order ID": order_id,
#             # "Payment ID": (getattr(order, "razorpay_payment_id", "") or "—"),
#             "Status": "PAID" if getattr(order, "paid", False) else "UNPAID",
#             "Event": getattr(getattr(booking, "event", None), "name", "—"),
#             "Check-in": check_in_txt,
#             "Check-out": check_out_txt,
#             "Guests": str(guests_total),
#             "Category": category_name or "—",
#         },
#         billed_to_name=billed_name,
#         billed_to_email=billed_email,
#         contact_phone=phone,
#     )

#     # Items block
#     y_after_items = _invoice_items_table(
#         c,
#         y=y_after_header,
#         items=[{"label": pkg_name, "amount": total_rupees}],
#         taxes_fees=taxes_fees,
#         grand_total_rupees=total_rupees,
#     )

#     # Reserve footer QR: bottom_limit ensures nothing crosses into footer
#     FOOTER_H = 22 * mm
#     bottom_limit = BOTTOM + FOOTER_H + GRID

#     # Booking details + guest list, fit in remaining space
#     _booking_details_and_guests_fit_single_page(
#         c,
#         start_y=y_after_items,
#         bottom_limit=bottom_limit,
#         property_name=property_name,
#         unit_type_name=unit_type_name,
#         category_name=category_name,
#         unit_labels=unit_labels,
#         event_dates=(check_in_txt, check_out_txt),
#         guests_total=guests_total,
#         gender_mix=gender_mix,
#         meal_mix=meal_mix,
#         guest_rows=guest_rows,
#     )

#     # Footer QR (always at page bottom)
#     _invoice_qr_footer(c, payload=invoice_qr)

#     # ==== PAGE 2: Entry Pass
#     c.showPage()
#     _pass_page(
#         c,
#         bg_filename=pass_bg_filename,
#         event_title=(getattr(getattr(booking, "event", None), "name", "Highway to Heal") or "Highway to Heal").upper(),
#         pass_label=pkg_name,
#         attendee=billed_name,
#         order_id=order_id,
#         amount_rupees=total_rupees,
#         dates=travel_dates or check_in_txt,
#         venue=venue or property_name,
#         qr_payload=pass_qr,
#         pass_logo_filename=logo_filename,
#     )
#     c.showPage()

#     c.save()
#     return buf.getvalue()


## version 2 of combined PDF builder (2 pages) – simplified, more pricing breakdown

def build_invoice_and_pass_pdf_from_order(
    order: Order,
    *,
    verify_url_base: Optional[str] = None,
    logo_filename: str = "Logo.png",
    pass_bg_filename: Optional[str] = "back.png",
    travel_dates: Optional[str] = None,
    venue: Optional[str] = "Highway to Heal",
) -> bytes:
    """
    Page 1: Invoice + Booking Details & Guest List (SAME PAGE) with QR footer.
    Page 2: Entry Pass.
    """
    ensure_unicode_font()

    # --- Billing info
    user = order.user
    profile = getattr(user, "profile", None)
    billed_name = (getattr(profile, "full_name", None) or user.get_full_name() or getattr(user, "username", "")).strip() or "Guest"
    billed_email = (user.email or "") or (getattr(profile, "email", "") or "")
    phone = getattr(profile, "phone_number", "") or ""

    # --- Money & package
    # Use ORDER amount for the grand total (this is gross, incl. any convenience)
    grand_total_rupees = int(round((getattr(order, "amount", 0) or 0) / 100.0))
    pkg = getattr(order, "package", None)
    pkg_name = getattr(pkg, "name", "Package")

    # --- IDs & verification
    order_id = getattr(order, "razorpay_order_id", None) or str(getattr(order, "id", "NA"))
    pay_id = getattr(order, "razorpay_payment_id", "") or ""
    verify_target = f"{verify_url_base.rstrip('/')}/{order_id}" if verify_url_base else None
    invoice_qr = verify_target or f'{{"type":"invoice","order_id":"{order_id}","paid":{str(getattr(order,"paid", False)).lower()},"amount":{grand_total_rupees}}}'
    pass_qr    = verify_target or f'{{"type":"pass","order_id":"{order_id}","name":"{billed_name}","pkg":"{pkg_name}"}}'

    # --- Booking & allocations
    booking = getattr(order, "booking", None) or Booking.objects.filter(order=order).first()

    property_name = getattr(getattr(booking, "property", None), "name", "—")
    unit_type_name = getattr(getattr(booking, "unit_type", None), "name", "—")
    category_name = (getattr(booking, "category", None) or "—")
    allocs = Allocation.objects.filter(booking=booking).select_related("unit") if booking else []
    unit_labels = ", ".join((getattr(a.unit, "label", None) or f"Unit#{a.unit_id}") for a in allocs) or "—"

    check_in = getattr(booking, "check_in", None)
    check_out = getattr(booking, "check_out", None)
    check_in_txt = check_in.strftime("%d %b %Y") if check_in else "—"
    check_out_txt = check_out.strftime("%d %b %Y") if check_out else "—"
    guests_total = int(getattr(booking, "guests", 1) or 1)

    # Gender & meal mix + guest rows
    comps = list(getattr(booking, "companions", []) or [])
    primary_gender = (getattr(booking, "primary_gender", "O") or "O").upper()
    primary_meal   = (getattr(booking, "primary_meal_preference", None) or getattr(booking, "primary_meal", None) or "—").upper()
    primary_blood  = getattr(booking, "blood_group", "") or "—"
    primary_age    = getattr(booking, "primary_age", None)

    count_m = 1 if primary_gender == "M" else 0
    count_f = 1 if primary_gender == "F" else 0
    count_o = 1 if primary_gender not in ("M", "F") else 0
    count_veg = 1 if primary_meal == "VEG" else 0
    count_nonveg = 1 if primary_meal in ("NONVEG", "NON-VEG", "NON VEG") else 0
    count_vegan = 1 if primary_meal == "VEGAN" else 0

    guest_rows = [{
        "name": billed_name,
        "gender": primary_gender,
        "age": (str(primary_age).strip() if primary_age not in (None, "") else "—"),
        "meal": (primary_meal if primary_meal != "—" else "—"),
        "blood": primary_blood,
        "role": "Primary",
    }]

    for cobj in comps:
        g = (cobj.get("gender") or "O").upper()
        a = str(cobj.get("age") or "—")
        meal = (cobj.get("meal_preference") or cobj.get("meal") or "—").upper()
        blood = cobj.get("blood_group") or "—"
        name = cobj.get("name") or "—"
        if g == "M": count_m += 1
        elif g == "F": count_f += 1
        else: count_o += 1
        if meal == "VEG": count_veg += 1
        elif meal in ("NONVEG", "NON-VEG", "NON VEG"): count_nonveg += 1
        elif meal == "VEGAN": count_vegan += 1
        guest_rows.append({
            "name": name, "gender": g, "age": a, "meal": meal, "blood": blood, "role": "Companion",
        })

    gender_mix = f"M:{count_m} • F:{count_f} • O:{count_o}"
    meal_bits = []
    if count_veg: meal_bits.append(f"VEG: {count_veg}")
    if count_nonveg: meal_bits.append(f"NON-VEG: {count_nonveg}")
    if count_vegan: meal_bits.append(f"VEGAN: {count_vegan}")
    meal_mix = " • ".join(meal_bits) if meal_bits else "—"

    # === Canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    # ==== PAGE 1 (Single page for invoice + booking details + footer QR)
    # Header + meta + billed-to panel
    y_after_header = _invoice_page_header_and_meta(
        c,
        logo_filename=logo_filename,
        invoice_title="Invoice",
        order_id=order_id,
        booking_date=getattr(order, "created_at", None) or datetime.now(),
        meta_right={
            "Status": "PAID" if getattr(order, "paid", False) else "UNPAID",
            "Event": getattr(getattr(booking, "event", None), "name", "—"),
            "Check-in": check_in_txt,
            "Check-out": check_out_txt,
            "Guests": str(guests_total),
            "Category": category_name or "—",
        },
        billed_to_name=billed_name,
        billed_to_email=billed_email,
        contact_phone=phone,
    )

    # ---------- Build invoice line items from booking.pricing_breakdown ----------
    def _ii(x):
        try:
            return int(x)
        except Exception:
            return 0

    bd = dict(getattr(booking, "pricing_breakdown", {}) or {})
    items: List[Dict[str, float]] = []
    taxes_fees: List[Dict[str, float]] = []

    # Base + Extras (from breakdown if present)
    base = bd.get("base") or {}
    extra_counts = bd.get("extra_counts") or {}
    extra_unit_prices = bd.get("extra_unit_prices") or {}

    # Base line
    base_includes = _ii(base.get("includes"))
    base_price    = _ii(base.get("price_inr"))
    if base_includes and base_price:
        lbl = f"{pkg_name} — base for {base_includes} guest{'s' if base_includes != 1 else ''}"
        items.append({"label": lbl, "amount": base_price})
    else:
        # fallback (older snapshots)
        subtotal_before = _ii(bd.get("total_inr_before_promo") or bd.get("total_inr"))
        if subtotal_before:
            items.append({"label": f"{pkg_name} — subtotal", "amount": subtotal_before})

    # Extras (adult / half / free)
    if extra_counts:
        a_count = _ii(extra_counts.get("adult"))
        h_count = _ii(extra_counts.get("child_half"))
        f_count = _ii(extra_counts.get("child_free"))
        a_price = _ii(extra_unit_prices.get("adult_inr"))
        h_price = _ii(extra_unit_prices.get("child_half_inr"))

        if a_count > 0 and a_price:
            items.append({"label": f"Extra adult × {a_count}", "amount": a_count * a_price})
        if h_count > 0 and h_price:
            items.append({"label": f"Child (half) × {h_count}", "amount": h_count * h_price})
        if f_count > 0:
            items.append({"label": f"Child (free) × {f_count}", "amount": 0})

    # Promo (negative line)
    promo = bd.get("promo") or {}
    promo_discount = _ii(promo.get("discount_inr") or getattr(booking, "promo_discount_inr", 0))
    if promo_discount:
        taxes_fees.append({"label": "Promotion / Discount", "amount": -abs(promo_discount)})

    # Convenience fee (split into MDR + GST if available)
    conv = bd.get("convenience") or {}
    platform_fee_inr = _ii(conv.get("platform_fee_inr"))
    platform_gst_inr = _ii(conv.get("platform_gst_inr"))
    old_conv_inr = _ii(bd.get("convenience_fee_inr"))  # legacy combined number

    if platform_fee_inr or platform_gst_inr:
        if platform_fee_inr:
            taxes_fees.append({"label": "Platform fee (MDR)", "amount": platform_fee_inr})
        if platform_gst_inr:
            taxes_fees.append({"label": "GST on platform fee", "amount": platform_gst_inr})
    elif old_conv_inr:
        taxes_fees.append({"label": "Convenience Fee", "amount": old_conv_inr})

    # Items block (grand total always from order)
    y_after_items = _invoice_items_table(
        c,
        y=y_after_header,
        items=items if items else [{"label": pkg_name, "amount": grand_total_rupees}],
        taxes_fees=taxes_fees,
        grand_total_rupees=grand_total_rupees,
    )

    # Reserve footer QR: bottom_limit ensures nothing crosses into footer
    FOOTER_H = 22 * mm
    bottom_limit = BOTTOM + FOOTER_H + GRID

    # Booking details + guest list, fit in remaining space
    _booking_details_and_guests_fit_single_page(
        c,
        start_y=y_after_items,
        bottom_limit=bottom_limit,
        property_name=property_name,
        unit_type_name=unit_type_name,
        category_name=category_name,
        unit_labels=unit_labels,
        event_dates=(check_in_txt, check_out_txt),
        guests_total=guests_total,
        gender_mix=gender_mix,
        meal_mix=meal_mix,
        guest_rows=guest_rows,
    )

    # Footer QR (always at page bottom)
    _invoice_qr_footer(c, payload=invoice_qr)

    # ==== PAGE 2: Entry Pass
    c.showPage()
    _pass_page(
        c,
        bg_filename=pass_bg_filename,
        event_title=(getattr(getattr(booking, "event", None), "name", "Highway to Heal") or "Highway to Heal").upper(),
        pass_label=pkg_name,
        attendee=billed_name,
        order_id=order_id,
        amount_rupees=grand_total_rupees,
        dates=travel_dates or check_in_txt,
        venue=venue or property_name,
        qr_payload=pass_qr,
        pass_logo_filename=logo_filename,
    )
    c.showPage()

    c.save()
    return buf.getvalue()


# =====================================================================
# PUBLIC API 2: Backward-compatible single-page ticket
# =====================================================================

def build_ticket_pdf(*, order_id: str, user_name: str, package_name: str, amount_inr: int) -> bytes:
    """
    Minimal pass PDF (compat). Preserves function signature.
    Uses the same money/₹ handling and QR robustness.
    """
    ensure_unicode_font()
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)

    p.setFont(_FONT_BOLD, 20)
    p.drawString(30 * mm, PAGE_H - 40 * mm, "Highway to Heal — Travel Pass")

    p.setFont(_FONT_BODY, 12)
    y = PAGE_H - 60 * mm
    p.drawString(30 * mm, y, f"Order: {order_id}"); y -= 10 * mm
    p.drawString(30 * mm, y, f"Name: {user_name}"); y -= 10 * mm
    p.drawString(30 * mm, y, f"Package: {package_name}"); y -= 10 * mm
    p.drawString(30 * mm, y, f"Amount Paid: {money(amount_inr)}")

    _draw_qr(p, f'{{"type":"pass","order_id":"{order_id}"}}', 30 * mm, 20 * mm, size=35 * mm)

    p.setFont(_FONT_BODY, 10)
    p.drawString(30 * mm, 20 * mm, "Present this PDF at the event gate with a valid ID.")

    p.showPage()
    p.save()
    return buffer.getvalue()
