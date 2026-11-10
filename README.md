# Zenodo draft uploader

Upload large files to a Zenodo draft from the terminal, or prepare a new version of a published
record. Based on [Pranav Durai's
script](https://github.com/zenodo/zenodo/issues/2514#issuecomment-4956445997).

The script uploads files one at a time and checks their size and MD5 checksum. It never publishes
anything: you review the draft and publish it in Zenodo.

## Usage

You'll need Nix and a [Zenodo token](https://zenodo.org/account/settings/applications/tokens/new/)
with `deposit:write` permission. Set `ZENODO_TOKEN` in your environment, then pass the ID of an
existing draft (the number in its URL) and the file paths:

```sh
nix run . -- upload 12345678 first.tar.gz second.tar.gz
```

The token is read only from `ZENODO_TOKEN`, not from a command-line option. To enter it without
saving it in your shell history, use this in zsh:

```sh
read -rs 'ZENODO_TOKEN?Zenodo token: '
echo
export ZENODO_TOKEN
```

Only the files you name are uploaded. Local files are never changed. Each file keeps its filename,
so you can't upload two files with the same name in one run.

Add `--sandbox` to any command to use [sandbox.zenodo.org](https://sandbox.zenodo.org) instead. It
needs a separate account and token, and is a safe place to try things out.

## New versions

To add or update files of a published record, create a new version from it:

```sh
nix run . -- new-version 12345678 missing-part.tar.gz --version 1.1
```

The ID is that of any published version of the record. This creates a new-version draft and copies
the previous version's files into it, sets the version metadata if `--version` is given, and
uploads the named files. It then prints the draft's URL: check the draft there and publish it.

Running the command again resumes the same draft instead of creating another. Files are only
copied into a draft that has none, and `--no-import` skips copying entirely. Other metadata is left
as it was, so update it in Zenodo before publishing if needed. You need to own or manage the record,
and Zenodo doesn't allow new versions of records with an external DOI.

## Retries and verification

Progress is shown while hashing and uploading. An upload counts as successful only when Zenodo
returns a matching size and MD5 checksum. Missing verification data is an error.

Files already in the draft are checked before being skipped. If a file doesn't match, the script
stops before uploading anything, unless you pass `--replace` to upload over it. Zenodo only swaps in
a replacement once it has been fully received, so an interrupted one keeps the previous file. Don't
edit the local files, or change the same draft from another process or in the browser, while it's
running.

Connection failures, timeouts, HTTP 408/429 responses, and server errors are retried up to five
attempts per operation by default. Use `--attempts` to change that, including the first try:

```sh
nix run . -- upload --attempts 10 12345678 first.tar.gz second.tar.gz
```

The limit applies separately to each file upload and each draft API call. `--attempts 1` disables
retries. Delays start at 10 seconds, then 20 and 40, and stay at 60 seconds for further retries.
Connection and read timeouts are 60 seconds and two hours respectively, not a limit on the total
upload time.

You can stop with Ctrl+C and run the same command again. Completed files are verified and skipped.
Interrupted uploads start over from the beginning. If an attempt left an incomplete or conflicting
file in the draft, inspect it in Zenodo and remove it before retrying.

Failures return a nonzero exit status. Files uploaded earlier stay in the draft, even if a later
upload fails.

## Development

The code is a small Python package: the upload logic lives in `src/zenodo_upload/uploader.py` and
the command-line interface in `src/zenodo_upload/cli.py`. Python, `requests`, and `tqdm` come from
the pinned Nix environment, which puts `src/` on `PYTHONPATH`.

```sh
nix develop . --command python -m zenodo_upload upload 12345678 first.tar.gz second.tar.gz
```

You can also use it as a library:

```python
import requests
from zenodo_upload import Uploader

with requests.Session() as session:
    session.headers["Authorization"] = f"Bearer {token}"
    Uploader(session).upload_files("12345678", ["first.tar.gz", "second.tar.gz"])
```

`upload_files` runs the same checks as the command: it validates the inputs and draft ID, and checks
every file already in the draft before uploading anything. `new_version`, `import_files` and
`set_version` cover the other steps of `new-version`, and `Uploader(session, api=SANDBOX_API)`
targets the sandbox.

The package is defined in `pyproject.toml`, so it also installs with `pip install .`, which provides
the `zenodo-upload` command.

The mock tests are in `tests/`. Run them with:

```sh
nix flake check . -L
```

The tests don't contact Zenodo.
