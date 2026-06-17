"""Generate an inline SVG QR code for a URL (no external services).

Used on the thank-you page for a scannable link to the dealership in Google
Maps. Returns None if the qrcode library isn't installed, so the template can
fall back to a plain link.
"""

try:
    import qrcode
    import qrcode.image.svg
except ImportError:
    qrcode = None


def qr_svg(data):
    if not qrcode or not data:
        return None
    try:
        img = qrcode.make(
            data,
            image_factory=qrcode.image.svg.SvgPathImage,
            box_size=10,
            border=2,
        )
        svg = img.to_string().decode("utf-8")
        # Strip any XML prolog so it can be inlined directly in HTML.
        if svg.startswith("<?xml"):
            svg = svg[svg.index("?>") + 2:].lstrip()
        return svg
    except Exception:
        return None
