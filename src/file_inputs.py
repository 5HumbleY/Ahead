import codecs
from collections import Counter
import csv
import hashlib
import io
from pathlib import Path


def text_with_info(path, encoding="auto", errors="strict"):
    path = Path(path).resolve()
    raw = path.read_bytes()
    chosen = encoding
    basis = "explicit"
    ambiguous = False
    candidates = []
    if encoding in {None, "auto"}:
        for marker, codec in [
            (codecs.BOM_UTF32_LE, "utf-32"),
            (codecs.BOM_UTF32_BE, "utf-32"),
            (codecs.BOM_UTF16_LE, "utf-16"),
            (codecs.BOM_UTF16_BE, "utf-16"),
            (codecs.BOM_UTF8, "utf-8-sig"),
        ]:
            if raw.startswith(marker):
                chosen = codec
                basis = "byte_order_mark"
                break
        else:
            try:
                raw.decode("utf-8")
                chosen = "utf-8"
                basis = "strict_utf8"
            except UnicodeDecodeError:
                from charset_normalizer import from_bytes

                detected = from_bytes(raw)
                best = detected.best()
                if best is None:
                    raise ValueError(
                        "No text encoding detected; supply encoding explicitly: "
                        + str(path)
                    )
                chosen = best.encoding
                basis = "charset_inference"
                ambiguous = True
                candidates = [
                    {
                        "encoding": x.encoding,
                        "coherence_percent": x.percent_coherence,
                        "chaos_percent": x.percent_chaos,
                    }
                    for x in list(detected)[:5]
                ]
    text = raw.decode(chosen, errors=errors)
    info = {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": chosen,
        "encoding_basis": basis,
        "encoding_ambiguous": ambiguous,
        "encoding_candidates": candidates,
        "errors": errors,
        "reader": "Standard CSV parsing preserves quoted delimiters; encoding inference is not authoritative metadata.",
    }
    return (text, info)


def rows_with_info(path, delimiter=None, encoding="auto", errors="strict"):
    (text, info) = text_with_info(path, encoding, errors)
    if delimiter is None:
        if Path(path).suffix.lower() == ".tsv":
            delimiter = "\t"
        else:
            try:
                delimiter = (
                    csv.Sniffer().sniff(text[:65536], delimiters=",;\t|").delimiter
                )
            except csv.Error:
                delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    info.update(
        delimiter=delimiter,
        record_count=len(rows),
        record_widths=dict(Counter(map(len, rows))),
    )
    return (rows, info)


def numeric(value):
    if not str(value).strip():
        return False
    try:
        float(str(value).strip().replace(",", "").rstrip("%"))
        return True
    except ValueError:
        return False


def profile_rows(rows, info):
    candidates = []
    for index, row in enumerate(rows[:24]):
        cells = [s.strip() for s in row]
        nonempty = [s for s in cells if s]
        if (
            len(nonempty) >= 2
            and len(nonempty) >= len(row) * 0.75
            and (len(set(nonempty)) == len(nonempty))
        ):
            textual = sum((not numeric(s) for s in nonempty))
            if textual == len(nonempty):
                candidates.append(
                    {
                        "record_index": index,
                        "columns": row,
                        "basis": "Structural candidate only; select the actual header explicitly",
                    }
                )

    def preview(indices):
        return [
            {
                "record_index": i,
                "cells": [
                    v if len(v) <= 240 else v[:240] + " [cell preview truncated]"
                    for v in rows[i]
                ],
            }
            for i in indices
        ]

    return {
        **info,
        "format": "delimited_records",
        "header_candidates": candidates,
        "head_records": preview(range(min(6, len(rows)))),
        "tail_records": preview(
            range(max(min(6, len(rows)), len(rows) - 10), len(rows))
        ),
        "complete_data": False,
        "meaning": "These are parsed-record previews, including any preamble/footer. Load full records/table for computation.",
        "read_records": 'csv_rows(path, delimiter=None, encoding="auto")',
        "read_table": 'read_table(path, header=actual_record_index, delimiter=None, encoding="auto")',
    }


def read_resource(kind, path, **options):
    if kind == "text":
        start = options.pop("start", 0)
        length = options.pop("length", None)
        if start < 0 or (length is not None and length < 1):
            raise ValueError("Invalid text page bounds")
        (text, info) = text_with_info(path, **options)
        return (text[start:] if length is None else text[start : start + length], info)
    if kind in {"rows", "table", "profile"}:
        header = options.pop("header", 0)
        (rows, info) = rows_with_info(path, **options)
        if kind == "rows":
            return (rows, info)
        if kind == "profile":
            return (profile_rows(rows, info), info)
        if not isinstance(header, int) or not 0 <= header < len(rows):
            raise ValueError("Select an actual header record from inspect_file(path)")
        columns = rows[header]
        if (
            not columns
            or any((not x.strip() for x in columns))
            or len(set(columns)) != len(columns)
        ):
            raise ValueError(
                "The selected header has empty/duplicate names; inspect the actual records or use csv_rows"
            )
        data = rows[header + 1 :]
        wider = [
            header + 1 + i for (i, row) in enumerate(data) if len(row) > len(columns)
        ]
        if wider:
            raise ValueError(
                "Records wider than the selected header: " + str(wider[:10])
            )
        import pandas as pd

        frame = pd.DataFrame(
            [row + [""] * (len(columns) - len(row)) for row in data],
            columns=columns,
            dtype=object,
        )
        frame.attrs["source"] = {
            **info,
            "header_record": header,
            "values": "Strings preserved; explicit analytical conversions belong to the task",
        }
        return (frame, info)
    raise ValueError("Unknown source reader: " + kind)


def read_source(path, encoding="auto", errors="strict", start=0, length=None):
    return read_resource(
        "text", path, encoding=encoding, errors=errors, start=start, length=length
    )[0]


def inspect_file(path, delimiter=None, encoding="auto", errors="strict"):
    return read_resource(
        "profile", path, delimiter=delimiter, encoding=encoding, errors=errors
    )[0]


def csv_rows(path, delimiter=None, encoding="auto", errors="strict"):
    return read_resource(
        "rows", path, delimiter=delimiter, encoding=encoding, errors=errors
    )[0]


def read_table(path, header=0, delimiter=None, encoding="auto", errors="strict"):
    return read_resource(
        "table",
        path,
        header=header,
        delimiter=delimiter,
        encoding=encoding,
        errors=errors,
    )[0]
