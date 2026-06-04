"""PDF generation for sales documents (quotes, orders, invoices, credit notes).

Uses xhtml2pdf (pure-Python, no system libraries) to render a simple,
print-oriented HTML template to a PDF byte string. The PDF templates are
standalone (they do NOT extend base.html) and use only the limited CSS subset
xhtml2pdf supports.
"""
from io import BytesIO

from django.http import HttpResponse
from django.template.loader import render_to_string


def render_to_pdf(template_src, context):
    """Render a template to PDF bytes (or None if rendering failed)."""
    from xhtml2pdf import pisa
    html = render_to_string(template_src, context)
    buf = BytesIO()
    result = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    if result.err:
        return None
    return buf.getvalue()


def pdf_response(filename, template_src, context, download=True):
    """Return an HttpResponse with the rendered PDF as an attachment."""
    pdf = render_to_pdf(template_src, context)
    if pdf is None:
        return HttpResponse("Could not generate PDF.", status=500)
    resp = HttpResponse(pdf, content_type="application/pdf")
    disposition = "attachment" if download else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return resp
