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

import glob
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("xcode-local-sign")

ADHOC = ["CODE_SIGN_IDENTITY=-", "DEVELOPMENT_TEAM=", "CODE_SIGN_STYLE=Manual",
         "PROVISIONING_PROFILE_SPECIFIER="]
DEFAULT_OUT = Path.home() / "Desktop" / "xcode_local_builds"
# KEY=VALUE, with optional conditions: OTHER_LDFLAGS[sdk=macosx*][arch=arm64]=...
BUILD_SETTING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]=]+=[^\]]*\])*=")


# --- subprocesses -----------------------------------------------------------
# Every command runs without a shell, with stdin closed (the server's stdin is
# the MCP channel: a child must never read it) and in its own process group,
# so a timeout or a cancelled request can stop it together with its children.
# Output goes to files, never pipes: a grandchild that keeps a pipe open could
# otherwise block the read forever.

@dataclass
class Run:
    code: int | None   # exit status; negative = killed by that signal
    out: str
    err: str
    timed_out: bool
    seconds: float


def _killpg(pgid: int, sig: int) -> bool:
    """Send `sig` to a process group. False if the group no longer exists."""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def _reap_group(pgid: int) -> bool:
    """Stop whatever is left in a process group after its leader exited
    (children that outlived it). True if something was left."""
    if not _killpg(pgid, 0):
        return False
    _killpg(pgid, signal.SIGTERM)
    for _ in range(30):
        await anyio.sleep(0.1)
        if not _killpg(pgid, 0):
            return True
    _killpg(pgid, signal.SIGKILL)
    return True


async def _stop(proc, first: int = signal.SIGTERM) -> None:
    """Stop a process and its group: `first`, then SIGTERM, then SIGKILL.
    xcodebuild gets SIGINT first: it then cancels the build in its build
    service, whose script phases and compilers live in other process groups
    and would be orphaned by a plain kill."""
    for sig in dict.fromkeys((first, signal.SIGTERM, signal.SIGKILL)):
        if proc.returncode is not None:
            break
        _killpg(proc.pid, sig)
        with anyio.move_on_after(15 if sig == signal.SIGINT else 5):
            await proc.wait()
    await _reap_group(proc.pid)


async def _start(cmd: list[str], stdout, stderr, env: dict | None = None):
    try:
        return await anyio.open_process(cmd, stdin=subprocess.DEVNULL, stdout=stdout,
                                        stderr=stderr, env=env, start_new_session=True)
    except OSError as e:
        hint = " (is Xcode installed? check `xcode-select -p`)" if "/" not in cmd[0] else ""
        raise RuntimeError(f"cannot run {cmd[0]}: {e.strerror or e}{hint}") from e


async def _run(cmd: list[str], timeout: float = 1800, *, log: Path | None = None,
               first_signal: int = signal.SIGTERM) -> Run:
    """Run `cmd`; output to `log` (stdout+stderr interleaved) or captured.
    Never raises on timeout: the group is stopped and timed_out is set. If the
    MCP request is cancelled, the group is stopped before the cancellation
    propagates."""
    t0 = time.monotonic()
    with ExitStack() as files:
        if log is not None:
            out = files.enter_context(open(log, "wb"))
            err = subprocess.STDOUT
        else:
            out = files.enter_context(tempfile.TemporaryFile())
            err = files.enter_context(tempfile.TemporaryFile())
        proc = await _start(cmd, out, err)
        try:
            with anyio.move_on_after(timeout) as deadline:
                await proc.wait()
        finally:
            with anyio.CancelScope(shield=True):
                if proc.returncode is None:
                    await _stop(proc, first_signal)
                else:
                    await _reap_group(proc.pid)

        def text(f) -> str:
            if log is not None:
                return ""
            f.seek(0)
            return f.read().decode(errors="replace")

        return Run(proc.returncode, text(out), text(err), deadline.cancelled_caught,
                   round(time.monotonic() - t0, 1))


# --- helpers ----------------------------------------------------------------
# Inputs are checked before anything is run, copied or deleted, and errors say
# what was expected (the caller is usually an AI that will retry).

def _existing(path: str, what: str) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"{what} not found: {p}")
    return p


def _is_bundle(p: Path) -> bool:
    """A real .app folder (not a symlink) with an Info.plist."""
    return (p.suffix == ".app" and p.is_dir() and not p.is_symlink()
            and ((p / "Contents" / "Info.plist").is_file() or (p / "Info.plist").is_file()))


def _bundle(app_path: str) -> tuple[Path, dict]:
    """Check that app_path is an .app bundle and return it with its Info.plist."""
    app = _existing(app_path, "app")
    if not _is_bundle(app):
        raise ValueError(f"not an .app bundle (a folder ending in .app with an Info.plist): {app}")
    plist = app / "Contents" / "Info.plist" if (app / "Contents").is_dir() else app / "Info.plist"
    try:
        return app, plistlib.loads(plist.read_bytes())
    except Exception as e:
        raise ValueError(f"unreadable Info.plist in {app}: {e}") from e


def _container_args(project: str) -> list[str]:
    p = _existing(project, "project")
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


async def _entitlements(app: str, strict: bool = False) -> dict:
    r = await _run(["codesign", "-d", "--entitlements", "-", "--xml", app], 60)
    if strict and r.code:
        # e.g. "code object is not signed at all": not the same as "no entitlements"
        raise ValueError(f"codesign cannot read {app}: {r.err.strip()}")
    data = r.out.encode()
    if not data.strip():
        return {}
    try:
        return plistlib.loads(data)
    except Exception:
        return {"_raw": r.out}


NESTED_CODE = (".framework", ".dylib", ".appex", ".xpc", ".app")


def _nested_code(app: Path) -> list[Path]:
    """Code inside `app`, deepest first: each piece has to be signed before
    whatever contains it. Symlinks are skipped (their target is signed
    through its real path); os.walk does not follow them either."""
    found = []
    for root, dirs, files in os.walk(app):
        for name in dirs + files:
            p = Path(root) / name
            if name.endswith(NESTED_CODE) and not p.is_symlink():
                found.append(p)
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def _errors(text: str, limit: int = 25) -> list[str]:
    seen, out = set(), []
    for line in text.splitlines():
        if (re.search(r"\berror:|ld: (?!warning)|\*\* (BUILD|TEST) FAILED", line)
                and line not in seen):
            seen.add(line); out.append(line.strip())
    return out[:limit]


# --- tools ------------------------------------------------------------------

@mcp.tool()
async def signing_status(project: str | None = None) -> str:
    """Signing identities on this Mac and, if `project` is given (folder,
    .xcodeproj or .xcworkspace), its team, signing style and sandbox/network
    entitlement build settings."""
    out: dict = {}
    r = await _run(["security", "find-identity", "-v", "-p", "codesigning"], 30)
    out["identities"] = [l.strip() for l in r.out.splitlines() if '"' in l]
    out["identity_count"] = len(out["identities"])
    r = await _run(["xcodebuild", "-version"], 60)
    # With only the Command Line Tools selected this explains why, on stderr.
    out["xcode"] = r.out.strip() if r.code == 0 else f"error: {r.err.strip()}"
    if project:
        args = _container_args(project)
        r = await _run(["xcodebuild", *args, "-list", "-json"], 300)
        try:
            info = json.loads(r.out)
        except Exception:
            info = {"error": ("timed out after 300 s" if r.timed_out else r.err[-800:])}
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
async def build_local(project: str, scheme: str,
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
    if not 10 <= timeout_s <= 7200:
        raise ValueError("timeout_s must be between 10 and 7200 seconds")
    bad = [s for s in extra_settings or [] if not BUILD_SETTING.match(s)]
    if bad:
        # Anything else would reach xcodebuild as an action ('clean') or an option.
        raise ValueError("extra_settings only takes KEY=VALUE build settings, e.g. "
                         f"ENABLE_OUTGOING_NETWORK_CONNECTIONS=NO; refused: {bad}")
    args = _container_args(project)
    # scheme and destination become one folder name: no '/' or '..' survives.
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{scheme}_{destination}").strip("_")
    dd = Path(derived_data).expanduser() if derived_data else DEFAULT_OUT / slug
    dd.mkdir(parents=True, exist_ok=True)
    cmd = ["xcodebuild", *args, "-scheme", scheme, "-destination", destination,
           "-configuration", configuration, "-derivedDataPath", str(dd), "build",
           *ADHOC, *(extra_settings or [])]
    log = dd / "build_local.log"
    # The log is written while xcodebuild runs, so a timeout keeps what it had.
    r = await _run(cmd, timeout_s, log=log, first_signal=signal.SIGINT)
    text = log.read_text(errors="replace")
    ok = r.code == 0 and "** BUILD SUCCEEDED **" in text
    hints = []
    if r.timed_out:
        hints.append(f"timed out after {timeout_s} s: the build was interrupted "
                     "(partial log in `log`); raise timeout_s to wait longer")
    if "missing Metal Toolchain" in text:
        hints.append("run: xcodebuild -downloadComponent MetalToolchain")
    if "unknown file type" in text and ".a'" in text:
        hints.append("static libs look like Git LFS pointers: git lfs install --local && git lfs pull")
    if "was modified during the build" in text:
        hints.append("package resolution raced with Xcode: rerun")
    apps = sorted(str(p) for p in (dd / "Build" / "Products").glob(f"{glob.escape(configuration)}*/*.app"))
    res = {"ok": ok, "timed_out": r.timed_out, "exit_code": r.code, "seconds": r.seconds,
           "command": shlex.join(cmd), "log": str(log), "errors": _errors(text), "hints": hints,
           "apps": {a: await _entitlements(a) for a in apps} if ok else {}}
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def show_entitlements(app_path: str) -> str:
    """Entitlements actually signed into an .app (what the sandbox enforces)."""
    app = _existing(app_path, "app")
    return json.dumps(await _entitlements(str(app), strict=True), indent=2, default=str)


@mcp.tool()
async def resign_adhoc(app_path: str, add: dict | None = None, remove: list[str] | None = None,
                       copy_to: str | None = None) -> str:
    """Re-sign an .app ad-hoc keeping its entitlements, with keys added
    (`add`, e.g. {"com.apple.security.network.client": true}) or removed
    (`remove`). `copy_to` works on a copy so the original build stays intact.
    Nested code (frameworks, dylibs, extensions, XPC services, helper apps)
    is re-signed first, deepest first; extensions, XPC services and helper
    apps keep their own entitlements."""
    src, _ = _bundle(app_path)
    try:
        plistlib.dumps(add or {})
    except (TypeError, OverflowError) as e:
        raise ValueError("add: values must be plist types (true/false, strings, numbers, "
                         f"arrays, dictionaries); {e}") from e
    app = src
    if copy_to:
        app = Path(copy_to).expanduser()
        s, d = src.resolve(), app.resolve()
        if d.suffix != ".app":
            raise ValueError(f"copy_to must be a path ending in .app: {app}")
        if d == s or s in d.parents or d in s.parents:
            raise ValueError("copy_to must be outside app_path (omit copy_to to re-sign in place)")
        if app.exists() or app.is_symlink():
            # Only ever replace a previous copy, never an arbitrary folder.
            if not _is_bundle(app):
                raise ValueError(f"copy_to exists and is not an .app bundle, refusing to replace it: {app}")
            r = await _run(["rm", "-rf", str(app)], 300)
            if r.code:
                raise RuntimeError(f"cannot remove {app}: {r.err.strip()}")
        r = await _run(["ditto", str(src), str(app)], 600)
        if r.code:
            raise RuntimeError(f"ditto {src} -> {app} failed: {r.err.strip()}")
    ents = await _entitlements(str(app))
    ents.pop("_raw", None)
    for k in remove or []:
        ents.pop(k, None)
    ents.update(add or {})
    ents.setdefault("com.apple.security.get-task-allow", True)
    # Keep the original signature's flags. With hardened runtime, ad-hoc
    # nested dylibs fail Library Validation (no team ID to match), so allow
    # them — local runs only.
    dv = (await _run(["codesign", "-dv", str(app)], 60)).err
    runtime = bool(re.search(r"flags=0x[0-9a-f]+\([^)]*runtime", dv))
    if runtime:
        ents["com.apple.security.cs.disable-library-validation"] = True
    nested = _nested_code(app)
    logs = []
    for n in nested:
        # Extensions, XPC services and helper apps carry their own entitlements
        # (usually their own sandbox); re-signing without them breaks them.
        keep = ["--preserve-metadata=entitlements"] if n.suffix in (".appex", ".xpc", ".app") else []
        r = await _run(["codesign", "--force", "--sign", "-", "--timestamp=none", *keep, str(n)], 120)
        if r.code:
            logs.append(f"{n}: {r.err.strip()}")
    fd, ent_file = tempfile.mkstemp(suffix=".plist")
    try:
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(ents, f)
        r = await _run(["codesign", "--force", "--sign", "-", "--timestamp=none",
                        *(["--options", "runtime"] if runtime else []),
                        "--entitlements", ent_file, str(app)], 300)
    finally:
        os.unlink(ent_file)
    v = await _run(["codesign", "--verify", "--deep", "--strict", str(app)], 120)
    return json.dumps({"app": str(app), "signed": r.code == 0, "sign_stderr": r.err.strip(),
                       "verify_ok": v.code == 0, "verify": v.err.strip(),
                       "nested_signed": [str(n.relative_to(app)) for n in nested],
                       "nested_errors": logs, "entitlements": await _entitlements(str(app))},
                      indent=2, default=str)


@mcp.tool()
async def launch_with_trace(app_path: str, env: dict | None = None, seconds: int = 30,
                            console_filter: str | None = None,
                            evidence_dir: str | None = None,
                            log_predicate: str | None = None) -> str:
    """Run the app's executable directly (sandbox applies) with `env`, for at
    most `seconds` (it may exit sooner). Captures stdout/stderr (only lines
    containing `console_filter` if given) and, in parallel, system log lines
    from the kernel/Sandbox about this process (or `log_predicate`). Saves
    console.txt, system_log.txt and entitlements.json to `evidence_dir`."""
    if not 1 <= seconds <= 600:
        raise ValueError("seconds must be between 1 and 600")
    app, info = _bundle(app_path)
    platforms = info.get("CFBundleSupportedPlatforms") or []
    if any(p.endswith("Simulator") for p in platforms):
        bid = info.get("CFBundleIdentifier", "<bundle id>")
        raise ValueError(
            f"{app.name} is a Simulator build ({', '.join(platforms)}); it cannot run directly "
            f"on macOS. Run it in a booted simulator: xcrun simctl install booted "
            f"{shlex.quote(str(app))} && xcrun simctl launch --console booted {bid}. "
            "Or rebuild with destination 'platform=macOS,variant=Mac Catalyst' or 'platform=macOS'.")
    if "iPhoneOS" in platforms:
        raise ValueError(f"{app.name} is an iOS device build; rebuild it for "
                         "'platform=macOS,variant=Mac Catalyst' to run it on this Mac")
    exe_name = info.get("CFBundleExecutable")
    if not exe_name:
        raise ValueError(f"no CFBundleExecutable in the Info.plist of {app}")
    exe = app / "Contents" / "MacOS" / exe_name
    if not exe.exists():
        exe = app / exe_name
    if not exe.is_file() or not os.access(exe, os.X_OK):
        raise ValueError(f"executable missing or not executable: {exe} (incomplete build?)")
    env = {str(k): str(v) for k, v in (env or {}).items()}
    bad = [k for k, v in env.items() if not k or "=" in k or "\0" in k + v]
    if bad:
        raise ValueError(f"env: invalid variable names or values for {bad}")
    out = Path(evidence_dir).expanduser() if evidence_dir else DEFAULT_OUT / "evidence" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    quoted = exe_name.replace("\\", "\\\\").replace('"', '\\"')   # NSPredicate string literal
    pred = log_predicate or (f'(process == "kernel" OR sender == "Sandbox" OR process == "sandboxd") '
                             f'AND eventMessage CONTAINS "{quoted}" '
                             f'AND NOT eventMessage CONTAINS "ReportCrash"')
    full_env = {**os.environ, **env}
    header = "$ " + " ".join([*(f"{k}={shlex.quote(v)}" for k, v in env.items()), shlex.quote(str(exe))]) + "\n"
    console_full = out / "console_full.txt"
    t0 = time.monotonic()
    children_killed = False
    with open(out / "system_log.txt", "wb") as logf, open(console_full, "wb") as conf:
        logp = await _start(["/usr/bin/log", "stream", "--style", "compact", "--predicate", pred],
                            logf, subprocess.STDOUT)
        p = None
        try:
            await anyio.sleep(2)
            t0 = time.monotonic()
            # The app writes straight to the file: if it leaves a child holding
            # its stdout, nothing here waits for that child.
            p = await _start([str(exe)], conf, subprocess.STDOUT, full_env)
            with anyio.move_on_after(seconds):
                await p.wait()
            if p.returncode is None:
                exit_code = "timeout (terminated)"
                await _stop(p)
            else:
                exit_code = p.returncode
                children_killed = await _reap_group(p.pid)
            await anyio.sleep(2)
        finally:
            with anyio.CancelScope(shield=True):
                if p is not None and p.returncode is None:
                    await _stop(p)
                await _stop(logp)
    elapsed = round(time.monotonic() - t0, 1)
    lines = console_full.read_text(errors="replace").splitlines()
    if console_filter:
        lines = [l for l in lines if console_filter in l]
    (out / "console.txt").write_text(header + "\n".join(lines) + "\n")
    ents = await _entitlements(str(app))
    (out / "entitlements.json").write_text(json.dumps(ents, indent=2, default=str))
    syslog = [l for l in (out / "system_log.txt").read_text(errors="replace").splitlines()
              if l and not l.startswith(("Filtering", "Timestamp"))]
    return json.dumps({"evidence_dir": str(out), "exit": exit_code,
                       "seconds": elapsed, "children_killed": children_killed,
                       "entitlements": ents, "console": lines[-60:], "system_log": syslog[-40:]},
                      indent=2, ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()
