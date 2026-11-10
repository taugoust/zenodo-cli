"""Sequential, verified draft uploads; inspired by Pranav Durai's Zenodo #2514 comment."""
import hashlib
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlsplit

import requests
from tqdm import tqdm

API = "https://zenodo.org/api"
SANDBOX_API = "https://sandbox.zenodo.org/api"
TIMEOUT = (60, 7200)  # connect and socket read timeouts, not a total deadline
ATTEMPTS = 5
CHUNK = 8 * 1024 * 1024


class UploadError(Exception):
    pass


class TransientError(UploadError):
    pass


class ConflictError(UploadError):
    pass


def check_id(value, label):
    # IDs are interpolated into API paths, so reject anything but digits.
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise UploadError(f"{label} must be a positive integer")
    return value


def check_paths(paths):
    paths = [Path(p) for p in paths]
    if len({p.name for p in paths}) != len(paths):
        raise UploadError("Duplicate input basenames")
    if any(not p.is_file() for p in paths):
        raise UploadError("All inputs must be readable regular files")
    return paths


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
        raise ConflictError("Conflicting file: size or MD5 mismatch; refusing replacement")


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
    def __init__(self, session, attempts=ATTEMPTS, api=API):
        self.session = session
        self.attempts = attempts
        self.api = api.rstrip("/")
        self.host = urlsplit(self.api).netloc

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
        draft_id = check_id(draft_id, "Draft ID")
        data = self.retry(
            lambda: self.request("GET", f"{self.api}/deposit/depositions/{draft_id}"),
            label="Draft API check",
        )
        if not isinstance(data, dict) or data.get("submitted") is not False:
            raise UploadError("Not a confirmed unpublished draft")
        links = data.get("links")
        bucket = links.get("bucket") if isinstance(links, dict) else None
        if not isinstance(bucket, str):
            raise UploadError("Missing bucket URL")
        parsed = urlsplit(bucket)
        if (parsed.scheme != "https" or parsed.netloc != self.host
                or not parsed.path.startswith("/api/files/") or parsed.query or parsed.fragment):
            raise UploadError("Untrusted bucket URL")
        files = data.get("files")
        if not isinstance(files, list):
            raise UploadError("Missing draft file listing")
        if any(not isinstance(item, dict) or not isinstance(item.get("filename"), str)
               for item in files):
            raise UploadError("Malformed draft file listing")
        return bucket.rstrip("/"), files

    def upload(self, draft_id, path, size, digest, replace=False):
        def attempt():
            # Reconcile ambiguous previous PUT outcomes before retrying. Never
            # overwrite an incompletely verified object, or a conflicting one
            # unless replacement was requested.
            bucket, files = self.draft(draft_id)
            matches = [f for f in files if isinstance(f, dict) and f.get("filename") == path.name]
            if len(matches) > 1:
                raise UploadError("Ambiguous remote filename")
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
            if matches:
                try:
                    verify(matches[0], size, digest)
                    print(f"Verified; skipping {path.name}")
                    return
                except ConflictError:
                    if not replace:
                        raise
                print(f"Replacing conflicting {path.name}")
            else:
                headers["If-None-Match"] = "*"
            with path.open("rb") as source, tqdm(total=size, desc=path.name,
                                                 unit="B", unit_scale=True) as progress:
                reader = ProgressReader(source, size, progress)
                # Zenodo stores the whole body before swapping it in, so an
                # interrupted replacement leaves the previous file intact.
                result = self.request("PUT", f"{bucket}/{quote(path.name, safe='')}",
                                      data=reader, headers=headers)
                if reader.count != size or reader.digest.hexdigest() != digest:
                    raise UploadError("Local file changed during upload")
            verify(result, size, digest)
            print(f"Verified upload: {path.name} ({size} bytes, MD5 {digest})")
        self.retry(attempt, label=f"Upload {path.name}")


    def upload_files(self, draft_id, paths, replace=False):
        """Upload files sequentially, checking every existing target before changing anything."""
        paths = check_paths(paths)
        self.draft(draft_id)
        local = [(p, *fingerprint(p)) for p in paths]
        _, remote = self.draft(draft_id)
        for p, size, digest in local:
            for item in remote:
                if isinstance(item, dict) and item.get("filename") == p.name:
                    try:
                        verify(item, size, digest)
                    except ConflictError:
                        if not replace:
                            raise
        for p, size, digest in local:
            self.upload(draft_id, p, size, digest, replace=replace)

