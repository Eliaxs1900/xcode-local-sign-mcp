# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""xcode-local-sign-mcp: build, sign and run Xcode apps on this Mac WITHOUT an
Apple developer certificate, and capture a log trace as proof.

Why: a cloned project carries its author's DEVELOPMENT_TEAM, and this Mac has
no signing identity, so a plain `xcodebuild` fails or produces an app the
sandbox will not run. Ad-hoc signing ("-") is enough to build and launch
locally, with the app's real entitlements and App Sandbox enforced.

Tools
  signing_status      identities in the keychain, the project's teams/signing
                      settings and sandbox entitlement build settings
  build_local         xcodebuild with ad-hoc signing (team cleared); returns the
                      .app path, its entitlements and any errors
  show_entitlements   entitlements actually signed into an .app
  resign_adhoc        re-sign an .app ad-hoc, adding/removing entitlement keys
                      (A/B test a sandbox permission without rebuilding)
  launch_with_trace   run the app's binary with env vars, capture its console
                      (optionally filtered) plus kernel/sandbox log lines, save
                      everything to an evidence folder

Limits (stated, not hidden)
  * XCUITest/XCTest on macOS still need a development certificate: Xcode
    refuses ad-hoc for the test action. Get one free by signing in with an
    Apple ID in Xcode ▸ Settings ▸ Accounts (the user does that, not this tool).
  * Ad-hoc apps run only on this Mac; never for distribution.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("xcode-local-sign")

ADHOC = ["CODE_SIGN_IDENTITY=-", "DEVELOPMENT_TEAM=", "CODE_SIGN_STYLE=Manual",
         "PROVISIONING_PROFILE_SPECIFIER="]
DEFAULT_OUT = Path.home() / "Desktop" / "xcode_local_builds"


def _run(cmd: list[str], timeout: int = 1800, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def _container_args(project: str) -> list[str]:
    p = Path(project).expanduser()
    if p.suffix == ".xcworkspace":
        return ["-workspace", str(p)]
    if p.suffix == ".xcodeproj":
        return ["-project", str(p)]
    ws = sorted(p.glob("*.xcworkspace"))
    if ws:
        return ["-workspace", str(ws[0])]
    pj = sorted(p.glob("*.xcodeproj"))
    if pj:
        return ["-project", str(pj[0])]
    raise ValueError(f"no .xcodeproj or .xcworkspace in {p}")


def _entitlements(app: str) -> dict:
    r = _run(["codesign", "-d", "--entitlements", "-", "--xml", app], timeout=60)
    data = r.stdout.encode()
    if not data.strip():
        return {}
    try:
        return plistlib.loads(data)
    except Exception:
        return {"_raw": r.stdout}


def _errors(text: str, limit: int = 25) -> list[str]:
    seen, out = set(), []
    for line in text.splitlines():
        if (re.search(r"\berror:|ld: (?!warning)|\*\* (BUILD|TEST) FAILED", line)
                and line not in seen):
            seen.add(line); out.append(line.strip())
    return out[:limit]


@mcp.tool()
def signing_status(project: str | None = None) -> str:
    """Signing identities on this Mac and, if `project` is given (folder,
    .xcodeproj or .xcworkspace), its team, signing style and sandbox/network
    entitlement build settings."""
    out: dict = {}
    r = _run(["security", "find-identity", "-v", "-p", "codesigning"], timeout=30)
    out["identities"] = [l.strip() for l in r.stdout.splitlines() if '"' in l]
    out["identity_count"] = len(out["identities"])
    out["xcode"] = _run(["xcodebuild", "-version"], timeout=60).stdout.strip()
    if project:
        args = _container_args(project)
        r = _run(["xcodebuild", *args, "-list", "-json"], timeout=300)
        try:
            info = json.loads(r.stdout)
        except Exception:
            info = {"error": r.stderr[-800:]}
        out["list"] = info
        pbx = None
        if args[0] == "-project":
            pbx = Path(args[1]) / "project.pbxproj"
        else:
            cands = list(Path(args[1]).parent.glob("*.xcodeproj/project.pbxproj"))
            pbx = cands[0] if cands else None
        if pbx and pbx.exists():
            txt = pbx.read_text(errors="replace")
            keys = r"(DEVELOPMENT_TEAM|CODE_SIGN_STYLE|CODE_SIGN_IDENTITY|CODE_SIGN_ENTITLEMENTS|ENABLE_APP_SANDBOX|ENABLE_HARDENED_RUNTIME|ENABLE_[A-Z_]*NETWORK[A-Z_]*|SUPPORTS_MACCATALYST|SUPPORTED_PLATFORMS)"
            found: dict[str, set] = {}
            for m in re.finditer(keys + r'\s*=\s*"?([^";]*)"?;', txt):
                found.setdefault(m.group(1), set()).add(m.group(2))
            out["project_settings"] = {k: sorted(v) for k, v in found.items()}
    out["advice"] = ("No identity: use build_local (ad-hoc). Tests on macOS need a free "
                     "Apple Development cert (Xcode ▸ Settings ▸ Accounts, done by the user)."
                     if out["identity_count"] == 0 else
                     "Identities present: Xcode can sign with them; build_local still works ad-hoc.")
    return json.dumps(out, indent=2, ensure_ascii=False)


@mcp.tool()
def build_local(project: str, scheme: str,
                destination: str = "platform=macOS,variant=Mac Catalyst",
                configuration: str = "Debug",
                derived_data: str | None = None,
                extra_settings: list[str] | None = None,
                timeout_s: int = 1800) -> str:
    """Build `scheme` signed ad-hoc (no certificate, team cleared). Destination
    examples: 'platform=macOS,variant=Mac Catalyst', 'platform=macOS',
    'platform=iOS Simulator,name=iPad Pro 13-inch (M5)'. extra_settings are
    KEY=VALUE build settings (e.g. ENABLE_OUTGOING_NETWORK_CONNECTIONS=NO).
    Returns the built .app path(s), their entitlements and any errors."""
    args = _container_args(project)
    dd = Path(derived_data).expanduser() if derived_data else DEFAULT_OUT / f"{scheme}_{re.sub(r'[^A-Za-z0-9]+', '_', destination)}"
    dd.mkdir(parents=True, exist_ok=True)
    cmd = ["xcodebuild", *args, "-scheme", scheme, "-destination", destination,
           "-configuration", configuration, "-derivedDataPath", str(dd), "build",
           *ADHOC, *(extra_settings or [])]
    t0 = time.time()
    r = _run(cmd, timeout=timeout_s)
    log = dd / "build_local.log"
    log.write_text(r.stdout + r.stderr)
    text = r.stdout + r.stderr
    ok = "** BUILD SUCCEEDED **" in text
    hints = []
    if "missing Metal Toolchain" in text:
        hints.append("run: xcodebuild -downloadComponent MetalToolchain")
    if "unknown file type" in text and ".a'" in text:
        hints.append("static libs look like Git LFS pointers: git lfs install --local && git lfs pull")
    if "was modified during the build" in text:
        hints.append("package resolution raced with Xcode: rerun")
    apps = sorted(str(p) for p in (dd / "Build" / "Products").glob(f"{configuration}*/*.app"))
    res = {"ok": ok, "seconds": round(time.time() - t0, 1), "command": shlex.join(cmd),
           "log": str(log), "errors": _errors(text), "hints": hints,
           "apps": {a: _entitlements(a) for a in apps} if ok else {}}
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def show_entitlements(app_path: str) -> str:
    """Entitlements actually signed into an .app (what the sandbox enforces)."""
    return json.dumps(_entitlements(str(Path(app_path).expanduser())), indent=2, default=str)


@mcp.tool()
def resign_adhoc(app_path: str, add: dict | None = None, remove: list[str] | None = None,
                 copy_to: str | None = None) -> str:
    """Re-sign an .app ad-hoc keeping its entitlements, with keys added
    (`add`, e.g. {"com.apple.security.network.client": true}) or removed
    (`remove`). `copy_to` works on a copy so the original build stays intact.
    Nested frameworks/dylibs are re-signed first."""
    src = Path(app_path).expanduser()
    app = src
    if copy_to:
        app = Path(copy_to).expanduser()
        if app.exists():
            subprocess.run(["rm", "-rf", str(app)], check=True)
        subprocess.run(["ditto", str(src), str(app)], check=True)
    ents = _entitlements(str(app))
    ents.pop("_raw", None)
    for k in remove or []:
        ents.pop(k, None)
    ents.update(add or {})
    ents.setdefault("com.apple.security.get-task-allow", True)
    # Keep the original signature's flags. With hardened runtime, ad-hoc
    # nested dylibs fail Library Validation (no team ID to match), so allow
    # them — local runs only.
    dv = _run(["codesign", "-dv", str(app)], timeout=60).stderr
    runtime = bool(re.search(r"flags=0x[0-9a-f]+\([^)]*runtime", dv))
    if runtime:
        ents["com.apple.security.cs.disable-library-validation"] = True
    nested = sorted({str(p) for pat in ("**/*.framework", "**/*.dylib", "**/*.appex", "**/*.xpc")
                     for p in app.glob(pat)}, key=len, reverse=True)
    logs = []
    for n in nested:
        r = _run(["codesign", "--force", "--sign", "-", "--timestamp=none", n], timeout=120)
        if r.returncode:
            logs.append(f"{n}: {r.stderr.strip()}")
    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as f:
        plistlib.dump(ents, f); ent_file = f.name
    r = _run(["codesign", "--force", "--sign", "-", "--timestamp=none",
              *(["--options", "runtime"] if runtime else []),
              "--entitlements", ent_file, str(app)], timeout=300)
    v = _run(["codesign", "--verify", "--deep", "--strict", str(app)], timeout=120)
    return json.dumps({"app": str(app), "signed": r.returncode == 0, "sign_stderr": r.stderr.strip(),
                       "verify_ok": v.returncode == 0, "verify": v.stderr.strip(),
                       "nested_errors": logs, "entitlements": _entitlements(str(app))},
                      indent=2, default=str)


@mcp.tool()
def launch_with_trace(app_path: str, env: dict | None = None, seconds: int = 30,
                      console_filter: str | None = None,
                      evidence_dir: str | None = None,
                      log_predicate: str | None = None) -> str:
    """Run the app's executable directly (sandbox applies) with `env`, for at
    most `seconds` (it may exit sooner). Captures stdout/stderr (only lines
    containing `console_filter` if given) and, in parallel, system log lines
    from the kernel/Sandbox about this process (or `log_predicate`). Saves
    console.txt, system_log.txt and entitlements.json to `evidence_dir`."""
    app = Path(app_path).expanduser()
    info = plistlib.loads((app / ("Contents/Info.plist" if (app / "Contents").exists() else "Info.plist")).read_bytes())
    exe_name = info["CFBundleExecutable"]
    exe = app / "Contents" / "MacOS" / exe_name
    if not exe.exists():
        exe = app / exe_name
    out = Path(evidence_dir).expanduser() if evidence_dir else DEFAULT_OUT / "evidence" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    pred = log_predicate or (f'(process == "kernel" OR sender == "Sandbox" OR process == "sandboxd") '
                             f'AND eventMessage CONTAINS "{exe_name}" '
                             f'AND NOT eventMessage CONTAINS "ReportCrash"')
    logf = open(out / "system_log.txt", "w")
    logp = subprocess.Popen(["/usr/bin/log", "stream", "--style", "compact", "--predicate", pred],
                            stdout=logf, stderr=subprocess.STDOUT)
    time.sleep(2)
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    t0 = time.time()
    p = subprocess.Popen([str(exe)], env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        raw, _ = p.communicate(timeout=seconds)
        exit_code = p.returncode
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGTERM)
        try:
            raw, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill(); raw, _ = p.communicate()
        exit_code = "timeout (terminated)"
    time.sleep(2)
    logp.terminate(); logp.wait(timeout=5); logf.close()
    lines = raw.splitlines()
    if console_filter:
        lines = [l for l in lines if console_filter in l]
    header = f"$ {' '.join(f'{k}={v}' for k, v in (env or {}).items())} {exe}\n"
    (out / "console.txt").write_text(header + "\n".join(lines) + "\n")
    ents = _entitlements(str(app))
    (out / "entitlements.json").write_text(json.dumps(ents, indent=2, default=str))
    syslog = [l for l in (out / "system_log.txt").read_text(errors="replace").splitlines()
              if l and not l.startswith(("Filtering", "Timestamp"))]
    return json.dumps({"evidence_dir": str(out), "exit": exit_code,
                       "seconds": round(time.time() - t0, 1),
                       "entitlements": ents, "console": lines[-60:], "system_log": syslog[-40:]},
                      indent=2, ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()
