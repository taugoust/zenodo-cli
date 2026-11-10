"""Sequential, verified draft uploads; inspired by Pranav Durai's Zenodo #2514 comment."""
import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import requests

from .uploader import API, ATTEMPTS, SANDBOX_API, Uploader, UploadError, check_id, check_paths


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--attempts", type=positive_integer, default=ATTEMPTS,
                        help="Maximum attempts per operation, including the first (default: 5)")
    common.add_argument("--replace", action="store_true",
                        help="Replace draft files whose size or checksum differ from the local file")
    common.add_argument("--sandbox", action="store_true",
                        help="Use sandbox.zenodo.org instead of zenodo.org")
    result = argparse.ArgumentParser(prog="zenodo-upload", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True, metavar="COMMAND")
    upload = commands.add_parser("upload", parents=[common],
                                 help="Upload files to an existing unpublished draft")
    upload.add_argument("draft_id", help="Existing unpublished Zenodo draft ID")
    upload.add_argument("files", nargs="+", type=Path)
    new_version = commands.add_parser(
        "new-version", parents=[common],
        help="Create or resume a new-version draft of a published record and upload files to it")
    new_version.add_argument("record_id", help="ID of a published version of the record")
    new_version.add_argument("files", nargs="*", type=Path)
    new_version.add_argument("--version", dest="version_label", metavar="VERSION",
                             help="Set the draft's version metadata, e.g. 1.1")
    new_version.add_argument("--no-import", dest="import_files", action="store_false",
                             help="Don't copy the previous version's files into an empty draft")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        print("ZENODO_TOKEN must be set", file=sys.stderr)
        return 1
    api = SANDBOX_API if args.sandbox else API
    try:
        with requests.Session() as session:
            session.headers["Authorization"] = f"Bearer {token}"
            uploader = Uploader(session, attempts=args.attempts, api=api)
            if args.command == "upload":
                draft_id = args.draft_id
            else:
                # Validate everything local before creating anything remotely.
                check_id(args.record_id, "Record ID")
                check_paths(args.files)
                draft_id = uploader.new_version(args.record_id)
                if args.import_files:
                    uploader.import_files(draft_id)
                if args.version_label is not None:
                    uploader.set_version(draft_id, args.version_label)
            if args.files:
                uploader.upload_files(draft_id, args.files, replace=args.replace)
            uploader.summarize(draft_id)
            if args.command == "new-version":
                print(f"Draft ready for review; publish it at https://{urlsplit(api).netloc}/uploads/{draft_id}")
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
