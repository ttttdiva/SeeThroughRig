# Public projection and release notes

The public SeeThroughRig tree is a reviewable projection of the canonical
production checkout. It contains the Aoi runtime, editor/player, tests,
recipes, documentation, the adopted baked model, and the approved showcase
media. It does not contain the source illustration, generated expression
artwork, decomposition work, private production projects, discarded keyform
art, candidate model directories, credentials, or inference weights.

`scripts/sync_public.py` owns the projection into a separate checkout such as
`D:/Publish/SeeThroughRig`. It uses an explicit allow-list and writes
`PUBLIC-MANIFEST.json`. Every inventory row records the source-relative path,
the source SHA-256, the projected SHA-256, and the applied text-redaction
rule, when one was needed. The manifest records the exact source commit while
omitting the private source repository URL.

The destination is safe to reuse. A managed file is replaced only after its
previous manifest hash still matches. A managed file changed locally causes a
conflict instead of being overwritten; unlisted files, empty or non-empty
directories, and `.git` are left alone. Stale managed files are removed only
when their old hash still matches. Run the dry-run first when preparing a
release:

```powershell
python scripts/sync_public.py --dry-run
```

Files are written one at a time through an atomic temporary replacement after
the full preflight. If the process is interrupted during apply, rerun after
resolving the reported conflict; unmanaged files and `.git` remain outside
the managed set.

## Release inputs

The public runtime is a generated, data-free package. Generate it from the
final clean source commit with the immutable baseline available in the
canonical checkout:

```powershell
python scripts/package_runtime.py `
  --output .local/public-runtime `
  --source-ref <final-source-commit>
```

The generated directory contains `runtime-provenance.json`. Its
`source_head` must equal `<final-source-commit>` before the directory is
passed to the public sync. A fresh public clone consumes this bundled runtime
and does not need the private source history or inference environment. The
history-dependent generator is a source release tool; it is not part of the
public quickstart.

Create and validate the data-only Aoi model package in the canonical checkout
after the final source commit. The exporter needs the original source hash and
the actual model preview while it is running; those private production inputs
are not copied into the public projection:

```powershell
$sourceHash = (Get-FileHash assets/aoi/original.png -Algorithm SHA256).Hash.ToLower()
python -m seethrough_rig.integration export `
  --model-dir web/models/aoi `
  --output artifacts/models/kotonoha-aoi.zip `
  --project-id kotonoha-aoi-public `
  --revision aoi-public-<final-source-commit> `
  --source-sha256 $sourceHash `
  --source-commit <final-source-commit> `
  --profile conversation-v1 `
  --wind 0.35 `
  --wind-blow 0.72 `
  --name kotonoha-aoi
python -m seethrough_rig.integration validate `
  --package artifacts/models/kotonoha-aoi.zip
```

Then project the release inputs into the separate public checkout:

```powershell
python scripts/sync_public.py `
  --source-ref <final-source-commit> `
  --runtime-package .local/public-runtime `
  --model-package artifacts/models/kotonoha-aoi.zip `
  --destination D:/Publish/SeeThroughRig
```

The sync tool does not create a GitHub repository or push. Review the
generated inventory, the destination Git diff, the runtime provenance, and
the ZIP validation result before a separate repository publication step.

## Public verification

The bundled static demo can be served without See-through inference:

```powershell
python -m http.server 8000 --directory .
```

Open `http://127.0.0.1:8000/showcase.html` for the video page,
`http://127.0.0.1:8000/web/showcase.html` for the interactive showcase, or
`http://127.0.0.1:8000/web/player.html?model=models/aoi&motion=motions/showcase.json`
for the player. A real WebGL2 browser check, the Node tests, and the Python
tests are separate evidence from Native rendering or a new inference run.

The source tree keeps the original build and integration tools. Rebuilding the
adopted model requires the compatible decomposition output and source artwork
that are intentionally absent from the public projection. The checked-in
model and the data-only ZIP are the reproducible public playback inputs.
