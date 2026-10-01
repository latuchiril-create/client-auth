# client-auth

FastAPI license service deployed by Render with `uvicorn main:app --host 0.0.0.0 --port $PORT`.

## Protected 1.21.8 release

`release/manifest.json` is an offline Ed25519-signed manifest for the licensed
JAR. `release/parts/artifact.part*` contain the same JAR encrypted with
AES-256-GCM and split into files smaller than 50 MB. The server checks the
encryption tag before streaming plaintext, and only active Loader sessions
may request the manifest or download. Loader independently verifies the
manifest signature, byte length, and JAR SHA-256.

Set these additional **secret** Render environment variables before deploying
this revision:

- `CLIENT_ARTIFACT_KEY_B64`: base64 contents of the offline artifact key file.
- `LICENSE_ASSERTION_SEED_B64`: base64 contents of the separate license
  assertion signing seed. The Java client pins only its public key.

Keep `ADMIN_API_KEY` configured and rotate any previously hardcoded admin key.
The manifest and encrypted parts paths default to the bundled `release/`
directory. Do not set `CLIENT_ARTIFACT_PATH` in Render; that optional setting
serves a plaintext JAR for local development.

The two private key files remain outside Git at `D:\FugaReleaseKeys` on the
release machine. Back them up securely. Never commit them or paste their
values into issues or logs. Rebuilding the client requires a new signed
manifest and encrypted parts from the exact new JAR.
