import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import zenodo_upload as z

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
        self.session = Mock()
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
                with self.assertRaises(z.UploadError):
                    self.upload()
                self.assertEqual(len(self.bodies), 1)
        self.sleep.assert_not_called()

    def test_verification_fail_closed(self):
        for data in (None, {}, {"size": len(PAYLOAD)}, {"checksum": MD5},
                     {**META, "size": len(PAYLOAD) + 1}, {**META, "size": str(len(PAYLOAD))},
                     {**META, "checksum": "md5:" + "0" * 32}, {**META, "checksum": "sha256:" + MD5}):
            with self.subTest(data=data):
                self.serve([response(body=data)])
                with self.assertRaises(z.UploadError):
                    self.upload()
                self.assertEqual(len(self.bodies), 1)

    def test_existing_verified_skip_and_conflict(self):
        for meta, succeeds in ((META, True), ({**META, "checksum": "0" * 32}, False), ({}, False)):
            self.session.request.reset_mock(side_effect=True)
            self.session.request.return_value = response(body={**DRAFT, "files": [
                {"filename": self.path.name, "filesize": meta.get("size"), "checksum": meta.get("checksum")}
            ]})
            if succeeds:
                self.upload()
            else:
                with self.assertRaises(z.UploadError):
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
        for draft in ({**DRAFT, "submitted": True}, {**DRAFT, "submitted": None},
                      {**DRAFT, "links": {"bucket": "https://evil.example/api/files/x"}},
                      {**DRAFT, "files": None}):
            self.session.request.return_value = response(body=draft)
            with self.assertRaises(z.UploadError):
                self.uploader.draft("123")

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
        with self.assertRaisesRegex(z.UploadError, "Invalid JSON"):
            self.uploader.draft("123")

    def test_cli_failure_and_secret_redaction(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(z.requests, "Session") as session:
            self.assertEqual(z.main(["123", str(self.path)]), 1)
            session.assert_not_called()
        output = io.StringIO()
        with patch.dict(os.environ, {"ZENODO_TOKEN": "sensitive-token"}), \
             patch.object(z.Uploader, "draft", side_effect=z.UploadError("sensitive-token")), \
             contextlib.redirect_stderr(output):
            self.assertEqual(z.main(["123", str(self.path)]), 1)
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
             patch.object(z, "Uploader") as uploader:
            uploader.return_value.draft.return_value = ("bucket", [])
            self.assertEqual(z.main(["--attempts", "10", "123", str(self.path)]), 0)
            self.assertEqual(uploader.call_args.kwargs["attempts"], 10)
            self.assertEqual(z.main(["123", str(self.path)]), 0)
            self.assertEqual(uploader.call_args.kwargs["attempts"], 5)

    def test_invalid_attempts(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                 patch.object(z.requests, "Session") as session, \
                 self.assertRaises(SystemExit) as error:
                z.main(["--attempts", value, "123", str(self.path)])
            self.assertEqual(error.exception.code, 2)
            session.assert_not_called()

    def test_multiple_files_sequential(self):
        other = self.path.with_name("second.tar.gz")
        other.write_bytes(PAYLOAD)
        with patch.dict(os.environ, {"ZENODO_TOKEN": "test"}), \
             patch.object(z.Uploader, "draft", return_value=(DRAFT["links"]["bucket"], [])), \
             patch.object(z.Uploader, "upload") as upload:
            self.assertEqual(z.main(["123", str(self.path), str(other)]), 0)
            self.assertEqual([c.args[1] for c in upload.call_args_list], [self.path, other])


if __name__ == "__main__":
    unittest.main()
