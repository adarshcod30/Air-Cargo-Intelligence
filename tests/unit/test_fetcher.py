"""Tests for content verification.

These exist because of an observed failure, not a hypothetical one:
`April2k26Annex5.pdf` returns an HTML error page with HTTP 200, while the
real file lives at `April2k26Anex5.pdf` (one fewer 'n'). Trusting the
status code or the file extension ingests the error page as cargo data.
"""

from services.ingestion.fetcher import sniff_media_type

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>"
HTML = b'\n<html lang="en" dir="ltr">\n<head>\n<script>window["x"]=1;</script>'
ZIP = b"PK\x03\x04\x14\x00\x00\x00"


class TestMediaSniffing:
    def test_pdf_recognised_by_magic_bytes(self):
        assert sniff_media_type(PDF) == "application/pdf"

    def test_html_recognised_even_when_declared_as_pdf(self):
        """The exact AAI trap: the server says PDF, the bytes say HTML."""
        assert sniff_media_type(HTML, declared="application/pdf") == "text/html"

    def test_xlsx_zip_container(self):
        assert sniff_media_type(ZIP) == "application/zip"

    def test_json_must_actually_parse(self):
        """A leading brace is weak evidence; confirm before believing it."""
        assert sniff_media_type(b'{"a": 1}') == "application/json"
        assert sniff_media_type(b"{not json at all") != "application/json"

    def test_bytes_beat_the_declared_header(self):
        assert sniff_media_type(PDF, declared="text/html") == "application/pdf"

    def test_unknown_content_falls_back(self):
        assert sniff_media_type(b"\x00\x01\x02\x03") == "application/octet-stream"
