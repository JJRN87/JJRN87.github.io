"""Normalize the official RIPREE CSV and publish a bounded, immutable local snapshot.

This does not upload anything. Only a successful fetch advances the official
retrieval date. Offline input requires its actual retrieval date explicitly.
The mutable manifest is replaced last; failed updates leave it unchanged.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

SOURCE_URL = "https://energia.serviciosmin.gob.es/Ripree/ExportarInstalaciones/Export"
DOWNLOAD_URL = "https://energia.serviciosmin.gob.es/Ripree/ExportarInstalaciones/GenerarExcel"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_TEXT = 4000
MIN_STATIONS = 1000
MAX_STATIONS = 30000
MAX_ROWS = 200000
MAX_PERIODS = 50
MAX_CONNECTIONS = 100
MAX_DEPTH = 32
MAX_CONTAINERS = 100000
MAX_TOKENS = 2000000
MAX_MANIFEST_BYTES = 16 * 1024
MANIFEST_FILE = "manifest.json"
HEADERS = (
    "COMUNIDAD AUTONOMA", "PROVINCIA", "MUNICIPIO", "LATITUD", "LONGITUD",
    "NOMBRE INSTALACION", "DIRECCIÓN", "CODIGO POSTAL", "LOCALIZACION",
    "TIPO HORARIO APERTURA", "HORARIO APERTURA", "NOMBRE OPERADOR",
    "COD.OPERADOR", "TIPOS DE SERVICIOS", "COD.INSTALACION",
    "ID. PUNTO DE RECARGA", "ACCESIBILIDAD", "METODOS DE PAGOS",
    "COD. PUNTO DE RECARGA", "ID. CONECTOR", "TIPO CONECTOR", "TIPO DE CARGA",
    "FORMATO", "POTENCIA MAXIMA", "VOLTAJE", "INTENSIDAD",
    "FECHA DE ULTIMA MODIFICACION",
)
MANIFEST_KEYS = {
    "schema", "source", "retrievedFromOfficialAt", "publishedAt", "sha256",
    "file", "compressedBytes", "uncompressedBytes", "stationCount", "sourceSha256",
}
DAYS = {"lunes": 1, "martes": 2, "miercoles": 3, "jueves": 4,
        "viernes": 5, "sabado": 6, "domingo": 7}
DAY_PATTERN = re.compile(r"\b(" + "|".join(DAYS) + r")\b\s*", re.I)
INTERVAL_PATTERN = re.compile(r"\((\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})\)")
BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
RESTRICTED = re.compile(
    r"\bacceso\s+restringido\b|\bprivad[oa]s?\b|\b(?:empleados|trabajadores)\b|"
    r"\bproximamente\b|\b(?:solo|exclusiv[oa]s?\s+para)\s+(?:clientes|residentes)\b|"
    r"\bresidentes\b(?=\s*$)|\bfuera\s+de\s+servicio\b",
    re.I,
)


class CatalogError(ValueError):
    """An input or publication guard rejected this update."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def instant(value: str | dt.datetime) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "Timestamp requires an explicit timezone")
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise CatalogError("Invalid ISO timestamp") from error


def iso(value: dt.datetime) -> str:
    return instant(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", value.casefold())
                   if unicodedata.category(c) != "Mn")


def clean(value: str) -> str:
    require(len(value) <= MAX_TEXT + 4, "CSV field exceeds text limit")
    value = value.strip()
    if value.startswith('="') and value.endswith('"'):
        value = value[2:-1]
    # Preserve word boundaries in addresses/conditions that contain line breaks.
    value = "".join(" " if c.isspace() else "" if ord(c) < 32 or 127 <= ord(c) <= 159 or c in BIDI else c
                    for c in value)
    value = unicodedata.normalize("NFC", value).strip()
    require(len(value) <= MAX_TEXT, "Cleaned field exceeds text limit")
    return value


def restricted_access(name: str) -> bool:
    text = fold(name)
    for match in RESTRICTED.finditer(text):
        preceding = text[max(0, match.start() - 35):match.start()]
        if re.search(r"\b(?:no|not|no solo)\s*$", preceding):
            continue
        # Ownership of the land does not determine whether charging is public.
        if match.group().startswith("privad") and re.search(r"\bpropiedad\s*$", preceding):
            continue
        return True
    return False


def parse_schedule(row: dict[str, str]) -> tuple[bool, tuple, tuple] | None:
    """Return allDay, periods, unknownDays. Unknown days never receive periods."""
    kind = row["TIPO HORARIO APERTURA"]
    text = fold(row["HORARIO APERTURA"])
    if kind == "24/7" and not text:
        return True, (), ()
    if kind not in ("24/7", "Horario habitual") or not text:
        return None
    matches = list(DAY_PATTERN.finditer(text))
    if not matches or text[:matches[0].start()].strip():
        return None
    seen, ambiguous, periods = set(), set(), set()
    for index, match in enumerate(matches):
        day = DAYS[match.group(1)]
        seen.add(day)
        body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        intervals = list(INTERVAL_PATTERN.finditer(body))
        # Do not salvage an unrecognised suffix, weekday or free-text exception.
        if not intervals or INTERVAL_PATTERN.sub("", body).strip():
            return None
        for period in intervals:
            h1, m1, h2, m2 = map(int, period.groups())
            if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
                ambiguous.add(day)
                continue
            start, end = h1 * 60 + m1, h2 * 60 + m2
            if (end - start) % 1440 < 5:
                ambiguous.add(day)
            else:
                periods.add((day, start, end))
    if kind == "24/7":
        # The source's 00:00–00:00 convention is not documented; do not infer it.
        expected = {(day, 0, 1439) for day in range(1, 8)}
        return (True, (), ()) if not ambiguous and periods == expected else None
    unknown = ambiguous | (set(range(1, 8)) - seen)
    valid = tuple(sorted(p for p in periods if p[0] not in unknown))
    if not valid or len(valid) > MAX_PERIODS:
        return None
    return False, valid, tuple(sorted(unknown))


def read_bounded(stream, maximum: int) -> bytes:
    result = bytearray()
    while True:
        block = stream.read(min(65536, maximum + 1 - len(result)))
        if not block:
            return bytes(result)
        result.extend(block)
        require(len(result) <= maximum, "Input exceeds byte limit")


def read_csv(data: bytes) -> list[dict[str, str]]:
    require(0 < len(data) <= MAX_SOURCE_BYTES, "CSV size outside allowed range")
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        elif b"\x00" in data[:100]:
            text = data.decode("utf-16-le")
        else:
            text = data.decode("utf-8-sig")
        prior_limit = csv.field_size_limit(MAX_TEXT + 4)
        try:
            # RIPREE exports unescaped literal quotes in names (e.g. La Pasionaria).
            # Accept its CSV dialect, but reject any shifted/missing/extra columns.
            reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";", strict=False)
            names = reader.fieldnames or []
            require(len(names) == len(HEADERS) and set(names) == set(HEADERS),
                    "Official CSV columns changed; review the source schema")
            rows = []
            for raw in reader:
                require(len(rows) < MAX_ROWS, "Too many CSV rows")
                require(None not in raw and all(value is not None for value in raw.values()),
                        "CSV row has missing or extra fields")
                rows.append({key: clean(value) for key, value in raw.items()})
            require(bool(rows), "Empty official CSV")
            return rows
        finally:
            csv.field_size_limit(prior_limit)
    except (UnicodeError, csv.Error) as error:
        raise CatalogError("Invalid official CSV encoding or syntax") from error


def coordinates(row: dict[str, str]) -> tuple[float, float] | None:
    try:
        lat, lon = (float(row[k].replace(",", ".")) for k in ("LATITUD", "LONGITUD"))
        return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) and 27 <= lat <= 44.5 and -19 <= lon <= 5 else None
    except ValueError:
        return None


def join(values) -> str:
    result = " · ".join(sorted({v for v in values if v}))
    require(len(result) <= MAX_TEXT, "Aggregated text exceeds limit")
    return result


def normalize(data: bytes, retrieved_at: dt.datetime) -> tuple[dict, dict]:
    rows = read_csv(data)
    sites = collections.defaultdict(list)
    for row in rows:
        sites[row["COD.INSTALACION"]].append(row)
    excluded = collections.Counter()
    stations = []
    incomplete_dates = 0
    for identifier, group in sorted(sites.items()):
        schedules = {parse_schedule(row) for row in group}
        if None in schedules:
            excluded["unknown_or_invalid_hours"] += 1
            continue
        if len(schedules) != 1:
            excluded["conflicting_hours"] += 1
            continue
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", identifier) or any(
                not row["ID. PUNTO DE RECARGA"] or not row["ID. CONECTOR"] for row in group):
            excluded["invalid_id"] += 1
            continue
        if any(restricted_access(row[field]) for row in group for field in ("NOMBRE INSTALACION", "DIRECCIÓN")):
            excluded["explicit_restricted_access"] += 1
            continue
        # Selection is stable if the export permutes connector rows.
        group.sort(key=lambda r: (r["ID. PUNTO DE RECARGA"], r["ID. CONECTOR"], tuple(r[k] for k in HEADERS)))
        points = [coordinates(row) for row in group]
        if any(point is None for point in points):
            excluded["invalid_coordinates"] += 1
            continue
        lat, lon = points[0]
        if any(math.hypot((x - lat) * 111, (y - lon) * 111 * math.cos(math.radians(lat))) > .25 for x, y in points):
            excluded["inconsistent_site_coordinates"] += 1
            continue
        zones = {"Atlantic/Canary" if row["PROVINCIA"] in ("Las Palmas", "Palmas, Las", "Santa Cruz de Tenerife")
                 else "Europe/Madrid" for row in group}
        if len(zones) != 1:
            excluded["inconsistent_timezone"] += 1
            continue
        connectors, conflict = {}, False
        for row in group:
            key = row["ID. PUNTO DE RECARGA"], row["ID. CONECTOR"]
            power_match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*kW", row["POTENCIA MAXIMA"])
            power = float(power_match[1].replace(",", ".")) if power_match else 0
            item = (row["TIPO CONECTOR"], power if 0 < power <= 10000 else None,
                    row["TIPO DE CARGA"], row["FORMATO"])
            if key in connectors and connectors[key] != item:
                conflict = True
            connectors[key] = item
        counts = collections.Counter(connectors.values())
        point_count = len({row["ID. PUNTO DE RECARGA"] for row in group})
        if conflict or len(counts) > MAX_CONNECTIONS or point_count > 10000 or any(n > 1000 for n in counts.values()):
            excluded["invalid_or_conflicting_connectors"] += 1
            continue
        dates, dates_incomplete = [], False
        for row in group:
            try:
                date = dt.datetime.strptime(row["FECHA DE ULTIMA MODIFICACION"], "%d/%m/%Y %H:%M:%S")
                # Source timestamps have no timezone. Allow the current local calendar day.
                if date.date() > instant(retrieved_at).date() + dt.timedelta(days=1):
                    raise ValueError("Future record")
                dates.append(date)
            except ValueError:
                dates_incomplete = True
        incomplete_dates += dates_incomplete
        all_day, periods, unknown = next(iter(schedules))
        first = group[0]
        stations.append({
            "id": identifier, "name": first["NOMBRE INSTALACION"], "lat": lat, "lon": lon,
            "address": clean(", ".join(v for v in (first["DIRECCIÓN"], first["MUNICIPIO"]) if v)),
            "zone": next(iter(zones)), "allDay": all_day, "periods": [list(p) for p in periods],
            "unknownDays": list(unknown),
            "rawHours": min(row["HORARIO APERTURA"] for row in group),
            "accessibility": join(row["ACCESIBILIDAD"] or "NODISPONIBLE" for row in group),
            "operator": join(row["NOMBRE OPERADOR"] for row in group),
            "location": join(row["LOCALIZACION"] for row in group),
            "services": join(s.strip() for row in group for s in row["TIPOS DE SERVICIOS"].split(",")),
            "payment": join(s.strip() for row in group for s in row["METODOS DE PAGOS"].split(",")),
            "points": point_count,
            "connections": [list(key) + [count] for key, count in sorted(counts.items(), key=lambda item: str(item[0]))],
            "oldestRecord": min(dates).date().isoformat() if dates else "",
            "newestRecord": max(dates).date().isoformat() if dates else "",
            "recordDateIncomplete": dates_incomplete,
        })
    payload = {"schema": 2, "source": SOURCE_URL, "stations": stations}
    quality = {
        "sourceSha256": hashlib.sha256(data).hexdigest(), "inputRows": len(rows),
        "inputSites": len(sites), "stationCount": len(stations), "excluded": dict(sorted(excluded.items())),
        "allDayStations": sum(s["allDay"] for s in stations),
        "partialScheduleStations": sum(bool(s["unknownDays"]) for s in stations),
        "canaryStations": sum(s["zone"] == "Atlantic/Canary" for s in stations),
        "incompleteRecordDateStations": incomplete_dates,
    }
    return payload, quality


def check_json_tree(value) -> None:
    """Match Android's structural budgets before serialization/publication."""
    stack = [(value, 0)]
    containers = tokens = 0
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list, tuple)):
            depth += 1
            containers += 1
            tokens += 2  # Begin and end tokens.
            require(depth <= MAX_DEPTH and containers <= MAX_CONTAINERS, "JSON structure exceeds limit")
            if isinstance(item, dict):
                tokens += len(item)  # Object property names are tokens too.
                require(all(isinstance(k, str) and len(k) <= 512 for k in item), "Invalid JSON key")
                stack.extend((v, depth) for v in item.values())
            else:
                stack.extend((v, depth) for v in item)
        else:
            tokens += 1
            if isinstance(item, str):
                require(len(item) <= MAX_TEXT, "JSON string exceeds limit")
            elif isinstance(item, float):
                require(math.isfinite(item), "Non-finite JSON number")
            elif isinstance(item, int):
                require(len(str(item)) <= 64, "JSON number exceeds limit")
            else:
                require(item is None, "Unsupported JSON value")
        require(tokens <= MAX_TOKENS, "JSON token count exceeds limit")


def encode_json(value, maximum: int = MAX_JSON_BYTES) -> bytes:
    check_json_tree(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    require(len(encoded) <= maximum, "JSON exceeds byte limit")
    return encoded


def deterministic_gzip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buffer, mode="wb", mtime=0) as stream:
        stream.write(data)
    result = buffer.getvalue()
    require(len(result) <= MAX_COMPRESSED_BYTES, "Compressed catalog exceeds byte limit")
    return result


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_previous(directory: Path) -> dict | None:
    path = directory / MANIFEST_FILE
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            data = read_bounded(stream, MAX_MANIFEST_BYTES)
        result = json.loads(data, object_pairs_hook=no_duplicate_keys,
                            parse_constant=lambda _: (_ for _ in ()).throw(CatalogError("Non-finite JSON")))
        check_json_tree(result)
        require(isinstance(result, dict) and set(result) == MANIFEST_KEYS, "Existing manifest schema changed")
        require(type(result["schema"]) is int and result["schema"] == 1 and result["source"] == SOURCE_URL,
                "Invalid existing manifest")
        digest = result["sha256"]
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "Invalid existing SHA")
        require(result["file"] == f"catalog-{digest}.bin", "Unsafe existing blob filename")
        require(type(result["stationCount"]) is int and MIN_STATIONS <= result["stationCount"] <= MAX_STATIONS,
                "Invalid existing station count")
        for field, maximum in (("compressedBytes", MAX_COMPRESSED_BYTES), ("uncompressedBytes", MAX_JSON_BYTES)):
            require(type(result[field]) is int and 0 < result[field] <= maximum, "Invalid existing size")
        require(re.fullmatch(r"[0-9a-f]{64}", result["sourceSha256"]) is not None, "Invalid existing source SHA")
        require(instant(result["retrievedFromOfficialAt"]) <= instant(result["publishedAt"]), "Invalid existing dates")
        with (directory / result["file"]).open("rb") as stream:
            blob = read_bounded(stream, MAX_COMPRESSED_BYTES)
        require(len(blob) == result["compressedBytes"] and hashlib.sha256(blob).hexdigest() == digest,
                "Existing catalog failed integrity check")
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise CatalogError("Cannot safely use previous publication: " + str(error)) from error


@contextlib.contextmanager
def publication_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".ripree-publish.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise CatalogError("Another publication holds .ripree-publish.lock; verify no job is running before removing it") from error
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def write_atomic(path: Path, data: bytes, immutable: bool = False) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ripree-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # Windows rename and POSIX hard-link creation both fail if the name exists.
            # POSIX rename alone would overwrite it, so it is deliberately not used there.
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                with path.open("rb") as stream:
                    require(read_bounded(stream, MAX_COMPRESSED_BYTES) == data, "Immutable blob already exists with different contents")
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(data: bytes, output: Path, retrieved_at: str | dt.datetime,
            published_at: str | dt.datetime | None = None) -> tuple[dict, dict]:
    retrieved = instant(retrieved_at)
    published = instant(published_at) if published_at is not None else utc_now()
    require(retrieved <= published <= utc_now() + dt.timedelta(minutes=2), "Publication/retrieval dates are in the future or reversed")
    payload, quality = normalize(data, retrieved)
    count = len(payload["stations"])
    require(MIN_STATIONS <= count <= MAX_STATIONS, "Station count outside publication limits")
    encoded = encode_json(payload)
    blob = deterministic_gzip(encoded)
    digest = hashlib.sha256(blob).hexdigest()
    manifest = {
        "schema": 1, "source": SOURCE_URL,
        "retrievedFromOfficialAt": iso(retrieved), "publishedAt": iso(published),
        "sha256": digest, "file": f"catalog-{digest}.bin", "compressedBytes": len(blob),
        "uncompressedBytes": len(encoded), "stationCount": count,
        "sourceSha256": hashlib.sha256(data).hexdigest(),
    }
    quality.update({"retrievedFromOfficialAt": iso(retrieved), "publishedAt": iso(published),
                    "sha256": digest, "compressedBytes": len(blob), "uncompressedBytes": len(encoded)})
    manifest_bytes = encode_json(manifest, MAX_MANIFEST_BYTES)
    quality_bytes = encode_json(quality, 65536)
    with publication_lock(output):
        previous = read_previous(output)
        if previous is not None:
            require(count * 4 >= previous["stationCount"] * 3, "Station count fell by more than 25%; manual source review required")
            require(retrieved >= instant(previous["retrievedFromOfficialAt"]), "Refusing to replace a newer official retrieval")
            require(published >= instant(previous["publishedAt"]), "Refusing to replace a newer publication")
        write_atomic(output / manifest["file"], blob, immutable=True)
        # This report is local evidence, not part of the Android protocol.
        write_atomic(output / "quality-report.json", quality_bytes)
        write_atomic(output / MANIFEST_FILE, manifest_bytes)  # Commit point; always last.
    return manifest, quality


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise CatalogError("Official endpoint redirected; review its URL before fetching")


def download_official(attempts: int = 2, timeout: int = 120) -> tuple[bytes, dt.datetime]:
    require(1 <= attempts <= 3 and 1 <= timeout <= 120, "Invalid download retry/timeout limits")
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(attempts):
        request = urllib.request.Request(
            DOWNLOAD_URL, data=b'{"soloConsolidado":true}',
            headers={"Content-Type": "application/json", "Accept": "text/csv, application/octet-stream",
                     "User-Agent": "GasHunter-official-catalog/2.0"},
        )
        try:
            started = time.monotonic()
            with opener.open(request, timeout=timeout) as response:
                require(response.status == 200 and response.url == DOWNLOAD_URL, "Unexpected official response/redirect")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    require(declared.isdecimal() and 0 < int(declared) <= MAX_SOURCE_BYTES, "Official response exceeds size limit")
                result = bytearray()
                while True:
                    require(time.monotonic() - started <= timeout, "Official download exceeded time budget")
                    block = response.read(min(65536, MAX_SOURCE_BYTES + 1 - len(result)))
                    if not block:
                        break
                    result.extend(block)
                    require(len(result) <= MAX_SOURCE_BYTES, "Official response exceeds size limit")
                require(time.monotonic() - started <= timeout, "Official download exceeded time budget")
                require(bool(result), "Empty official response")
                if declared is not None:
                    require(len(result) == int(declared), "Truncated official response")
                return bytes(result), utc_now()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise CatalogError(f"Official service returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt + 1 == attempts:
                raise CatalogError("Official download failed: " + type(error).__name__) from error
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise CatalogError("Official download failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Local publication directory; no upload is performed")
    parser.add_argument("--source", type=Path, help="Optional previously downloaded official CSV")
    parser.add_argument("--retrieved-at", help="Actual ISO retrieval timestamp; mandatory for --source")
    args = parser.parse_args(argv)
    if args.source is not None and not args.retrieved_at:
        parser.error("--source requires its actual --retrieved-at timestamp")
    if args.source is None and args.retrieved_at:
        parser.error("--retrieved-at is only valid with --source; live fetches use their actual completion time")
    try:
        if args.source is not None:
            with args.source.open("rb") as stream:
                data = read_bounded(stream, MAX_SOURCE_BYTES)
            retrieved = instant(args.retrieved_at)
        else:
            data, retrieved = download_official()
        manifest, quality = publish(data, args.output, retrieved)
        print(json.dumps({"manifest": manifest, "quality": quality}, ensure_ascii=True, indent=2))
        return 0
    except (CatalogError, OSError) as error:
        print("Catalog update rejected; previous manifest retained: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
