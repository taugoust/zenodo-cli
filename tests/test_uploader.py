import contextlib
import copy
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from zenodo_upload import cli, uploader as z

PAYLOAD = b"archive contents"
MD5 = hashlib.md5(PAYLOAD).hexdigest()
META = {"size": len(PAYLOAD), "checksum": "md5:" + MD5}
DRAFT = {"submitted": False, "links": {"bucket": "https://zenodo.org/api/files/bucket"}, "files": []}


def response(status=200, body=None):
    result = Mock(status_code=status)
    result.json.return_value = body
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "file #1.tar.gz"
        self.path.write_bytes(PAYLOAD)
        self.session = Mock(headers={})
        self.uploader = z.Uploader(self.session)
        self.sleep = patch.object(z.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def serve(self, outcomes):
        self.bodies = []
        def request(method, url, **kwargs):
            self.assertEqual(kwargs["timeout"], (60, 7200))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertIn(method, ("GET", "PUT"))
            if method == "GET":
                return response(body=DRAFT)
            self.assertTrue(url.endswith("file%20%231.tar.gz"))
            self.assertEqual(kwargs["headers"]["If-None-Match"], "*")
            reader = kwargs["data"]
            self.assertEqual(len(reader), len(PAYLOAD))
            self.bodies.append(b"".join(iter(lambda: reader.read(3), b"")))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.session.request.side_effect = request

    def upload(self):
        self.uploader.upload("123", self.path, len(PAYLOAD), MD5)

    def test_hash(self):
        self.assertEqual(z.fingerprint(self.path), (len(PAYLOAD), MD5))

    def test_success_streamed(self):
        self.serve([response(body=META)])
        self.upload()
        self.assertEqual(self.bodies, [PAYLOAD])

    def test_retries_restart(self):
        self.serve([requests.ConnectionError("secret"), response(503), response(body=META)])
        self.upload()
        self.assertEqual(self.bodies, [PAYLOAD] * 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [10, 20])

    def test_exhaustion(self):
        self.serve([response(500) for _ in range(z.ATTEMPTS)])
        with self.assertRaises(z.UploadError):
            self.upload()
        self.assertEqual(len(self.bodies), z.ATTEMPTS)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [10, 20, 40, 60])

    def test_permanent_errors_no_retry(self):
        for status in (301, 400, 401, 403, 404, 409, 412):
            with self.subTest(status=status):
                self.serve([response(status)])
                with self.assertRaisesRegex(z.UploadError, f"HTTP {status}; request refused"):
                    self.upload()
                self.assertEqual(len(self.bodies), 1)
        self.sleep.assert_not_called()

    def test_verification_fail_closed(self):
        malformed = "Missing or malformed size/checksum"
        for data, reason in ((None, "Missing file verification metadata"), ({}, malformed),
                             ({"size": len(PAYLOAD)}, malformed), ({"checksum": MD5}, malformed),
                             ({**META, "size": len(PAYLOAD) + 1}, "Conflicting file"),
                             ({**META, "size": str(len(PAYLOAD))}, malformed),
                             ({**META, "checksum": "md5:" + "0" * 32}, "Conflicting file"),
                             ({**META, "checksum": "sha256:" + MD5}, "malformed MD5 checksum")):
            with self.subTest(data=data):
                self.serve([response(body=data)])
                with self.assertRaisesRegex(z.UploadError, reason):
                    self.upload()
                self.assertEqual(len(self.bodies), 1)

    def test_existing_verified_skip_and_conflict(self):
        for meta, reason in ((META, None), ({**META, "checksum": "0" * 32}, "Conflicting file"),
                             ({}, "Missing or malformed size/checksum")):
            with self.subTest(meta=meta):
                self.session.request.reset_mock(side_effect=True)
                self.session.request.return_value = response(body={**DRAFT, "files": [
                    {"filename": self.path.name, "filesize": meta.get("size"), "checksum": meta.get("checksum")}
                ]})
                if reason is None:
                    self.upload()
                else:
                    with self.assertRaisesRegex(z.UploadError, reason):
                        self.upload()
                self.assertEqual([c.args[0] for c in self.session.request.call_args_list], ["GET"])

    def test_lost_response_reconciled(self):
        self.session.request.side_effect = [response(body=DRAFT), requests.Timeout(),
            response(body={**DRAFT, "files": [{"filename": self.path.name, **META}]})]
        self.upload()
        self.assertEqual([c.args[0] for c in self.session.request.call_args_list], ["GET", "PUT", "GET"])

    def test_local_change(self):
        self.path.write_bytes(b"changed contents")
        self.serve([response(body=META)])
        with self.assertRaisesRegex(z.UploadError, "Local file changed"):
            self.upload()

    def test_untrusted_or_published_draft(self):
        unpublished, missing = "Not a confirmed unpublished draft", "Missing bucket URL"
        cases = [({**DRAFT, "submitted": True}, unpublished), ({**DRAFT, "submitted": None}, unpublished),
                 ({k: v for k, v in DRAFT.items() if k != "submitted"}, unpublished),
                 ({**DRAFT, "links": {}}, missing), ({**DRAFT, "links": {"bucket": None}}, missing),
                 ({**DRAFT, "links": "https://zenodo.org/api/files/x"}, missing), ({**DRAFT, "links": None}, missing),
                 ({**DRAFT, "files": None}, "Missing draft file listing"),
                 ({**DRAFT, "files": [{"filename": 1}]}, "Malformed draft file listing")]
        for bucket in ("https://evil.example/api/files/x", "http://zenodo.org/api/files/x",
                       "https://zenodo.org.evil.example/api/files/x", "https://evil.example/zenodo.org/api/files/x",
                       "https://user@zenodo.org/api/files/x", "https://zenodo.org:8443/api/files/x",
                       "https://zenodo.org/api/records/x", "https://zenodo.org/api/files/x?y=1",
                       "https://zenodo.org/api/files/x#y"):
            cases.append(({**DRAFT, "links": {"bucket": bucket}}, "Untrusted bucket URL"))
        for draft, reason in cases:
            with self.subTest(draft=draft):
                self.session.request.return_value = response(body=draft)
                with self.assertRaisesRegex(z.UploadError, reason):
                    self.uploader.draft("123")
        self.session.request.return_value = response(body={**DRAFT, "links": {"bucket": "https://zenodo.org/api/files/b/"}})
        self.assertEqual(self.uploader.draft("123"), ("https://zenodo.org/api/files/b", []))

    def test_invalid_draft_id_rejected_before_request(self):
        for draft_id in ("0", "007", "abc", "", "12 ", "1/../2", "../x", "1?x=y", "-5", None):
            with self.subTest(draft_id=draft_id):
                with self.assertRaisesRegex(z.UploadError, "Draft ID must be a positive integer"):
                    self.uploader.draft(draft_id)
                with self.assertRaisesRegex(z.UploadError, "Draft ID must be a positive integer"):
                    self.uploader.upload_files(draft_id, [self.path])
        self.session.request.assert_not_called()
        self.session.request.return_value = response(body=DRAFT)
        self.uploader.draft(123)
        self.assertTrue(self.session.request.call_args.args[1].endswith("/deposit/depositions/123"))

    def test_invalid_local_inputs_rejected_before_request(self):
        twin = Path(self.tmp.name) / "sub" / self.path.name
        twin.parent.mkdir()
        twin.write_bytes(PAYLOAD)
        for paths, reason in (([self.path, twin], "Duplicate input basenames"),
                              ([self.path, self.path], "Duplicate input basenames"),
                              ([self.path, twin.parent], "readable regular files"),
                              ([self.path, Path(self.tmp.name) / "missing"], "readable regular files")):
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(z.UploadError, reason):
                    self.uploader.upload_files("123", paths)
                output = io.StringIO()
                with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), contextlib.redirect_stderr(output), \
                     patch.object(cli.requests, "Session") as session:
                    session.return_value.__enter__.return_value = self.session
                    self.assertEqual(cli.main(["123", *map(str, paths)]), 1)
                self.assertIn(reason, output.getvalue())
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), contextlib.redirect_stderr(output), \
             patch.object(cli.requests, "Session") as session:
            session.return_value.__enter__.return_value = self.session
            self.assertEqual(cli.main(["../x", str(self.path)]), 1)
        self.assertIn("Draft ID must be a positive integer", output.getvalue())
        self.session.request.assert_not_called()

    def test_preflight_conflict_blocks_all_uploads(self):
        # The first file is new and would upload fine; the conflict on the second
        # must stop the run before any PUT, not after the first upload.
        other = self.path.with_name("second.tar.gz")
        other.write_bytes(PAYLOAD)
        self.session.request.return_value = response(body={**DRAFT, "files": [
            {"filename": other.name, **META, "checksum": "0" * 32}]})
        with self.assertRaisesRegex(z.UploadError, "Conflicting file"):
            self.uploader.upload_files("123", [self.path, other])
        self.assertEqual({c.args[0] for c in self.session.request.call_args_list}, {"GET"})

    def test_preflight_verified_existing_file_uploads_rest(self):
        other = self.path.with_name("second.tar.gz")
        other.write_bytes(PAYLOAD)
        existing = response(body={**DRAFT, "files": [{"filename": self.path.name, **META}]})
        def request(method, url, **kwargs):
            if method == "GET":
                return existing
            self.assertEqual(kwargs["data"].read(), PAYLOAD)
            return response(body=META)
        self.session.request.side_effect = request
        self.uploader.upload_files("123", [self.path, other])
        puts = [c.args[1] for c in self.session.request.call_args_list if c.args[0] == "PUT"]
        self.assertEqual(puts, ["https://zenodo.org/api/files/bucket/second.tar.gz"])

    def test_nested_retry_messages_identify_operation(self):
        self.session.request.side_effect = [
            response(body=DRAFT), response(503),
            response(503), response(503),
            response(body={**DRAFT, "files": [{"filename": self.path.name, **META}]}),
        ]
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.upload()
        log = output.getvalue()
        self.assertIn(f"Upload {self.path.name}: transient failure; retry 2/5", log)
        self.assertIn("Draft API check: transient failure; retry 2/5", log)
        self.assertIn("Draft API check: transient failure; retry 3/5", log)

    def test_exhaustion_identifies_operation(self):
        self.uploader = z.Uploader(self.session, attempts=1)
        self.session.request.return_value = response(503)
        with self.assertRaisesRegex(z.UploadError, "Draft API check: transient failures exhausted 1 attempts"):
            self.uploader.draft("123")
        self.serve([response(503)])
        with self.assertRaises(z.UploadError) as error:
            self.upload()
        self.assertIn(f"Upload {self.path.name}:", str(error.exception))

    def test_get_retries_and_invalid_json(self):
        self.session.request.side_effect = [response(429), response(body=DRAFT)]
        self.uploader.draft("123")
        bad = response()
        bad.json.side_effect = ValueError("secret")
        self.session.request.side_effect = [bad]
        with self.assertRaisesRegex(z.UploadError, "Invalid JSON") as error:
            self.uploader.draft("123")
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def test_transport_errors_never_echoed(self):
        secret = "https://zenodo.org/api?access_token=sensitive-token"
        for failure in (requests.ConnectionError(secret), requests.Timeout(secret),
                        requests.exceptions.ChunkedEncodingError(secret),
                        requests.exceptions.InvalidURL(secret)):
            with self.subTest(failure=type(failure).__name__):
                self.session.request.reset_mock()
                self.session.request.side_effect = failure
                output = io.StringIO()
                with contextlib.redirect_stderr(output), self.assertRaises(z.UploadError) as error:
                    self.uploader.draft("123")
                self.assertNotIn("sensitive-token", output.getvalue() + str(error.exception))
                self.assertIsNone(error.exception.__cause__)
                self.assertTrue(error.exception.__suppress_context__)
                # retry() replaces transient errors, so check the raw request layer too.
                with self.assertRaises(z.UploadError) as error:
                    self.uploader.request("GET", "https://zenodo.org/api")
                self.assertNotIn("sensitive-token", str(error.exception))
                self.assertIsNone(error.exception.__cause__)
                self.assertTrue(error.exception.__suppress_context__)

    def test_cli_end_to_end_sets_auth_and_hides_transport_errors(self):
        self.session.request.side_effect = requests.ConnectionError("leak sensitive-token secret")
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "sensitive-token"}), contextlib.redirect_stderr(output), \
             patch.object(cli.requests, "Session") as session:
            session.return_value.__enter__.return_value = self.session
            self.assertEqual(cli.main(["--attempts", "2", "123", str(self.path)]), 1)
        self.assertEqual(self.session.headers, {"Authorization": "Bearer sensitive-token"})
        self.assertEqual(self.session.request.call_count, 2)
        log = output.getvalue()
        self.assertIn("Draft API check: transient failures exhausted 2 attempts", log)
        self.assertNotIn("sensitive-token", log)
        self.assertNotIn("secret", log)

    def test_cli_failure_and_secret_redaction(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cli.requests, "Session") as session:
            self.assertEqual(cli.main(["123", str(self.path)]), 1)
            session.assert_not_called()
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "sensitive-token"}), \
             patch.object(z.Uploader, "draft", side_effect=z.UploadError("sensitive-token")), \
             contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(["123", str(self.path)]), 1)
        self.assertNotIn("sensitive-token", output.getvalue())

    def test_custom_attempts(self):
        self.uploader = z.Uploader(self.session, attempts=7)
        self.serve([response(503) for _ in range(7)])
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(z.UploadError):
            self.upload()
        self.assertEqual(len(self.bodies), 7)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list],
                         [10, 20, 40, 60, 60, 60])
        self.assertIn("retry 7/7", output.getvalue())

    def test_one_attempt_no_retry(self):
        self.uploader = z.Uploader(self.session, attempts=1)
        self.serve([response(503)])
        with self.assertRaises(z.UploadError):
            self.upload()
        self.assertEqual(len(self.bodies), 1)
        self.sleep.assert_not_called()

    def test_attempts_cli(self):
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), \
             patch.object(cli, "Uploader") as uploader:
            uploader.return_value.draft.return_value = ("bucket", [])
            self.assertEqual(cli.main(["--attempts", "10", "123", str(self.path)]), 0)
            self.assertEqual(uploader.call_args.kwargs["attempts"], 10)
            self.assertEqual(cli.main(["123", str(self.path)]), 0)
            self.assertEqual(uploader.call_args.kwargs["attempts"], 5)

    def test_invalid_attempts(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                 patch.object(cli.requests, "Session") as session, \
                 self.assertRaises(SystemExit) as error:
                cli.main(["--attempts", value, "123", str(self.path)])
            self.assertEqual(error.exception.code, 2)
            session.assert_not_called()

    def test_chunked_encoding_retried_other_transport_errors_not(self):
        self.serve([requests.exceptions.ChunkedEncodingError(), response(body=META)])
        self.upload()
        self.assertEqual(self.bodies, [PAYLOAD] * 2)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [10])
        self.sleep.reset_mock()
        self.serve([requests.exceptions.InvalidURL("secret")])
        with self.assertRaisesRegex(z.UploadError, "^HTTP transport failure$"):
            self.upload()
        self.assertEqual(len(self.bodies), 1)
        self.sleep.assert_not_called()

    def run_cli(self, *argv):
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), contextlib.redirect_stderr(output), \
             patch.object(cli.requests, "Session") as session:
            session.return_value.__enter__.return_value = self.session
            status = cli.main([*argv])
        return status, output.getvalue()

    def test_cli_interrupt(self):
        # Interrupts must stop the run whether they land in a request or a retry sleep.
        for target in ("request", "sleep"):
            with self.subTest(target=target):
                self.session.request.reset_mock(side_effect=True)
                self.sleep.reset_mock(side_effect=True)
                if target == "request":
                    self.session.request.side_effect = KeyboardInterrupt
                else:
                    self.session.request.return_value = response(503)
                    self.sleep.side_effect = KeyboardInterrupt
                status, log = self.run_cli("123", str(self.path))
                self.assertEqual(status, 130)
                self.assertEqual(log.splitlines()[-1], "Interrupted")
                self.assertEqual(self.session.request.call_count, 1)

    def test_cli_local_io_and_malformed_api_errors_generic(self):
        generic = "Error: local I/O or malformed API data\n"
        with patch.object(z, "fingerprint", side_effect=PermissionError(13, "denied", "/secret/path")):
            status, log = self.run_cli("123", str(self.path))
        self.assertEqual((status, log), (1, generic))
        self.session.request.return_value = response(body=["not", "a", "draft"])
        status, log = self.run_cli("123", str(self.path))
        self.assertEqual((status, log), (1, "Error: Not a confirmed unpublished draft\n"))
        self.session.request.return_value = response(body={**DRAFT, "files": [{"filename": self.path.name, **META}]})
        with patch.object(z, "verify", side_effect=TypeError("secret")):
            status, log = self.run_cli("123", str(self.path))
        self.assertEqual(status, 1)
        self.assertTrue(log.endswith(generic))
        self.assertNotIn("secret", log)

    def test_multiple_files_sequential(self):
        other = self.path.with_name("second.tar.gz")
        other.write_bytes(PAYLOAD)
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), \
             patch.object(z.Uploader, "draft", return_value=(DRAFT["links"]["bucket"], [])), \
             patch.object(z.Uploader, "upload") as upload:
            self.assertEqual(cli.main(["123", str(self.path), str(other)]), 0)
            self.assertEqual([c.args[1] for c in upload.call_args_list], [self.path, other])


class FakeZenodo:
    """Stateful stand-in for the Zenodo draft, record and bucket endpoints."""

    def __init__(self, test, api="https://zenodo.org/api"):
        self.test, self.api = test, api
        self.bucket = api.replace("/api", "/api/files") + "/bucket456"
        self.files = []
        self.calls = []
        self.fail = {}  # (method, path) -> list of outcomes to apply before handling

    def __call__(self, method, url, **kwargs):
        self.test.assertTrue(url.startswith(self.api + "/"), url)
        self.test.assertFalse(kwargs["allow_redirects"])
        path = url[len(self.api):]
        self.calls.append((method, path))
        outcomes = self.fail.get((method, path))
        if outcomes:
            outcome = outcomes.pop(0)
            if outcome == "lost":
                # The server applies the request, but the response never arrives.
                self.handle(method, path, kwargs)
                raise requests.Timeout("lost")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return self.handle(method, path, kwargs)

    def handle(self, method, path, kwargs):
        if (method, path) == ("GET", "/deposit/depositions/456"):
            return response(body={"submitted": False, "links": {"bucket": self.bucket},
                                  "files": copy.deepcopy(self.files)})
        if method == "PUT" and path.startswith("/files/bucket456/"):
            name = requests.utils.unquote(path.rsplit("/", 1)[1])
            body = kwargs["data"].read()
            self.files = [f for f in self.files if f["filename"] != name]
            entry = {"filename": name, "filesize": len(body), "checksum": hashlib.md5(body).hexdigest()}
            self.files.append(entry)
            return response(201, {"size": len(body), "checksum": "md5:" + entry["checksum"]})
        raise AssertionError(f"unexpected request {method} {path}")


class FakeZenodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "missing.tar.gz"
        self.path.write_bytes(PAYLOAD)
        self.zenodo = FakeZenodo(self)
        self.session = Mock(headers={})
        self.session.request.side_effect = self.zenodo
        self.uploader = z.Uploader(self.session)
        self.sleep = patch.object(z.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def run_cli(self, *argv):
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), contextlib.redirect_stderr(output), \
             contextlib.redirect_stdout(output), patch.object(cli.requests, "Session") as session:
            session.return_value.__enter__.return_value = self.session
            status = cli.main([*argv])
        return status, output.getvalue()


class ReplaceAndSandboxTests(FakeZenodoTests):
    def test_replace_conflicting_file(self):
        self.zenodo.files = [{"filename": self.path.name, "filesize": 3, "checksum": "b" * 32}]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(z.ConflictError):
            self.uploader.upload_files("456", [self.path])
        self.assertNotIn("PUT", [c[0] for c in self.zenodo.calls])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.uploader.upload_files("456", [self.path], replace=True)
        self.assertIn(f"Replacing conflicting {self.path.name}", output.getvalue())
        put = next(c for c in self.session.request.call_args_list if c.args[0] == "PUT")
        self.assertNotIn("If-None-Match", put.kwargs["headers"])
        self.assertEqual(self.zenodo.files, [{"filename": self.path.name, "filesize": len(PAYLOAD),
                                              "checksum": MD5}])

    def test_replace_keeps_matching_and_refuses_malformed(self):
        self.zenodo.files = [{"filename": self.path.name, "filesize": len(PAYLOAD), "checksum": MD5}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.uploader.upload_files("456", [self.path], replace=True)
        self.assertNotIn("PUT", [c[0] for c in self.zenodo.calls])
        self.zenodo.files = [{"filename": self.path.name, "filesize": len(PAYLOAD)}]
        with contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(z.UploadError, "Missing or malformed size/checksum"):
            self.uploader.upload_files("456", [self.path], replace=True)
        self.assertNotIn("PUT", [c[0] for c in self.zenodo.calls])

    def test_replace_lost_response_reconciled(self):
        self.zenodo.files = [{"filename": self.path.name, "filesize": 3, "checksum": "b" * 32}]
        self.zenodo.fail[("PUT", f"/files/bucket456/{self.path.name}")] = ["lost"]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.uploader.upload_files("456", [self.path], replace=True)
        self.assertEqual([c for c in self.zenodo.calls if c[0] == "PUT"],
                         [("PUT", f"/files/bucket456/{self.path.name}")])

    def test_sandbox(self):
        self.zenodo = FakeZenodo(self, api="https://sandbox.zenodo.org/api")
        self.session.request.side_effect = self.zenodo
        status, log = self.run_cli("--sandbox", "456", str(self.path))
        self.assertEqual(status, 0, log)
        self.assertEqual([c[0] for c in self.zenodo.calls if c[0] != "GET"], ["PUT"])
        # A production uploader must not trust a sandbox bucket, and vice versa.
        session = Mock()
        for api, bucket in ((z.API, "https://sandbox.zenodo.org/api/files/b"),
                            (z.SANDBOX_API, "https://zenodo.org/api/files/b")):
            with self.subTest(api=api):
                session.request.return_value = response(body={**DRAFT, "links": {"bucket": bucket}})
                with self.assertRaisesRegex(z.UploadError, "Untrusted bucket URL"):
                    z.Uploader(session, api=api).draft("456")
                self.assertTrue(session.request.call_args.args[1].startswith(api + "/"))


if __name__ == "__main__":
    unittest.main()
