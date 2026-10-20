# Security and public-repository hygiene

## Deployment trust boundary

This is a reverse proxy, not an authentication service or an inference sandbox. Use a trusted
private network or TLS frontend and retain Strata's normal API authentication. Bind defaults to
loopback. Backend control credentials are separate environment secrets. Ordinary request
credentials are forwarded unchanged. No destructive administrative HTTP routes are provided.

Managed mode requires exclusive access to a serial backend, a trusted shared snapshot directory,
and one proxy process. File ownership is a security boundary: do not give untrusted users write
access to snapshot/state directories. Snapshot files contain private model state and token IDs;
use encryption and private backups as appropriate. SQLite fingerprints are sensitive equality
metadata even without plaintext histories. Never import snapshots from an untrusted source.

Private directories must be mode 0700 and owned by the runtime UID. Snapshots must be regular,
single-link, owner-only files; symlink reads/deletes are refused. Operators must protect ancestor
directories too. Mount checks cannot prevent an administrator from racing unmounts; use service
mount dependencies and a private underlying mountpoint on both hosts. Ensure only one catalog
owns a snapshot directory; local file locks protect both configured directories, but are not
a distributed backend ownership protocol.

Default logs contain fixed categories, opaque fingerprint prefixes and random filenames, not
requests, responses, headers, upstream error bodies or URLs. Access logging is disabled. Python
or service-manager diagnostics under exceptional runtime failures may include paths; inspect and
redact all logs before sharing. Do not enable upstream request capture for public diagnostics.

## Repository rules

* Use `config.example.yaml` with generic localhost values; place actual overrides in ignored
  `config.local.yaml`, an ignored `.env`, or configuration outside the checkout.
* Never commit credentials, real network addresses/hostnames, identifying account/path details,
  conversations, collected inference payloads, snapshots, SQLite catalogs, logs, or environments.
* `.gitignore` excludes local config, secrets, snapshots, state databases, logs and Python artifacts.
  Ignore rules are not a secret scanner and cannot protect a file explicitly force-added.
* Tests use fabricated payloads only and must never use a deployment config or external server.
* Before every commit inspect `git diff --cached --check`, `git diff --cached --stat`,
  `git diff --cached` and `git ls-files`. Search staged content for credentials, identifying
  addresses/paths and runtime data. Do not publish raw failure output without reviewing it.
* No Git remote should be configured or pushed without the repository owner's approval.

## Reporting issues

Do not attach raw catalogs, snapshots, credentials or actual conversations to public issues.
Provide version information, fixed error category, a synthetic reproducer and redacted counts.
If a vulnerability requires private details, contact the eventual repository maintainer through
an appropriate private reporting channel before disclosing it publicly. This source distribution
does not invent a maintainer email or private reporting endpoint.
