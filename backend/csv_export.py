"""CSV download helpers shared by the administrator and account-holder APIs."""

from __future__ import annotations

import csv
import io

from fastapi.responses import Response


def csv_text(value) -> str:
    """Neutralise spreadsheet formula injection in free-text cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def csv_response(filename: str, header: list[str], rows) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def iso(value) -> str:
    return value.isoformat(timespec="seconds") + "Z" if value else ""


def cents_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"
