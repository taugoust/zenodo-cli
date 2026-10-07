"""Sequential, verified draft uploads; inspired by Pranav Durai's Zenodo #2514 comment."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlsplit

import requests
from tqdm import tqdm

API = "https://zenodo.org/api"
TIMEOUT = (60, 7200)  # connect and socket read timeouts, not a total deadline
ATTEMPTS = 5
CHUNK = 8 * 1024 * 1024


class UploadError(Exception):
    pass


class TransientError(UploadError):
    pass


def verify(metadata, size, digest):
    if not isinstance(metadata, dict):
        raise UploadError("Missing file verification metadata")
    remote_size = metadata.get("size", metadata.get("filesize"))
    checksum = metadata.get("checksum")
    if type(remote_size) is not int or not isinstance(checksum, str):
        raise UploadError("Missing or malformed size/checksum")
    checksum = checksum.removeprefix("md5:").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", checksum):
        raise UploadError("Missing or malformed MD5 checksum")
    if remote_size != size or checksum != digest:
        raise UploadError("Conflicting file: size or MD5 mismatch; refusing replacement")


def fingerprint(path):
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as source, tqdm(desc=f"Hashing {path.name}", total=path.stat().st_size,
                                        unit="B", unit_scale=True) as progress:
        while chunk := source.read(CHUNK):
            size += len(chunk)
            digest.update(chunk)
            progress.update(len(chunk))
    return size, digest.hexdigest()


class ProgressReader:
    def __init__(self, source, size, progress):
        self.source, self.size, self.progress = source, size, progress
        self.digest = hashlib.md5()
        self.count = 0

    def __len__(self):
        return self.size

    def read(self, size=-1):
        chunk = self.source.read(CHUNK if size < 0 else min(size, CHUNK))
        self.digest.update(chunk)
        self.count += len(chunk)
        self.progress.update(len(chunk))
        return chunk


class Uploader:
    def __init__(self, session, attempts=ATTEMPTS):
        self.session = session
        self.attempts = attempts

    def request(self, method, url, **kwargs):
        try:
            with self.session.request(method, url, timeout=TIMEOUT,
                                      allow_redirects=False, **kwargs) as response:
                status = response.status_code
                if status in (408, 429) or 500 <= status < 600:
                    raise TransientError(f"HTTP {status}")
                if not 200 <= status < 300:
                    raise UploadError(f"HTTP {status}; request refused")
                try:
                    return response.json()
                except ValueError:
                    raise UploadError("Invalid JSON response") from None
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError):
            # Never echo exception text, URLs, response bodies, or credentials.
            raise TransientError("Connection interrupted or timed out") from None
        except requests.RequestException:
            raise UploadError("HTTP transport failure") from None

    def retry(self, operation, label):
        for attempt in range(self.attempts):
            try:
                return operation()
            except TransientError:
                if attempt == self.attempts - 1:
                    raise UploadError(f"{label}: transient failures exhausted {self.attempts} attempts") from None
                delay = min(10 * 2 ** min(attempt, 3), 60)
                print(f"{label}: transient failure; retry {attempt + 2}/{self.attempts} in {delay}s",
                      file=sys.stderr)
                time.sleep(delay)

    def draft(self, draft_id):
        data = self.retry(
            lambda: self.request("GET", f"{API}/deposit/depositions/{draft_id}"),
            label="Draft API check",
        )
        if not isinstance(data, dict) or data.get("submitted") is not False:
            raise UploadError("Not a confirmed unpublished draft")
        bucket = data.get("links", {}).get("bucket")
        if not isinstance(bucket, str):
            raise UploadError("Missing bucket URL")
        parsed = urlsplit(bucket)
        if (parsed.scheme != "https" or parsed.netloc != "zenodo.org"
                or not parsed.path.startswith("/api/files/") or parsed.query or parsed.fragment):
            raise UploadError("Untrusted bucket URL")
        files = data.get("files")
        if not isinstance(files, list):
            raise UploadError("Missing draft file listing")
        if any(not isinstance(item, dict) or not isinstance(item.get("filename"), str)
               for item in files):
            raise UploadError("Malformed draft file listing")
        return bucket.rstrip("/"), files

    def upload(self, draft_id, path, size, digest):
        def attempt():
            # Reconcile ambiguous previous PUT outcomes before retrying. Never
            # overwrite a visible conflicting or incompletely verified object.
            bucket, files = self.draft(draft_id)
            matches = [f for f in files if isinstance(f, dict) and f.get("filename") == path.name]
            if len(matches) > 1:
                raise UploadError("Ambiguous remote filename")
            if matches:
                verify(matches[0], size, digest)
                print(f"Verified; skipping {path.name}")
                return
            with path.open("rb") as source, tqdm(total=size, desc=path.name,
                                                 unit="B", unit_scale=True) as progress:
                reader = ProgressReader(source, size, progress)
                result = self.request("PUT", f"{bucket}/{quote(path.name, safe='')}",
                                      data=reader, headers={"Content-Type": "application/octet-stream",
                                                            "Content-Length": str(size),
                                                            "If-None-Match": "*"})
                if reader.count != size or reader.digest.hexdigest() != digest:
                    raise UploadError("Local file changed during upload")
            verify(result, size, digest)
            print(f"Verified upload: {path.name} ({size} bytes, MD5 {digest})")
        self.retry(attempt, label=f"Upload {path.name}")


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft_id", help="Existing unpublished Zenodo draft ID")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--attempts", type=positive_integer, default=ATTEMPTS,
                        help="Maximum attempts per operation, including the first (default: 5)")
    args = parser.parse_args(argv)
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        print("ZENODO_TOKEN must be set", file=sys.stderr)
        return 1
    try:
        if not re.fullmatch(r"[1-9][0-9]*", args.draft_id):
            raise UploadError("Draft ID must be a positive integer")
        if len({p.name for p in args.files}) != len(args.files):
            raise UploadError("Duplicate input basenames")
        if any(not p.is_file() for p in args.files):
            raise UploadError("All inputs must be readable regular files")
        with requests.Session() as session:
            session.headers["Authorization"] = f"Bearer {token}"
            uploader = Uploader(session, attempts=args.attempts)
            uploader.draft(args.draft_id)
            local = [(p, *fingerprint(p)) for p in args.files]
            # Check all existing targets before making any changes.
            _, remote = uploader.draft(args.draft_id)
            for p, size, digest in local:
                for item in remote:
                    if isinstance(item, dict) and item.get("filename") == p.name:
                        verify(item, size, digest)
            for p, size, digest in local:
                uploader.upload(args.draft_id, p, size, digest)
    except UploadError as error:
        print(f"Error: {error}".replace(token, "[REDACTED]"), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, AttributeError):
        print("Error: local I/O or malformed API data", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
