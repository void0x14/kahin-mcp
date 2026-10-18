# Kahin Camoufox patches

These are Kahin's own changes on top of the Camoufox recipe base
(`daijro/camoufox 152.0.4-beta.31` = Firefox 152.0.4 source + Camoufox overlay).
They are the delta against the recipe baseline, not against vanilla Firefox:
the baseline already contains `additions/` and the recipe `patches/` series.

## What this is

The patch files are a **record**, not an install gate. Nothing here runs
automatically and nothing forces a build step. The engine that Kahin actually
uses is whatever `$KAHIN_HOME/engines/current/camoufox-bin` points at:

```
$KAHIN_HOME/engines/<version>-<build>-<stamp>/   # source-built distribution
$KAHIN_HOME/engines/current -> <version>-...     # selected engine
```

`kahin/the_twins/mirage.py` resolves `engines/current` before any Camoufox
cache path, and reads the sibling `version.json` so `launch_options` does not
need a cache-resident active version. `KAHIN_CAMOUFOX_BIN` overrides all of it.

## Updating

Plain `camoufox fetch` writes only to `~/.cache/camoufox`; it never touches
`$KAHIN_HOME/engines`. To switch to a fetched version, repoint `current` (or
set `KAHIN_CAMOUFOX_BIN`):

```bash
ln -sfn ~/.cache/camoufox/browsers/official/<version> \
        "$KAHIN_HOME/engines/current"
```

To use the patches with a new source tree, apply them to that tree and put the
result under `engines/` — or skip it. The stock fetched engine starts fine
either way; the patches only add the OOPIF click, per-context timezone and
Juggler child routing behaviour.

## Regenerating the series

The baseline is: Firefox source + `scripts/copy-additions.sh` + the recipe
`patches/` applied in recipe order. Diff the live tree against that baseline;
only these files differ:

| Patch | Files | Purpose |
|---|---|---|
| 0001 | `juggler/content/JugglerFrameChild.sys.mjs` | per-frame Juggler child routing |
| 0002 | `dom/events/EventStateManager.cpp`, `dom/ipc/BrowserBridgeParent.cpp` | synthesized input into a nested OOPIF (Turnstile click) |
| 0003 | `js/public/Date.h`, `js/src/vm/DateTime.cpp`, `js/src/vm/Realm.cpp` | per-context timezone override restored |
| 0004 | `build/moz.configure/rust.configure` | gentoo vendor alias for distro Rust triples |

`base.json` records the base version, build id and verified hashes.
