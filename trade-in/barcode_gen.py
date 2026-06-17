"""Generate an inline Code128 SVG barcode for a serial number.

Code128 with the human-readable text underneath is the classic "official
document" look (think VIN sticker / certificate). Returns None if the library
isn't available so callers can degrade gracefully.
"""

import io

try:
    import barcode
    from barcode.writer import SVGWriter
    try:
        from barcode.writer import ImageWriter  # needs Pillow
    except Exception:
        ImageWriter = None
except ImportError:
    barcode = None
    ImageWriter = None


def barcode_svg(data):
    if not barcode or not data:
        return None
    try:
        b = barcode.get("code128", str(data), writer=SVGWriter())
        buf = io.BytesIO()
        b.write(buf, options={
            "module_width": 0.28,
            "module_height": 14.0,
            "font_size": 9,
            "text_distance": 3.5,
            "quiet_zone": 2.0,
            "background": "white",
            "foreground": "black",
        })
        svg = buf.getvalue().decode("utf-8")
        # Strip the XML prolog/doctype so it inlines cleanly in HTML.
        idx = svg.find("<svg")
        return svg[idx:] if idx != -1 else svg
    except Exception:
        return None


def barcode_png(data):
    """Code128 as PNG bytes (for email, where SVG isn't reliably supported).
    Returns None if unavailable."""
    if not barcode or not ImageWriter or not data:
        return None
    try:
        b = barcode.get("code128", str(data), writer=ImageWriter())
        buf = io.BytesIO()
        b.write(buf, options={
            "module_height": 10.0, "module_width": 0.25, "font_size": 8,
            "text_distance": 3.0, "quiet_zone": 1.0, "dpi": 200,
        })
        return buf.getvalue()
    except Exception:
        return None
