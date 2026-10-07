# Zenodo draft uploader

Upload large files to an existing Zenodo draft from the terminal. Based on [Pranav Durai's script](https://github.com/zenodo/zenodo/issues/2514#issuecomment-4956445997).

The script uploads files one at a time and checks their size and MD5 checksum. It doesn't create drafts, change metadata, or publish anything.

## Usage

You'll need Nix, an existing draft, and a [Zenodo token](https://zenodo.org/account/settings/applications/tokens/new/) with `deposit:write` permission. The draft ID is the number in your draft's URL.

Set `ZENODO_TOKEN` in your environment, then pass the draft ID and file paths:

```sh
nix run . -- 12345678 first.tar.gz second.tar.gz
```

The token is read only from `ZENODO_TOKEN`, not from a command-line option. To enter it without saving it in your shell history, use this in zsh:

```sh
read -rs 'ZENODO_TOKEN?Zenodo token: '
echo
export ZENODO_TOKEN
```

Only the files you name are uploaded. Local files are never changed. Each file keeps its filename, so you can't upload two files with the same name in one run.

## Retries and verification

Progress is shown while hashing and uploading. An upload counts as successful only when Zenodo returns a matching size and MD5 checksum. Missing verification data is an error.

Files already in the draft are checked before being skipped. If a file doesn't match, the script stops instead of replacing it. Don't edit the local files or upload to the same draft from another process while it's running.

Connection failures, timeouts, HTTP 408/429 responses, and server errors are retried up to five attempts per operation by default. Use `--attempts` to change that, including the first try:

```sh
nix run . -- --attempts 10 12345678 first.tar.gz second.tar.gz
```

The limit applies separately to each file upload and each draft API check. `--attempts 1` disables retries. Delays start at 10 seconds, then 20 and 40, and stay at 60 seconds for further retries. Connection and read timeouts are 60 seconds and two hours respectively, not a limit on the total upload time.

You can stop with Ctrl+C and run the same command again. Completed files are verified and skipped. Interrupted uploads start over from the beginning. If an attempt left an incomplete or conflicting file in the draft, inspect it in Zenodo and remove it before retrying.

Failures return a nonzero exit status. Files uploaded earlier stay in the draft, even if a later upload fails.

## Development

Python, `requests`, and `tqdm` come from the pinned Nix environment.

```sh
nix develop . --command python zenodo_upload.py 12345678 first.tar.gz second.tar.gz
```

Run the mock tests with:

```sh
nix flake check . -L
```

The tests don't contact Zenodo.
