"""Sequential, verified draft uploads; inspired by Pranav Durai's Zenodo #2514 comment."""
import argparse
import os
from pathlib import Path
import sys

import requests

from .uploader import API, ATTEMPTS, SANDBOX_API, Uploader, UploadError


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(prog="zenodo-upload", description=__doc__)
    parser.add_argument("draft_id", help="Existing unpublished Zenodo draft ID")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--attempts", type=positive_integer, default=ATTEMPTS,
                        help="Maximum attempts per operation, including the first (default: 5)")
    parser.add_argument("--replace", action="store_true",
                        help="Replace draft files whose size or checksum differ from the local file")
    parser.add_argument("--sandbox", action="store_true",
                        help="Use sandbox.zenodo.org instead of zenodo.org")
    args = parser.parse_args(argv)
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        print("ZENODO_TOKEN must be set", file=sys.stderr)
        return 1
    api = SANDBOX_API if args.sandbox else API
    try:
        with requests.Session() as session:
            session.headers["Authorization"] = f"Bearer {token}"
            uploader = Uploader(session, attempts=args.attempts, api=api)
            uploader.upload_files(args.draft_id, args.files, replace=args.replace)
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
