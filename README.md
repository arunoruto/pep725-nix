# PEP 725 → Nix prototype

Experiment: can [PEP 725](https://peps.python.org/pep-0725/) `[external]` metadata plus the
[PEP 804](https://peps.python.org/pep-0804/) Nix mapping generate working nixpkgs derivations?

## Inputs

- `external_metadata/` — `[external]` tables for the 37 top-150 PyPI packages that need a compiler,
  written by the PEP authors for their build experiment
  ([rgommers/external-deps-build](https://github.com/rgommers/external-deps-build)).
- `registry/` — `registry.json` and `nix.mapping.json` from
  [jaimergp/external-metadata-mappings](https://github.com/jaimergp/external-metadata-mappings).

Both are copied unchanged under their original licenses (MIT and BSD-3-Clause), included as
`LICENSE` in each directory. Snapshot taken 2026-10-02.

## Usage

```sh
python3 pep725_to_nix.py generate numpy   # writes out/numpy.nix, prints every mapping decision
python3 pep725_to_nix.py build numpy      # nix build against the nixpkgs flake registry entry
python3 pep725_to_nix.py compare          # diff PEP 725 inputs vs nixpkgs' hand-written ones
```

The version defaults to the one in nixpkgs so the comparison is like-for-like. Python build/runtime
dependencies come from the sdist's own `[build-system]` and `[project]` tables.

`NIX_POLICY` and `HOOK_OPT_OUTS` in the script hold every decision the registry could not express;
each entry there is a finding.

## Results (nixpkgs 26.05pre, Python 3.13)

| | |
|---|---|
| numpy 2.4.4 | builds, links nixpkgs `blas`/`lapack` |
| pandas 2.3.3 | builds |
| cryptography 48.0.0 | builds after filling in the `cargoDeps` hash and mapping `maturin` to `maturinBuildHook`; links nixpkgs OpenSSL 3.6.4 |
| pillow 12.3.0 | fails as generated (zlib not found); builds after adding `pkg-config` by hand (`out/pillow.nix`, raw output in `out/pillow-generated.nix`). imagequant, xcb and JPEG 2000 still not compiled in |
| scipy 1.17.1 | fails as generated (meson looks for OpenBLAS by name); builds after adding `mesonFlags = [ "-Dblas=blas" "-Dlapack=lapack" ]` by hand (`out/scipy.nix`, raw output in `out/scipy-generated.nix`). Links nixpkgs `blas`/`lapack`, uses its vendored boost/qhull |
| pyyaml 6.0.3, cross-built for aarch64 | builds unchanged from the generated file; `_yaml.so` is an aarch64 ELF linking aarch64 `libyaml-0.so.2` |
| cross evaluation (aarch64) | `build-requires` resolve to x86_64 tools targeting aarch64, `host-requires` to aarch64 libraries, for pyyaml, numpy and cryptography |
| `compare` over 37 packages | 18 identical to nixpkgs, 19 differ (see `out/compare.txt`) |

## Findings

### The mapping file

1. **Wrong targets.** `dep:generic/libpq` → `libpqxx` (the C++ wrapper; nixpkgs uses `libpq`).
   `dep:generic/libjpeg` lists `libjpeg_original` before `libjpeg` (nixpkgs' default, which is
   libjpeg-turbo), with nothing saying which to prefer. The `clblas` entry links to `clblast`.
2. **Virtual packages have no default.** `virtual/interface/blas` lists 6 candidates and
   `virtual/interface/lapack` lists `openblas`, `amd-libflame` — but not nixpkgs' switchable
   `blas`/`lapack` wrappers, which are exactly the "virtual package" concept.
   Stale names too: `xorg.libxcb` triggers nixpkgs' deprecation warning (now `libxcb`).
3. **Compilers map to `gcc`.** In nixpkgs `virtual/compiler/c`/`cxx` mean "stdenv.cc" (clang on
   Darwin); adding `gcc` is wrong. Same for `generic/make`, `generic/patch` (stdenv) and
   `generic/python` (buildPythonPackage).
4. **One attr is not always enough.** `virtual/compiler/rust` → `rustc`, but a build also needs
   `cargo`, `rustPlatform.cargoSetupHook` and a `cargoDeps` hash that no metadata can provide.
5. **Package-manager commands are for imperative installs only** (`nix-env -iA`, `nix-shell -p`);
   nothing describes "add to a derivation", which is how Nix actually consumes this.

### Nix-specific consumption issues

6. **Setup hooks.** A DepURL says *what* a tool is, not that its Nix setup hook may change the
   build. ninja is harmless (pandas builds without opt-outs, since buildPythonPackage sets its own
   buildPhase), but `cmake`'s configure hook needs `dontUseCmakeConfigure`, which nixpkgs sets by
   hand. The generator adds it; not yet tested with a cmake-based package.
7. **Name collisions.** `python3Packages.callPackage` resolves `ninja`/`cmake` to the PyPI wrapper
   packages, so external deps must be referenced as `pkgs.<attr>`. This is the "PyPI-packaged
   binaries" case PEP 725 lists under rejected ideas, showing up concretely.
8. **Python build backends that are not Python modules.** cryptography's `[build-system]` requires
   `maturin`; in nixpkgs that is `rustPlatform.maturinBuildHook`, and a plain `python3Packages`
   lookup silently resolves to the maturin binary instead, failing with `Cannot import 'maturin'`.
9. **Transitive deps.** nixpkgs gets `libffi` for cryptography via `cffi`, while PEP 725 lists it
   directly; harmless, but a naive diff flags it.

### The `[external]` tables vs nixpkgs (from `compare`)

10. **Tables can be incomplete where packages vendor or auto-detect.** nixpkgs links system libraries
   the tables omit: aiohttp → `llhttp`, grpcio → `c-ares`/`openssl`/`zlib`, google-crc32c →
   `crc32c`, matplotlib → `freetype`/`qhull`, scipy → `boost`/`qhull`/`xsimd`, pynacl → `libsodium`,
   pyarrow → `arrow-cpp`. These are mostly "unvendoring" choices distros make, which PEP 725 cannot
   express (no "uses X if available, else vendored copy").
11. **Tables can also over-declare.** numpy's table lists `virtual/compiler/fortran`, and so does
    nixpkgs, but numpy 2.4.4 builds and links BLAS/LAPACK fine without gfortran (its `meson.build`
    only declares C and C++).
12. **`pkg-config` differs** for cffi, lxml, pandas and pillow: nixpkgs adds it, the tables don't.
    pandas builds fine without it, but pillow does not: without pkg-config its `setup.py` only
    searches FHS paths and cannot find zlib. So at least pillow's table is missing
    `dep:generic/pkg-config`.
13. **Out-of-tree tables go stale.** pillow 12 supports AVIF; nixpkgs links `libavif`, the table
    (written for an older pillow) does not list it. An argument for upstream-owned `[external]`.

### Declaring a dependency is not the same as the build finding it

14. With pkg-config added, pillow's `[external]` table names the right libraries and they are all in
    `buildInputs`, yet imagequant, openjpeg and xcb are still not compiled in. nixpkgs' own pillow
    patches `setup.py` in `preConfigure` with hard-coded store paths for imagequant, openjpeg and
    avif ("The build process fails to find the pkg-config files for these dependencies") and sets
    `CFLAGS`/`LDFLAGS` for xcb. PEP 725 metadata cannot carry this; it is per-ecosystem build glue
    that stays in the derivation (or in a generator's override set).
15. **Virtual packages need a selection knob.** `dep:virtual/interface/blas` correctly says "any
    BLAS", and numpy auto-detects nixpkgs' `blas`. scipy does not: its meson build defaults to
    OpenBLAS by name and fails until told `-Dblas=blas -Dlapack=lapack`, which nixpkgs passes in
    `mesonFlags`. Choosing the implementation is the consumer's job, but *how* to tell the build is
    per-package and outside PEP 725.
16. **Vendoring explains the scipy "gap".** nixpkgs passes `-Duse-system-libraries=all` to use
    system boost/qhull/xsimd; the default build uses scipy's bundled copies, which is what the table
    describes. The table is right for a default build; distros opting into system libraries need
    information PEP 725 does not have.

### Cross-compilation

17. **The build/host split maps directly onto nixpkgs splicing.** Evaluated under
    `pkgsCross.aarch64-multiplatform`, `build-requires` land in `nativeBuildInputs` and resolve to
    x86_64 tools targeting aarch64 (`aarch64-unknown-linux-gnu-gfortran`, cross `rustc`,
    pkg-config), while `host-requires` land in `buildInputs` and resolve to aarch64 libraries
    (openssl, libffi, blas, lapack, libyaml). This is concrete support for keeping the standard
    build/host terminology debated in the PEP 725 thread. An actual cross build of pyyaml from the
    unmodified generated file succeeds, with the extension linking the aarch64 libyaml.

## Not covered

- Only numpy, pandas, cryptography, pillow and scipy were built; the other 32 were compared
  statically.
- Linux x86_64 only. Cross-compilation was evaluated for three packages and built for pyyaml only;
  the cross-built module was inspected, not executed (no aarch64 machine or emulation).
- Version constraints on DepURLs are reported, not checked.

### Out of scope: why `pip install numpy` fails on NixOS

The familiar NixOS errors (`libz.so.1: cannot open shared object file` for numpy, missing
`libstdc++.so.6` for pandas) come from prebuilt manylinux **wheels**, not from building. The wheels
expect these libraries from an FHS system:

| Wheel | Bundled in the wheel | Expected from the system |
|---|---|---|
| numpy 2.4.4 | OpenBLAS, `libgfortran`, `libquadmath` | `libc`, `libm`, `libgcc_s`, `libstdc++.so.6`, `libz.so.1` |
| pandas 2.3.3 | nothing | `libc`, `libm`, `libgcc_s`, `libstdc++.so.6` |

numpy's `libz.so.1` is needed by the bundled `libgfortran`, not by numpy itself. What a wheel
expects from the system is governed by the manylinux tag and auditwheel, not by PEP 725, so an
`[external]` table rightly would not list it. For Nix tooling consuming wheels, the answer is
`autoPatchelfHook` plus nixpkgs' `pythonManylinuxPackages`; PEP 725 helps the sdist path only.
