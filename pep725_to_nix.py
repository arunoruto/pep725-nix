#!/usr/bin/env python3
"""Prototype: turn PEP 725 ``[external]`` metadata into a nixpkgs derivation.

The script combines three inputs:

1. An ``[external]`` table (PEP 725), either from the ``external-deps-build`` submodule
   (tables written by the PEP authors for the top-150 PyPI experiment) or from
   the sdist's own ``pyproject.toml`` if upstream ships one.
2. The Nix mapping from the PEP 804 registry (``external-metadata-mappings`` submodule).
3. The sdist's ``[build-system]`` and ``[project]`` tables, for the Python side.

It writes ``out/<pkg>.nix`` (a ``python3Packages.callPackage``-able derivation)
and prints a report of every mapping decision, so the gaps between PEP 725/804
and what Nix needs become visible.

Subcommands
-----------
generate PKG [--version V]   write out/PKG.nix and print the mapping report
build PKG                    nix-build the generated derivation
compare [PKG ...]            diff generated external inputs against nixpkgs' own
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent
METADATA_DIR = ROOT / "vendor" / "external-deps-build" / "external_metadata"
NIX_MAPPING = ROOT / "vendor" / "external-metadata-mappings" / "data" / "nix.mapping.json"
OUT_DIR = ROOT / "out"

NIXPKGS = 'import (builtins.getFlake "nixpkgs") {}'

# Decisions the PEP 804 Nix mapping cannot express today. Each entry is a
# finding in itself: ``None`` means "nothing to add", a list is the attrs to use.
NIX_POLICY: dict[str, tuple[list[str] | None, str]] = {
    "dep:virtual/compiler/c": (None, "registry says `gcc`; stdenv.cc already provides it (clang on Darwin)"),
    "dep:virtual/compiler/cxx": (None, "registry says `gcc`; stdenv.cc already provides it (clang on Darwin)"),
    "dep:virtual/compiler/rust": (
        ["rustc", "cargo", "rustPlatform.cargoSetupHook"],
        "registry says `rustc`; cargo + cargoSetupHook are needed too, and a cargoDeps hash",
    ),
    "dep:generic/python": (None, "registry says `python3`; buildPythonPackage provides it"),
    "dep:generic/make": (None, "registry says `gnumake`; part of stdenv"),
    "dep:generic/patch": (None, "registry says `patch`; part of stdenv"),
    "dep:virtual/interface/blas": (["blas"], "registry lists 6 candidates, first is `blas`; no default marked"),
    "dep:virtual/interface/lapack": (
        ["lapack"],
        "registry lists `openblas`, `amd-libflame`; nixpkgs' switchable `lapack` wrapper is missing",
    ),
}

# Setup hooks that hijack buildPythonPackage phases when the tool lands in
# nativeBuildInputs. ninja is absent on purpose: buildPythonPackage defines its
# own buildPhase, so ninja's hook stays inactive (verified with pandas).
HOOK_OPT_OUTS = {
    "cmake": ["dontUseCmakeConfigure"],
    "meson": ["dontUseMesonConfigure"],
}

# [build-system] requirements that nixpkgs provides as setup hooks, not Python modules.
BUILD_BACKEND_HOOKS = {
    "maturin": "rustPlatform.maturinBuildHook",
}

# PyPI distribution name -> python3Packages attr, where they differ.
PYTHON_ATTR_ALIASES = {
    "psycopg2-binary": "psycopg2",
}

SECTIONS = {
    "build-requires": "nativeBuildInputs",
    "host-requires": "buildInputs",
    "dependencies": "propagatedBuildInputs",
}


@dataclass
class Resolution:
    depurl: str
    section: str
    attrs: list[str]
    note: str
    kind: str  # policy | registry | ambiguous | missing


@dataclass
class Plan:
    pname: str
    version: str
    url: str
    hash: str
    build_system: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    python_notes: list[str] = field(default_factory=list)


def load_nix_mapping() -> dict[str, list[dict]]:
    mapping: dict[str, list[dict]] = {}
    for entry in json.loads(NIX_MAPPING.read_text())["mappings"]:
        mapping.setdefault(entry["id"], []).append(entry)
    return mapping


def split_specifier(spec: str) -> tuple[str, str | None, str | None]:
    """Split an external dependency specifier into (DepURL, version, marker)."""
    depurl, _, marker = spec.partition(";")
    depurl = depurl.strip()
    depurl, _, version = depurl.partition("@")
    return depurl, version or None, marker.strip() or None


def resolve(spec: str, section: str, mapping: dict[str, list[dict]]) -> Resolution:
    depurl, version, marker = split_specifier(spec)
    caveats = []
    if version:
        caveats.append(f"version `{version}` not checked")
    if marker:
        caveats.append(f"marker `{marker}` ignored")
    suffix = f" ({'; '.join(caveats)})" if caveats else ""

    if depurl in NIX_POLICY:
        attrs, why = NIX_POLICY[depurl]
        return Resolution(depurl, section, attrs or [], why + suffix, "policy")

    entries = mapping.get(depurl, [])
    if not entries:
        return Resolution(depurl, section, [], "no entry in nix.mapping.json" + suffix, "missing")
    specs = [e["specs"] if isinstance(e["specs"], str) else e["specs"][0] for e in entries]
    if len(specs) > 1:
        return Resolution(
            depurl, section, specs[:1],
            f"{len(specs)} candidates {specs}, picked the first" + suffix, "ambiguous",
        )
    return Resolution(depurl, section, specs, "registry" + suffix, "registry")


def pypi_sdist(pname: str, version: str | None) -> tuple[str, str, str]:
    """Return (version, url, SRI hash) for a package's sdist on PyPI."""
    path = f"{pname}/{version}" if version else pname
    with urllib.request.urlopen(f"https://pypi.org/pypi/{path}/json") as resp:
        data = json.load(resp)
    for f in data["urls"]:
        if f["packagetype"] == "sdist":
            sri = "sha256-" + base64.b64encode(bytes.fromhex(f["digests"]["sha256"])).decode()
            return data["info"]["version"], f["url"], sri
    sys.exit(f"{pname} {version}: no sdist on PyPI")


def sdist_pyproject(url: str) -> dict:
    with urllib.request.urlopen(url) as resp:
        blob = resp.read()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        member = min(
            (m for m in tar.getmembers() if m.name.endswith("/pyproject.toml")),
            key=lambda m: m.name.count("/"),
        )
        f = tar.extractfile(member)
        assert f is not None
        return tomllib.load(f)


def python_attr(req: str) -> str | None:
    """Map a PEP 508 requirement to a python3Packages attr, or None if inapplicable."""
    r = Requirement(req)
    if r.marker and not r.marker.evaluate({"extra": ""}):
        return None
    name = canonicalize_name(r.name)
    return PYTHON_ATTR_ALIASES.get(name, name)


def nixpkgs_version(pname: str) -> str | None:
    attr = PYTHON_ATTR_ALIASES.get(canonicalize_name(pname), canonicalize_name(pname))
    out = nix_eval(f'let pkgs = {NIXPKGS}; in pkgs.python3Packages."{attr}".version or null')
    return out


def nix_eval(expr: str):
    proc = subprocess.run(
        ["nix", "eval", "--json", "--impure", "--expr", expr],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(proc.stderr)
    return json.loads(proc.stdout)


def make_plan(pname: str, version: str | None) -> Plan:
    version = version or nixpkgs_version(pname)
    version, url, sri = pypi_sdist(pname, version)
    pyproject = sdist_pyproject(url)
    plan = Plan(pname, version, url, sri)

    external = pyproject.get("external")
    if external:
        plan.python_notes.append("sdist ships its own [external] table")
    else:
        table = METADATA_DIR / f"{pname}.toml"
        if not table.exists():
            sys.exit(f"{pname}: sdist has no [external] table and {table} does not exist")
        external = tomllib.loads(table.read_text())["external"]

    mapping = load_nix_mapping()
    for key, section in SECTIONS.items():
        for spec in external.get(key, []):
            plan.resolutions.append(resolve(spec, section, mapping))
        for extra, specs in external.get(f"optional-{key}", {}).items():
            for spec in specs:
                res = resolve(spec, section, mapping)
                res.note += f" (optional extra `{extra}`, included)"
                plan.resolutions.append(res)

    build_system = pyproject.get("build-system", {})
    for attr in filter(None, map(python_attr, build_system.get("requires", []))):
        if attr in BUILD_BACKEND_HOOKS:
            plan.resolutions.append(Resolution(
                f"[build-system] {attr}", "nativeBuildInputs", [BUILD_BACKEND_HOOKS[attr]],
                "not a Python module in nixpkgs; provided as a setup hook", "policy",
            ))
        else:
            plan.build_system.append(attr)
    project = pyproject.get("project", {})
    if "dependencies" in project.get("dynamic", []):
        plan.python_notes.append("[project] dependencies are dynamic; runtime Python deps not generated")
    plan.dependencies = [a for a in map(python_attr, project.get("dependencies", [])) if a]
    return plan


def render(plan: Plan) -> str:
    inputs: dict[str, list[str]] = {s: [] for s in SECTIONS.values()}
    comments: dict[str, list[str]] = {s: [] for s in SECTIONS.values()}
    for res in plan.resolutions:
        for attr in res.attrs:
            if attr not in inputs[res.section]:
                inputs[res.section].append(attr)
        if res.kind != "registry":
            comments[res.section].append(f"# {res.depurl}: {res.note}")

    opt_outs = sorted({
        flag
        for attrs in inputs.values() for attr in attrs
        for flag in HOOK_OPT_OUTS.get(attr, [])
    })
    python_args = sorted(set(plan.build_system + plan.dependencies))

    # External deps go through `pkgs.` on purpose: python3Packages.callPackage
    # would otherwise resolve e.g. `ninja` or `cmake` to the PyPI wrapper packages.
    def nix_list(attrs: list[str], pkgs_prefix: bool) -> str:
        items = [f"pkgs.{a}" if pkgs_prefix else a for a in attrs]
        return "[ " + " ".join(items) + " ]" if items else "[ ]"

    lines = [
        f"# Generated by pep725_to_nix.py from PEP 725 [external] metadata for {plan.pname} {plan.version}.",
        "{",
        "  lib,",
        "  pkgs,",
        "  buildPythonPackage,",
        "  fetchurl,",
        *[f"  {a}," for a in python_args],
        "}:",
        "",
        "buildPythonPackage rec {",
        f'  pname = "{plan.pname}";',
        f'  version = "{plan.version}";',
        "  pyproject = true;",
        "",
        "  src = fetchurl {",
        f'    url = "{plan.url}";',
        f'    hash = "{plan.hash}";',
        "  };",
        "",
        f"  build-system = {nix_list(plan.build_system, False)};",
    ]
    if "rustPlatform.cargoSetupHook" in inputs["nativeBuildInputs"]:
        # No metadata can know this hash; `build` fills it in from the mismatch error.
        lines += [
            "",
            "  # Not derivable from metadata: vendored crates for virtual/compiler/rust",
            "  cargoDeps = pkgs.rustPlatform.fetchCargoVendor {",
            "    inherit pname version src;",
            "    hash = lib.fakeHash;",
            "  };",
        ]
    for key, section in SECTIONS.items():
        lines += ["", f"  # [external] {key}"]
        lines += [f"  {c}" for c in comments[section]]
        lines.append(f"  {section} = {nix_list(inputs[section], True)};")
    lines += ["", f"  dependencies = {nix_list(plan.dependencies, False)};"]
    if opt_outs:
        lines += ["", "  # Setup hooks pulled in by [external] tools would take over the build"]
        lines += [f"  {flag} = true;" for flag in opt_outs]
    lines += [
        "",
        "  # Upstream test suites need more than the metadata describes",
        "  doCheck = false;",
        "  pythonImportsCheck = [ ];",
        "}",
        "",
    ]
    return "\n".join(lines)


def report(plan: Plan) -> None:
    print(f"{plan.pname} {plan.version}")
    for res in plan.resolutions:
        target = ", ".join(res.attrs) or "-"
        print(f"  [{res.kind:9}] {res.section:22} {res.depurl:34} -> {target:22} {res.note}")
    print(f"  build-system: {', '.join(plan.build_system) or '-'}")
    print(f"  dependencies: {', '.join(plan.dependencies) or '-'}")
    for note in plan.python_notes:
        print(f"  note: {note}")


def cmd_generate(args) -> None:
    plan = make_plan(args.pkg, args.version)
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{args.pkg}.nix"
    out.write_text(render(plan))
    report(plan)
    print(f"  wrote {out.relative_to(ROOT)}")


def cmd_build(args) -> None:
    path = OUT_DIR / f"{args.pkg}.nix"
    if not path.exists():
        sys.exit(f"{path} missing, run `generate {args.pkg}` first")
    expr = f"let pkgs = {NIXPKGS}; in pkgs.python3Packages.callPackage {path} {{}}"
    cmd = ["nix", "build", "--impure", "--no-link", "--print-out-paths", "-L", "--expr", expr]
    while True:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        assert proc.stderr is not None
        log = []
        for line in proc.stderr:
            sys.stderr.write(line)
            log.append(line)
        if proc.wait() == 0:
            return
        # Fill in hashes left as lib.fakeHash (cargoDeps) and retry once per hash.
        got = re.search(r"got:\s+(sha256-\S+)", "".join(log))
        source = path.read_text()
        if not (got and "lib.fakeHash" in source):
            sys.exit(proc.returncode)
        print(f"filling in hash {got.group(1)} and rebuilding", file=sys.stderr)
        path.write_text(source.replace("lib.fakeHash", f'"{got.group(1)}"', 1))


def cmd_compare(args) -> None:
    """Diff the external inputs PEP 725 implies against what nixpkgs uses today."""
    mapping = load_nix_mapping()
    pkgs = args.pkgs or sorted(p.stem for p in METADATA_DIR.glob("*.toml"))
    generated: dict[str, list[str]] = {}
    for pkg in pkgs:
        external = tomllib.loads((METADATA_DIR / f"{pkg}.toml").read_text())["external"]
        attrs = []
        for key, section in SECTIONS.items():
            specs = list(external.get(key, []))
            for extra in external.get(f"optional-{key}", {}).values():
                specs += extra
            for spec in specs:
                attrs += resolve(spec, section, mapping).attrs
        generated[pkg] = sorted(set(attrs))

    # Resolve generated attrs to pnames and collect nixpkgs' non-Python inputs in one eval.
    attr_set = sorted({a for attrs in generated.values() for a in attrs})
    py_attrs = {pkg: PYTHON_ATTR_ALIASES.get(pkg, pkg) for pkg in pkgs}
    expr = f"""
      let
        pkgs = {NIXPKGS};
        lib = pkgs.lib;
        pname = d: d.pname or (builtins.parseDrvName d.name).name;
        isHook = d: lib.hasSuffix "-hook" (pname d) || lib.hasSuffix "-hook.sh" (pname d);
        external = d: !(d ? pythonModule) && !(isHook d);
        # Hooks like maturinBuildHook propagate cargo/rustc, so expand them one level.
        expand = d: [ d ] ++ lib.optionals (isHook d)
          ((d.propagatedBuildInputs or []) ++ (d.propagatedNativeBuildInputs or []));
        inputs = p: lib.unique (map pname (builtins.filter external (lib.concatMap expand
          ((p.nativeBuildInputs or []) ++ (p.buildInputs or []) ++ (p.propagatedBuildInputs or [])))));
        get = a: lib.attrByPath (lib.splitString "." a) null pkgs;
      in {{
        attrs = lib.genAttrs (builtins.fromJSON {json.dumps(json.dumps(attr_set))}) (a:
          let r = builtins.tryEval (let d = get a; in if d == null then null else pname d);
          in if r.success then r.value else null);
        nixpkgs = lib.mapAttrs (_: a:
          let r = builtins.tryEval (inputs pkgs.python3Packages.${{a}});
          in if r.success then r.value else null) (builtins.fromJSON {json.dumps(json.dumps(py_attrs))});
      }}
    """
    data = nix_eval(expr)

    def normalize(name: str) -> str:
        name = re.sub(r"-wrapper$", "", name)
        return "pkg-config" if name == "pkgconf" else name

    def is_hook(name: str) -> bool:
        return name.endswith(("-hook", "-hook.sh"))

    missing_attrs = [a for a, p in data["attrs"].items() if p is None]
    for pkg in pkgs:
        ours = {
            normalize(data["attrs"][a])
            for a in generated[pkg]
            if data["attrs"].get(a) and not is_hook(data["attrs"][a])
        }
        theirs = data["nixpkgs"].get(pkg)
        if theirs is None:
            print(f"{pkg:20} not in nixpkgs python3Packages")
            continue
        theirs = {normalize(n) for n in theirs}
        only_pep = sorted(ours - theirs)
        only_nix = sorted(theirs - ours)
        status = "match" if not (only_pep or only_nix) else "diff"
        print(f"{pkg:20} {status:5}  only in PEP 725: {', '.join(only_pep) or '-':30}"
              f" only in nixpkgs: {', '.join(only_nix) or '-'}")
    if missing_attrs:
        print(f"\nattrs from the mapping that do not exist in nixpkgs: {', '.join(missing_attrs)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    gen = sub.add_parser("generate", help="write out/PKG.nix and print the mapping report")
    gen.add_argument("pkg")
    gen.add_argument("--version", help="PyPI version (default: the version in nixpkgs)")
    gen.set_defaults(func=cmd_generate)

    build = sub.add_parser("build", help="build the generated derivation")
    build.add_argument("pkg")
    build.set_defaults(func=cmd_build)

    cmp = sub.add_parser("compare", help="diff PEP 725 inputs against nixpkgs")
    cmp.add_argument("pkgs", nargs="*")
    cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
