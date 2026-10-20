"""Offline, stdlib-only checks: python -m unittest -v test_ripree_catalog."""
import contextlib
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import ripree_catalog as catalog


RETRIEVED = "2026-10-20T00:00:00Z"
PUBLISHED = "2026-10-20T00:01:00Z"
NOW = dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc)
RAW_SOURCE = Path(__file__).resolve().parents[2] / "artifacts/ev-sources-2026-10-20/ripree-csv.raw"


def row(identifier="TEST1", **changes):
    result = dict.fromkeys(catalog.HEADERS, "")
    result.update({
        "PROVINCIA": "Madrid", "MUNICIPIO": "Madrid", "LATITUD": "40,4168", "LONGITUD": "-3,7038",
        "NOMBRE INSTALACION": "Punto público", "DIRECCIÓN": "Calle Ejemplo 1",
        "TIPO HORARIO APERTURA": "24/7", "COD.INSTALACION": identifier,
        "ID. PUNTO DE RECARGA": "EVSE" + identifier, "ID. CONECTOR": "1",
        "TIPO CONECTOR": "CCS", "POTENCIA MAXIMA": "50 kW", "TIPO DE CARGA": "DC",
        "FORMATO": "Cable", "ACCESIBILIDAD": "NODISPONIBLE",
        "FECHA DE ULTIMA MODIFICACION": "19/10/2026 12:00:00",
    })
    result.update(changes)
    return result


def csv_bytes(rows, encoding="utf-16"):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=catalog.HEADERS, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode(encoding)


def synthetic_source(count):
    return csv_bytes(row("S" + str(i)) for i in range(count))


def schedule(text, kind="Horario habitual"):
    return catalog.parse_schedule(row(**{"HORARIO APERTURA": text, "TIPO HORARIO APERTURA": kind}))


class ScheduleTests(unittest.TestCase):
    def test_all_day_requires_consistent_evidence(self):
        self.assertEqual(schedule("", "24/7"), (True, (), ()))
        full = " ".join(f"{day} (00:00 - 23:59)" for day in catalog.DAYS)
        self.assertEqual(schedule(full, "24/7"), (True, (), ()))
        self.assertIsNone(schedule(full.replace("23:59", "00:00"), "24/7"))
        self.assertIsNone(schedule(full.replace("23:59", "20:00", 1), "24/7"))
        self.assertIsNone(schedule("Lunes (00:00 - 23:59)", "24/7"))

    def test_split_hours_sorted_deduplicated_and_midnight_preserved(self):
        a = schedule("Martes (07:00 - 23:59) (00:00 - 01:00) Lunes (22:00 - 02:00)")
        b = schedule("Lunes (22:00 - 02:00) Martes (00:00 - 01:00) (07:00 - 23:59) (00:00 - 01:00)")
        self.assertEqual(a, b)
        self.assertEqual(a, (False, ((1, 1320, 120), (2, 0, 60), (2, 420, 1439)), (3, 4, 5, 6, 7)))

    def test_whole_ambiguous_day_is_unknown_not_closed_or_partly_open(self):
        text = "Lunes (08:00 - 18:00) (00:00 - 00:00) Martes (08:00 - 18:00)"
        self.assertEqual(schedule(text), (False, ((2, 480, 1080),), (1, 3, 4, 5, 6, 7)))
        self.assertIsNone(schedule("Lunes (00:00 - 00:00)"))
        self.assertIsNone(schedule("Lunes (08:00 - 18:00) (00:00 - 00:00)"))

    def test_short_placeholder_and_invalid_time_invalidate_only_that_day(self):
        for bad in ("(00:00 - 00:01)", "(01:00 - 01:04)", "(24:00 - 23:59)", "(00:61 - 20:00)"):
            with self.subTest(bad=bad):
                self.assertEqual(schedule("Lunes " + bad + " Martes (08:00 - 18:00)"),
                                 (False, ((2, 480, 1080),), (1, 3, 4, 5, 6, 7)))

    def test_accents_and_case(self):
        self.assertEqual(schedule("MIÉRCOLES (10:00 - 17:00) Sábado (10:00 - 17:00)"),
                         (False, ((3, 600, 1020), (6, 600, 1020)), (1, 2, 4, 5, 7)))

    def test_free_text_exceptions_unknown_weekdays_and_empty_rejected(self):
        for text in ("Lunes (08:00 - 18:00), salvo festivos", "Lunes cerrado",
                     "Lunes (08:00 - 18:00) Monday (08:00 - 18:00)", ""):
            with self.subTest(text=text):
                self.assertIsNone(schedule(text))

    def test_period_budget(self):
        text = "Lunes " + " ".join(f"({h:02}:00 - {h:02}:15) ({h:02}:30 - {h:02}:45)" for h in range(24))
        text += " Martes (00:00 - 01:00) (01:00 - 02:00) (02:00 - 03:00)"
        self.assertIsNone(schedule(text))


class NormalizationTests(unittest.TestCase):
    def test_explicit_restrictions_and_public_controls(self):
        restricted = (
            "(Acceso Restringido) General Química", "Telpark - Hosp. Ramon y Cajal residentes",
            "Parking empleados", "Sólo clientes", "Privado", "PROXIMAMENTE - Apertura",
        )
        public = ("Peñacerrada", "CRED LA CERRADA", "Parking Villacerrada",
                  "Propiedad privada, acceso público", "No solo clientes", "No privado")
        for text in restricted:
            with self.subTest(text=text):
                self.assertTrue(catalog.restricted_access(text))
        for text in public:
            with self.subTest(text=text):
                self.assertFalse(catalog.restricted_access(text))

    def test_restricted_resident_site_does_not_contaminate_neighbor(self):
        data = csv_bytes([
            row("2025000282", **{"NOMBRE INSTALACION": "Telpark - Hosp. Ramon y Cajal"}),
            row("2025000291", **{"NOMBRE INSTALACION": "Telpark - Hosp. Ramon y Cajal residentes"}),
            row("RVNAHW5DPBWDKIQECAMO", **{"NOMBRE INSTALACION": "(Acceso Restringido) General Química"}),
        ])
        payload, quality = catalog.normalize(data, catalog.instant(RETRIEVED))
        self.assertEqual([s["id"] for s in payload["stations"]], ["2025000282"])
        self.assertEqual(quality["excluded"], {"explicit_restricted_access": 2})

    def test_dates_controls_connector_dedup_and_source_scope(self):
        base = row(**{"NOMBRE INSTALACION": '="Punto\u202e\npúblico\u0001"',
                      "FECHA DE ULTIMA MODIFICACION": ""})
        newer = dict(base, **{"ID. CONECTOR": "2", "FECHA DE ULTIMA MODIFICACION": "19/10/2026 12:00:00"})
        payload, quality = catalog.normalize(csv_bytes([base, base, newer]), catalog.instant(RETRIEVED))
        station = payload["stations"][0]
        self.assertEqual(payload["schema"], 2)
        self.assertNotIn("retrieved", payload)
        self.assertEqual(station["name"], "Punto público")
        self.assertEqual(station["address"], "Calle Ejemplo 1, Madrid")
        self.assertEqual(station["connections"], [["CCS", 50.0, "DC", "Cable", 2]])
        self.assertEqual(station["points"], 1)
        self.assertEqual(station["oldestRecord"], "2026-10-19")
        self.assertEqual(station["newestRecord"], "2026-10-19")
        self.assertTrue(station["recordDateIncomplete"])
        self.assertEqual(quality["incompleteRecordDateStations"], 1)

    def test_unknown_disability_access_is_not_private(self):
        source = csv_bytes([row("A", ACCESIBILIDAD="NO"), row("B", ACCESIBILIDAD="NODISPONIBLE")])
        payload, _ = catalog.normalize(source, catalog.instant(RETRIEVED))
        self.assertEqual(len(payload["stations"]), 2)

    def test_conflicting_hours_and_connector_data_exclude_site(self):
        base = row()
        conflicts = [
            dict(base, **{"TIPO HORARIO APERTURA": "Horario habitual", "HORARIO APERTURA": "Lunes (08:00 - 18:00)"}),
            dict(base, **{"POTENCIA MAXIMA": "100 kW"}),
        ]
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                payload, _ = catalog.normalize(csv_bytes([base, conflict]), catalog.instant(RETRIEVED))
                self.assertEqual(payload["stations"], [])

    def test_coordinate_and_canary_timezone_guards(self):
        for lat, lon in (("NaN", "0"), ("90", "0"), ("40", "20"), ("inf", "0")):
            payload, quality = catalog.normalize(csv_bytes([row(LATITUD=lat, LONGITUD=lon)]), catalog.instant(RETRIEVED))
            self.assertEqual(payload["stations"], [])
            self.assertEqual(quality["excluded"]["invalid_coordinates"], 1)
        source = csv_bytes([row(PROVINCIA="Palmas, Las", LATITUD="28,1", LONGITUD="-15,4")])
        payload, _ = catalog.normalize(source, catalog.instant(RETRIEVED))
        self.assertEqual(payload["stations"][0]["zone"], "Atlantic/Canary")

    def test_encoding_schema_field_and_row_limits(self):
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-8", "utf-8-sig"):
            # BOM-less UTF-16 big-endian is not an official format.
            if encoding != "utf-16-be":
                self.assertEqual(len(catalog.read_csv(csv_bytes([row()], encoding))), 1)
        with self.assertRaises(catalog.CatalogError):
            catalog.read_csv(b"<html>service unavailable</html>")
        with mock.patch.object(catalog, "MAX_ROWS", 1), self.assertRaises(catalog.CatalogError):
            catalog.read_csv(csv_bytes([row("A"), row("B")]))
        with self.assertRaises(catalog.CatalogError):
            catalog.read_csv(csv_bytes([row(**{"NOMBRE INSTALACION": "x" * 4005})]))
        with mock.patch.object(catalog, "MAX_SOURCE_BYTES", 1), self.assertRaises(catalog.CatalogError):
            catalog.read_csv(csv_bytes([row()]))

    def test_missing_or_extra_columns_rejected(self):
        data = csv_bytes([row()], "utf-8").decode()
        with self.assertRaises(catalog.CatalogError):
            catalog.read_csv(data.replace("LATITUD", "LATITUD NUEVA").encode())
        with self.assertRaises(catalog.CatalogError):
            catalog.read_csv((data.rstrip("\r\n") + ";EXTRA\r\n").encode())

    @unittest.skipUnless(RAW_SOURCE.exists(), "Official regression CSV not available on this host")
    def test_public_csv_regression(self):
        payload, quality = catalog.normalize(RAW_SOURCE.read_bytes(), catalog.instant(RETRIEVED))
        self.assertEqual(quality["inputRows"], 42862)
        self.assertEqual(quality["inputSites"], 12074)
        self.assertEqual(quality["stationCount"], 10191)
        self.assertEqual(quality["excluded"], {"explicit_restricted_access": 16, "unknown_or_invalid_hours": 1867})
        self.assertEqual(quality["partialScheduleStations"], 1009)
        self.assertEqual(quality["allDayStations"], 8976)
        stations = {s["id"]: s for s in payload["stations"]}
        for identifier in ("6ADFUZC0MOZAP6TTOJZQ", "2024004250", "2024006553", "2024004259", "RVMU69XW81SW4P8C6WTZ"):
            self.assertIn(identifier, stations)
        self.assertNotIn("RVNAHW5DPBWDKIQECAMO", stations)
        self.assertNotIn("2025000291", stations)
        self.assertIn("2025000282", stations)
        raw = catalog.encode_json(payload)
        blob = catalog.deterministic_gzip(raw)
        self.assertLessEqual(len(raw), catalog.MAX_JSON_BYTES)
        self.assertLessEqual(len(blob), catalog.MAX_COMPRESSED_BYTES)


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source1000 = synthetic_source(1000)
        cls.source2000 = synthetic_source(2000)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".ripree-test-", dir=Path(__file__).resolve().parent)
        self.output = Path(self.temp.name)
        self.now = mock.patch.object(catalog, "utc_now", return_value=NOW)
        self.now.start()

    def tearDown(self):
        self.now.stop()
        self.assertTrue(self.output.resolve().is_relative_to(Path(__file__).resolve().parent))
        self.temp.cleanup()

    def publish(self, source=None, retrieved=RETRIEVED, published=PUBLISHED):
        return catalog.publish(source or self.source1000, self.output, retrieved, published)

    def test_manifest_contract_and_content_integrity(self):
        manifest, quality = self.publish()
        self.assertEqual(set(manifest), catalog.MANIFEST_KEYS)
        self.assertEqual(manifest["source"], catalog.SOURCE_URL)
        self.assertEqual(manifest["schema"], 1)
        blob = (self.output / manifest["file"]).read_bytes()
        raw = gzip.decompress(blob)
        self.assertEqual(manifest["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(manifest["sourceSha256"], hashlib.sha256(self.source1000).hexdigest())
        self.assertEqual(manifest["compressedBytes"], len(blob))
        self.assertEqual(manifest["uncompressedBytes"], len(raw))
        self.assertEqual(manifest["stationCount"], 1000)
        self.assertEqual(catalog.read_previous(self.output), manifest)
        self.assertEqual(json.loads((self.output / "quality-report.json").read_bytes()), quality)
        self.assertEqual(set(json.loads(raw)), {"schema", "source", "stations"})

    def test_same_data_and_row_permutation_keep_blob_hash_and_advance_manifest_only(self):
        first, _ = self.publish()
        newer, _ = self.publish(retrieved="2026-10-20T01:00:00Z", published="2026-10-20T01:01:00Z")
        reversed_source = csv_bytes(reversed(catalog.read_csv(self.source1000)))
        reordered, _ = self.publish(reversed_source, retrieved="2026-10-20T02:00:00Z", published="2026-10-20T02:01:00Z")
        self.assertEqual(first["sha256"], newer["sha256"])
        self.assertEqual(first["sha256"], reordered["sha256"])
        self.assertNotEqual(first["sourceSha256"], reordered["sourceSha256"])
        self.assertEqual(len(list(self.output.glob("catalog-*.bin"))), 1)

    def test_quality_drop_rejected_but_exact_25_percent_allowed(self):
        self.publish(self.source2000)
        before = (self.output / catalog.MANIFEST_FILE).read_bytes()
        with self.assertRaisesRegex(catalog.CatalogError, "25%"):
            self.publish(synthetic_source(1499))
        self.assertEqual((self.output / catalog.MANIFEST_FILE).read_bytes(), before)
        manifest, _ = self.publish(synthetic_source(1500))
        self.assertEqual(manifest["stationCount"], 1500)

    def test_count_guard_keeps_previous(self):
        self.publish()
        before = (self.output / catalog.MANIFEST_FILE).read_bytes()
        with self.assertRaisesRegex(catalog.CatalogError, "Station count"):
            self.publish(synthetic_source(999))
        self.assertEqual((self.output / catalog.MANIFEST_FILE).read_bytes(), before)
        with mock.patch.object(catalog, "MAX_STATIONS", 999), self.assertRaises(catalog.CatalogError):
            self.publish()

    def test_no_date_rollback_or_future_timestamp(self):
        self.publish(retrieved="2026-10-20T01:00:00Z", published="2026-10-20T02:00:00Z")
        before = (self.output / catalog.MANIFEST_FILE).read_bytes()
        for retrieved, published in ((RETRIEVED, PUBLISHED),
                                     ("2026-10-20T01:00:00Z", "2026-10-20T01:30:00Z"),
                                     ("2026-10-22T00:00:00Z", "2026-10-22T00:01:00Z"),
                                     ("2026-10-20T03:00:00Z", PUBLISHED)):
            with self.subTest(retrieved=retrieved, published=published), self.assertRaises(catalog.CatalogError):
                self.publish(retrieved=retrieved, published=published)
            self.assertEqual((self.output / catalog.MANIFEST_FILE).read_bytes(), before)

    def test_manifest_commit_failure_preserves_previous_and_old_blob(self):
        old, _ = self.publish()
        before = (self.output / catalog.MANIFEST_FILE).read_bytes()
        original = catalog.os.replace
        def failing_replace(source, destination):
            if Path(destination).name == catalog.MANIFEST_FILE:
                raise OSError("simulated disk failure")
            return original(source, destination)
        with mock.patch.object(catalog.os, "replace", side_effect=failing_replace), self.assertRaises(OSError):
            self.publish(self.source2000)
        self.assertEqual((self.output / catalog.MANIFEST_FILE).read_bytes(), before)
        self.assertTrue((self.output / old["file"]).exists())
        self.assertFalse((self.output / ".ripree-publish.lock").exists())

    def test_immutable_blob_and_previous_manifest_corruption_fail_closed(self):
        manifest, _ = self.publish()
        before = (self.output / catalog.MANIFEST_FILE).read_bytes()
        (self.output / manifest["file"]).write_bytes(b"corrupt")
        with self.assertRaises(catalog.CatalogError):
            self.publish()
        self.assertEqual((self.output / catalog.MANIFEST_FILE).read_bytes(), before)
        with self.assertRaises(catalog.CatalogError):
            catalog.write_atomic(self.output / manifest["file"], b"different", immutable=True)
        self.assertEqual((self.output / manifest["file"]).read_bytes(), b"corrupt")

    def test_lock_blocks_parallel_publication(self):
        with catalog.publication_lock(self.output):
            with self.assertRaisesRegex(catalog.CatalogError, "Another publication"):
                self.publish()
        self.assertFalse((self.output / catalog.MANIFEST_FILE).exists())

    def test_cli_requires_actual_offline_retrieval_date(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            catalog.main(["--output", str(self.output), "--source", str(self.output / "old.csv")])
        self.assertEqual(error.exception.code, 2)


class BudgetTests(unittest.TestCase):
    def test_json_limits(self):
        for obj, patches in (
                ([[]], {"MAX_DEPTH": 1}),
                ([[], []], {"MAX_CONTAINERS": 2}),
                ({"a": 1}, {"MAX_TOKENS": 3}),
                ("x" * 4001, {}),
                (float("nan"), {}),
                ({1: 2}, {})):
            with self.subTest(obj=str(obj)[:80]), contextlib.ExitStack() as stack:
                for key, value in patches.items():
                    stack.enter_context(mock.patch.object(catalog, key, value))
                with self.assertRaises(catalog.CatalogError):
                    catalog.check_json_tree(obj)
        with self.assertRaises(catalog.CatalogError):
            catalog.encode_json({"a": 1}, maximum=3)
        with mock.patch.object(catalog, "MAX_COMPRESSED_BYTES", 1), self.assertRaises(catalog.CatalogError):
            catalog.deterministic_gzip(b"x")

    def test_read_limit_and_iso_timezone(self):
        with self.assertRaises(catalog.CatalogError):
            catalog.read_bounded(io.BytesIO(b"too long"), 3)
        with self.assertRaises(catalog.CatalogError):
            catalog.instant("2026-10-20T12:00:00")
        self.assertEqual(catalog.iso(catalog.instant("2026-10-20T12:00:00+02:00")), "2026-10-20T10:00:00Z")


class FakeResponse(io.BytesIO):
    status = 200
    url = catalog.DOWNLOAD_URL
    def __init__(self, body=b"official csv", headers=None):
        super().__init__(body)
        self.headers = headers or {}


class DownloadTests(unittest.TestCase):
    def call_with(self, response, **kwargs):
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(catalog.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(catalog, "utc_now", return_value=NOW):
            result = catalog.download_official(**kwargs)
        return result, opener

    def test_official_post_success_records_fetch_completion_only(self):
        result, opener = self.call_with(FakeResponse())
        self.assertEqual(result, (b"official csv", NOW))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, catalog.DOWNLOAD_URL)
        self.assertEqual(request.data, b'{"soloConsolidado":true}')
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 300)

    def test_total_download_deadline_does_not_advance_retrieval_date(self):
        with mock.patch.object(catalog.time, "monotonic", side_effect=[0, 301]), \
                self.assertRaises(catalog.CatalogError):
            self.call_with(FakeResponse())

    def test_size_truncation_empty_and_redirect_guards(self):
        bad_responses = [
            FakeResponse(headers={"Content-Length": str(catalog.MAX_SOURCE_BYTES + 1)}),
            FakeResponse(headers={"Content-Length": "999"}),
            FakeResponse(body=b""),
        ]
        redirect = FakeResponse()
        redirect.url = "https://example.invalid/"
        bad_responses.append(redirect)
        for response in bad_responses:
            with self.subTest(headers=response.headers), self.assertRaises(catalog.CatalogError):
                self.call_with(response)
        with self.assertRaises(catalog.CatalogError):
            catalog.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid/")

    def test_transient_errors_have_bounded_retries(self):
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.HTTPError(catalog.DOWNLOAD_URL, 503, "down", {}, None), FakeResponse()]
        with mock.patch.object(catalog.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(catalog.time, "sleep"):
            data, _ = catalog.download_official()
        self.assertEqual(data, b"official csv")
        self.assertEqual(opener.open.call_count, 2)
        opener.open.reset_mock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with mock.patch.object(catalog.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(catalog.time, "sleep"), self.assertRaises(catalog.CatalogError):
            catalog.download_official()
        self.assertEqual(opener.open.call_count, 2)

    def test_304_is_not_a_fresh_retrieval(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(catalog.DOWNLOAD_URL, 304, "not modified", {}, None)
        with mock.patch.object(catalog.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(catalog, "utc_now") as clock, self.assertRaises(catalog.CatalogError):
            catalog.download_official()
        clock.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
