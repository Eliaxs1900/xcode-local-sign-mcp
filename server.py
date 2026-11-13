# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2"]
# ///
"""xcode-local-sign-mcp: build, sign and run Xcode apps on this Mac WITHOUT an
Apple developer certificate, and capture a log trace as proof.

Why: a cloned project carries its author's DEVELOPMENT_TEAM, and this Mac has
no signing identity, so a plain `xcodebuild` fails or produces an app the
sandbox will not run. Ad-hoc signing ("-") is enough to build and launch
locally, with the app's real entitlements and App Sandbox enforced.

Tools (their descriptions and parameter docs below are what the AI client sees)
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
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

INSTRUCTIONS = """\
Builds, signs and runs Xcode apps on this Mac without an Apple developer
certificate (ad-hoc signing), and records what the App Sandbox did.

Typical flow:
1. signing_status(project): schemes, team and sandbox settings.
2. build_local(project, scheme): ad-hoc build; returns the .app and its entitlements.
3. To test one permission: resign_adhoc(app, add=... or remove=..., copy_to=...)
   gives a copy with and a copy without it.
4. launch_with_trace(app, env, seconds): runs it and returns its console and
   the kernel's sandbox log lines; everything is saved in an evidence folder.

Ad-hoc apps only run on this Mac. XCTest/XCUITest still need a development
certificate (the user signs in to Xcode with an Apple ID)."""

mcp = FastMCP("xcode-local-sign", instructions=INSTRUCTIONS)

ADHOC = ["CODE_SIGN_IDENTITY=-", "DEVELOPMENT_TEAM=", "CODE_SIGN_STYLE=Manual",
         "PROVISIONING_PROFILE_SPECIFIER="]
DEFAULT_OUT = Path.home() / "Desktop" / "xcode_local_builds"
# KEY=VALUE, with optional conditions: OTHER_LDFLAGS[sdk=macosx*][arch=arm64]=...
BUILD_SETTING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]=]+=[^\]]*\])*=")
# Replies stay small: lines are cut to this length and only the tail of long
# outputs is returned. Everything is kept in full in the log/evidence files.
MAX_LINE = 400


def _clip(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= MAX_LINE else f"{line[:MAX_LINE]}… [+{len(line) - MAX_LINE} chars]"


def _tail(lines: list[str], n: int) -> list[str]:
    return [_clip(l) for l in lines[-n:]]


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


def _errors(text: str) -> list[str]:
    """Distinct error lines of an xcodebuild log, in order. `\\bld:` so that
    "xcodebuild: WARNING" is not taken for a linker error."""
    seen, out = set(), []
    for line in text.splitlines():
        line = line.strip()
        if (re.search(r"\berror:|\bld: (?!warning)|\*\* (BUILD|TEST) (FAILED|INTERRUPTED)", line)
                and line not in seen):
            seen.add(line)
            out.append(line)
    return out


# --- tools ------------------------------------------------------------------

PROJECT_DOC = ("Folder that contains the project, or the path of a .xcodeproj or "
               ".xcworkspace (a folder with both uses the workspace).")


@mcp.tool(annotations=ToolAnnotations(title="Signing status", readOnlyHint=True))
async def signing_status(
    project: Annotated[str | None, Field(description=PROJECT_DOC + " Omit it to only check this Mac.")] = None,
) -> str:
    """Check how this Mac and a project are set up for signing, before building.
    Returns JSON: the code-signing identities in the keychain (usually none,
    which is why build_local signs ad-hoc), the Xcode version and, with
    `project`, its schemes and targets (xcodebuild -list) plus the signing and
    sandbox build settings in its project files (DEVELOPMENT_TEAM,
    CODE_SIGN_*, ENABLE_APP_SANDBOX, ENABLE_*NETWORK*...). Changes nothing."""
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


@mcp.tool(annotations=ToolAnnotations(title="Build with ad-hoc signing", readOnlyHint=False,
                                      destructiveHint=False, idempotentHint=True))
async def build_local(
    project: Annotated[str, Field(description=PROJECT_DOC)],
    scheme: Annotated[str, Field(description="Scheme to build; signing_status(project) lists them.")],
    destination: Annotated[str, Field(description=(
        "xcodebuild -destination: 'platform=macOS,variant=Mac Catalyst' (iPad apps on the Mac), "
        "'platform=macOS' (native Mac apps) or 'generic/platform=iOS Simulator' (only builds: "
        "launch_with_trace cannot run Simulator apps)."))] = "platform=macOS,variant=Mac Catalyst",
    configuration: Annotated[str, Field(description="Build configuration, usually Debug or Release.")] = "Debug",
    derived_data: Annotated[str | None, Field(description=(
        "Folder for the build products and build_local.log. Default: "
        "~/Desktop/xcode_local_builds/<scheme>_<destination>, reused so rebuilds are incremental."))] = None,
    extra_settings: Annotated[list[str] | None, Field(description=(
        "Extra build settings, each KEY=VALUE, e.g. ['ENABLE_OUTGOING_NETWORK_CONNECTIONS=NO']. "
        "Actions or options such as 'clean' or '-quiet' are refused."))] = None,
    timeout_s: Annotated[int, Field(ge=10, le=7200, description="Stop the build after this many seconds.")] = 1800,
) -> str:
    """Build a scheme with ad-hoc signing: no certificate needed (team and
    provisioning profile are cleared), so a cloned project builds and runs on
    this Mac with its real entitlements and App Sandbox.
    Returns JSON: ok, timed_out, exit_code, seconds, the exact xcodebuild
    command, the log path, up to 25 error lines (errors_total), hints for
    common failures and the built .app paths with their signed entitlements.
    May take minutes; on timeout or cancellation the build is stopped cleanly."""
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
    errors = _errors(text)
    res = {"ok": ok, "timed_out": r.timed_out, "exit_code": r.code, "seconds": r.seconds,
           "command": shlex.join(cmd), "log": str(log),
           "errors": [_clip(e) for e in errors[:25]], "errors_total": len(errors), "hints": hints,
           "apps": {a: await _entitlements(a) for a in apps} if ok else {}}
    if not ok and not any("error:" in e for e in errors):
        # Failed without a recognisable error line: show how the log ends.
        res["log_tail"] = _tail(text.splitlines(), 30)
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


@mcp.tool(annotations=ToolAnnotations(title="Show signed entitlements", readOnlyHint=True,
                                      openWorldHint=False))
async def show_entitlements(
    app_path: Annotated[str, Field(description="The .app bundle, e.g. one returned by build_local or resign_adhoc.")],
) -> str:
    """Entitlements signed into an .app: what the App Sandbox and the system
    actually enforce, which can differ from the project's .entitlements file.
    Returns them as JSON ({} means signed without entitlements). Fails if the
    path does not exist or is not signed. Changes nothing."""
    app = _existing(app_path, "app")
    return json.dumps(await _entitlements(str(app), strict=True), indent=2, default=str)


@mcp.tool(annotations=ToolAnnotations(title="Re-sign ad-hoc with other entitlements", readOnlyHint=False,
                                      destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def resign_adhoc(
    app_path: Annotated[str, Field(description="The .app to re-sign.")],
    add: Annotated[dict[str, Any] | None, Field(description=(
        'Entitlements to add or overwrite, e.g. {"com.apple.security.network.client": true}. '
        "Values: true/false, strings, numbers, arrays or dictionaries."))] = None,
    remove: Annotated[list[str] | None, Field(description=(
        'Entitlement keys to remove, e.g. ["com.apple.security.network.client"].'))] = None,
    copy_to: Annotated[str | None, Field(description=(
        "Work on a copy at this path (ending in .app, outside app_path; an earlier copy there is "
        "replaced). Recommended: without it the app is changed in place."))] = None,
) -> str:
    """Re-sign an .app ad-hoc with entitlements added or removed, to test one
    sandbox permission without rebuilding (A/B: a copy with it, a copy
    without). The other entitlements are kept, plus get-task-allow.
    Nested code (frameworks, dylibs, extensions, XPC services, helper apps)
    is re-signed first, deepest first; extensions, XPC services and helper
    apps keep their own entitlements. With the hardened runtime on,
    disable-library-validation is added so ad-hoc libraries load (local
    runs only). Returns JSON: the app path, signed, verify_ok (codesign
    --verify --deep --strict), the entitlements now signed and any nested
    signing errors."""
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
                       "nested_signed": [str(n.relative_to(app)) for n in nested[:50]],
                       "nested_signed_total": len(nested),
                       "nested_errors": [_clip(l) for l in logs[:20]],
                       "entitlements": await _entitlements(str(app))},
                      indent=2, default=str)


@mcp.tool(annotations=ToolAnnotations(title="Run the app and record a trace", readOnlyHint=False,
                                      destructiveHint=False, idempotentHint=False))
async def launch_with_trace(
    app_path: Annotated[str, Field(description="The .app to run: a macOS or Mac Catalyst build.")],
    env: Annotated[dict[str, str | int | float | bool] | None, Field(description=(
        'Environment variables for the app, e.g. {"MY_DEBUG_FLAG": "1"}.'))] = None,
    seconds: Annotated[int, Field(ge=1, le=600, description="Maximum run time; the app may exit sooner.")] = 30,
    console_filter: Annotated[str | None, Field(description=(
        "Only return console lines that contain this text (the full console is saved anyway)."))] = None,
    evidence_dir: Annotated[str | None, Field(description=(
        "Folder for the evidence files. Default: a new folder under "
        "~/Desktop/xcode_local_builds/evidence."))] = None,
    log_predicate: Annotated[str | None, Field(description=(
        "Custom `log stream --predicate` (NSPredicate) instead of the default: kernel and "
        "Sandbox messages that mention the executable."))] = None,
) -> str:
    """Run an .app's executable directly, with its App Sandbox, for at most
    `seconds`, and record what happened (e.g. to prove that the sandbox
    blocks something). Captures the app's stdout/stderr and, at the same
    time, kernel/Sandbox log lines that mention it ("Sandbox: App(pid)
    deny(1) network-outbound ..."). When it ends or time is up, the app and
    any process it left behind are stopped.
    Returns JSON: exit (exit code or "timeout (terminated)"; exit_signal if
    it was killed), the last 60 console lines, the last 40 system log lines,
    totals, hints and the evidence folder (console_full.txt, console.txt,
    system_log.txt, entitlements.json).
    An empty system_log does not prove nothing was denied: macOS drops the
    report if the app exits right after the violation and stops reporting
    repeated ones for a while. The app's own error in the console is the
    first evidence. Simulator builds cannot be run here."""
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
    if evidence_dir:
        out = Path(evidence_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
    else:   # one new folder per launch: two launches in the same second do not mix
        root = DEFAULT_OUT / "evidence"
        root.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9]+", "_", exe_name)
        out = Path(tempfile.mkdtemp(prefix=f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-", dir=root))
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
    all_lines = console_full.read_text(errors="replace").splitlines()
    lines = [l for l in all_lines if console_filter in l] if console_filter else all_lines
    (out / "console.txt").write_text(header + "\n".join(lines) + "\n")
    ents = await _entitlements(str(app))
    (out / "entitlements.json").write_text(json.dumps(ents, indent=2, default=str))
    syslog = [l for l in (out / "system_log.txt").read_text(errors="replace").splitlines()
              if l and not l.startswith(("Filtering", "Timestamp"))]
    res = {"evidence_dir": str(out), "exit": exit_code,
           "seconds": elapsed, "children_killed": children_killed, "entitlements": ents,
           "console": _tail(lines, 60), "console_lines_total": len(all_lines),
           "console_full": str(console_full),
           "system_log": _tail(syslog, 40), "system_log_lines_total": len(syslog)}
    if console_filter:
        res["console_lines_matched"] = len(lines)
    hints = []
    if isinstance(exit_code, int) and exit_code < 0:
        try:
            res["exit_signal"] = signal.Signals(-exit_code).name
        except ValueError:
            res["exit_signal"] = f"signal {-exit_code}"
        if -exit_code == signal.SIGKILL:
            hints.append("killed by the system (SIGKILL). Right at launch this usually means the "
                         "signature or entitlements were rejected: restricted ones (com.apple.developer.*, "
                         "keychain-access-groups...) need a provisioning profile. Check show_entitlements "
                         "and remove them with resign_adhoc.")
    res["hints"] = hints
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()
