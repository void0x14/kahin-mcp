# Kahin Camoufox patches

These are Kahin's own changes on top of the Camoufox recipe base
(`daijro/camoufox 152.0.4-beta.31` = Firefox 152.0.4 source + Camoufox overlay).
They are the delta against the recipe baseline, not against vanilla Firefox:
the baseline already contains `additions/` and the recipe `patches/` series.

## Why a patch series

An installed engine is a build artifact. It belongs in the Camoufox cache only
as long as it is disposable. Kahin needs the opposite: a browser it can rebuild
after every upstream bump, with the same behaviour. The durable form is this
patch series plus a rebuild script; the cache is replaceable by definition.

Engine installs live outside the cache:

```
$KAHIN_HOME/engines/<version>-<build>-<stamp>/   # source-built distribution
$KAHIN_HOME/engines/current -> <version>-...     # selected engine
```

`kahin/the_twins/mirage.py` resolves `$KAHIN_HOME/engines/current/camoufox-bin`
before any Camoufox cache path, and reads the sibling `version.json` so
`launch_options` does not need a cache-resident active version.

## Upgrading Camoufox

1. Generate a fresh Camoufox source tree from the recipe (`make dir`).
2. Apply this series and build:

   ```bash
   scripts/build-camoufox-patched.sh /path/to/camoufox-<version>
   ```

   The script dry-runs every patch first. A rejected hunk is a rebase task,
   never a silently skipped patch.

3. Verify the installed engine starts and `navigator.buildID` matches the new
   build. Then move `engines/current` to the new directory.

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
