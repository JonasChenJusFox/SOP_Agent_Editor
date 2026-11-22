"""Run the Python backend through a Cloudflare Tunnel and deploy its frontend.

Usage: python scripts/cloudflare.py up
       python scripts/cloudflare.py deploy --origin https://api.example.com
Wrangler uses your own Cloudflare login. Application secrets never enter its env.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "private"
LOGS = ROOT / "runs" / "cloudflare-logs"
CLOUDFLARED_VERSION = "2026.11.1"


def backend_environment():
    env = dict(os.environ)
    path = ROOT / ".env"
    allowed = {"OPENAI_API_KEY", "OPENAI_MODEL", "ADMISSIONS_WEB_PASSWORD"}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if match and match[1] in allowed and match[1] not in env:
                parts = shlex.split(match[2], comments=True)
                if len(parts) != 1:
                    raise ValueError("A required .env setting is invalid; use one quoted value per line.")
                env[match[1]] = parts[0]
    return env


def cloudflare_environment():
    LOGS.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if key not in {"OPENAI_API_KEY", "OPENAI_MODEL", "ADMISSIONS_WEB_PASSWORD"}}
    env.update(WRANGLER_LOG_PATH=str(LOGS), WRANGLER_SEND_METRICS="false", CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV="false")
    return env


def wrangler(*args, stream=False):
    executable = ROOT / "node_modules/wrangler/bin/wrangler.js"
    if not executable.is_file():
        raise ValueError("Run npm ci before using the Cloudflare commands.")
    result = subprocess.run(["node", str(executable), *args, "--env-file", str(ROOT / "cloudflare/.env.example")],
                            cwd=ROOT, env=cloudflare_environment(), stdout=None if stream else subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    result.check_returncode()
    return result


def deploy(origin=None, dry_run=False):
    subprocess.run(["node", str(ROOT / "scripts/build_web.mjs")], cwd=ROOT, env=cloudflare_environment(), check=True)
    args = ["deploy", "--config", str(ROOT / "cloudflare/wrangler.tunnel.jsonc")]
    if origin:
        from urllib.parse import urlsplit
        url = urlsplit(origin)
        if url.scheme != "https" or not url.netloc or url.path not in {"", "/"} or url.query or url.fragment or url.username:
            raise ValueError("--origin must be an HTTPS origin without a path or credentials.")
        args += ["--var", "API_ORIGIN:" + origin.rstrip("/")]
    if dry_run:
        args += ["--dry-run", "--outdir", str(ROOT / "runs/cloudflare-build")]
    result = wrangler(*args)
    url = re.search(r"https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", result.stdout)
    if not dry_run and url:
        PRIVATE.mkdir(mode=0o700, exist_ok=True)
        version = re.search(r"Current Version ID:\s*([a-f0-9-]+)", result.stdout)
        (PRIVATE / "cloudflare-deployment.json").write_text(json.dumps({"url": url[0], "api_origin": origin,
              "version_id": version[1] if version else None}, indent=2) + "\n")
        return url[0]
    return None


def cloudflared():
    import shutil
    existing = shutil.which("cloudflared")
    if existing:
        return Path(existing)
    folder = ROOT / ".tools" / "cloudflared"
    executable = folder / "cloudflared"
    if executable.is_file():
        return executable
    system, machine = platform.system(), platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64" if machine in {"x86_64", "amd64"} else None
    if not arch or system not in {"Darwin", "Linux"}:
        raise ValueError("Install cloudflared for your operating system first.")
    name = f"cloudflared-{'darwin' if system == 'Darwin' else 'linux'}-{arch}" + (".tgz" if system == "Darwin" else "")
    request = urllib.request.Request("https://api.github.com/repos/cloudflare/cloudflared/releases/tags/" + CLOUDFLARED_VERSION, headers={"User-Agent": "admissions-editor-setup"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((item for item in release["assets"] if item["name"] == name), None)
    if asset is None:
        raise ValueError("No matching cloudflared release. Install it using the official instructions.")
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / name
    print(f"Installing official cloudflared {release['tag_name']} into .tools/cloudflared", flush=True)
    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, archive.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = asset.get("digest")
    if expected and expected != "sha256:" + digest:
        archive.unlink()
        raise ValueError("cloudflared download checksum mismatch.")
    if system == "Darwin":
        with tarfile.open(archive) as bundle:
            member = next((m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
            if member is None:
                raise ValueError("The cloudflared archive has no executable.")
            with bundle.extractfile(member) as source, executable.open("wb") as target:
                shutil.copyfileobj(source, target)
    else:
        archive.replace(executable)
    executable.chmod(0o755)
    (folder / "release.json").write_text(json.dumps({"version": release["tag_name"], "asset": name, "sha256": digest,
                                                    "publisher_digest_verified": bool(expected)}, indent=2) + "\n")
    return executable


def password_file(env):
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    path = PRIVATE / "web-password.txt"
    if not path.exists():
        password = env.get("ADMISSIONS_WEB_PASSWORD", "")
        if not password or password.startswith(("REPLACE_", "YOUR_")):
            password = secrets.token_urlsafe(32)
        if len(password) < 12:
            raise ValueError("The web access password must contain at least 12 characters.")
        with path.open("x", encoding="utf-8") as target:
            path.chmod(0o600)
            target.write(password + "\n")
    current = path.read_text().strip()
    if len(current) < 12 or current.startswith(("REPLACE_", "YOUR_")):
        raise ValueError("Set a real access password in private/web-password.txt.")
    return path


def up(args):
    executable = cloudflared()
    env = backend_environment()
    password = password_file(env)
    LOGS.mkdir(parents=True, exist_ok=True)
    backend_log = (LOGS / "backend.log").open("a", encoding="utf-8")
    tunnel_log = (LOGS / "tunnel.log").open("w", encoding="utf-8")
    processes = []
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
        command = [sys.executable, "-u", "-m", "admissions_editor", "serve", "--host", "127.0.0.1", "--port", str(args.port),
                   "--config", str(ROOT / args.config), "--password-file", str(password), "--data-dir", str(ROOT / "runs/web")]
        backend = subprocess.Popen(command, cwd=ROOT, env=env, stdout=backend_log, stderr=subprocess.STDOUT)
        processes.append(backend)
        for _ in range(80):
            if backend.poll() is not None:
                raise ValueError("The backend did not start; inspect runs/cloudflare-logs/backend.log.")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/session", timeout=1) as response:
                    if json.load(response).get("password_required"):
                        break
            except OSError:
                time.sleep(.25)
        else:
            raise ValueError("Timed out waiting for the Python backend.")
        tunnel = subprocess.Popen([str(executable), "tunnel", "--url", f"http://127.0.0.1:{args.port}", "--protocol", "http2", "--no-autoupdate"],
                                  cwd=ROOT, stdout=tunnel_log, stderr=subprocess.STDOUT)
        processes.append(tunnel)
        origin = None
        for _ in range(180):
            if tunnel.poll() is not None:
                raise ValueError("Tunnel did not start; inspect runs/cloudflare-logs/tunnel.log.")
            text = (LOGS / "tunnel.log").read_text(errors="replace")
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
            if match:
                origin = match[0]
                break
            time.sleep(.25)
        if not origin:
            raise ValueError("Timed out waiting for a Cloudflare Tunnel URL.")
        state = {"backend_port": args.port, "api_origin": origin, "temporary_tunnel": True,
                 "password_file": str(password.relative_to(ROOT)), "backend_pid": backend.pid, "tunnel_pid": tunnel.pid}
        (PRIVATE / "cloudflare-runtime.json").write_text(json.dumps(state, indent=2) + "\n")
        print(f"Backend tunnel: {origin}", flush=True)
        print("Access password is stored in private/web-password.txt; it is not printed.", flush=True)
        direct_verified = False
        for _ in range(8):
            try:
                request = urllib.request.Request(origin + "/api/session", headers={"User-Agent": "AdmissionsEditorHealth/0.3", "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    if json.load(response).get("password_required"):
                        direct_verified = True
                        break
            except (OSError, ValueError):
                time.sleep(1)
        if not direct_verified:
            print("Direct Tunnel DNS is not yet verified from this Mac; checking through the deployed Worker next.", flush=True)
        if not args.no_deploy:
            frontend = deploy(origin)
            verified = False
            if frontend:
                for _ in range(15):
                    try:
                        request = urllib.request.Request(frontend + "/api/session", headers={"User-Agent": "AdmissionsEditorHealth/0.3", "Accept": "application/json"})
                        with urllib.request.urlopen(request, timeout=4) as response:
                            if json.load(response).get("password_required"):
                                verified = True
                                break
                    except (OSError, ValueError):
                        time.sleep(1)
            print("Frontend-to-backend connection: " + ("verified" if verified else "not verified; inspect the local Tunnel log"), flush=True)
        print("Backend connection is running. Keep this process and computer on; Ctrl+C stops the connection.", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        raise ValueError("A backend or Tunnel process stopped. Run the up command again.")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
        backend_log.close()
        tunnel_log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up", help="Run a password-protected backend, open a temporary Tunnel and deploy the frontend.")
    start.add_argument("--port", type=int, default=8789)
    start.add_argument("--config", default="examples/online.toml")
    start.add_argument("--no-deploy", action="store_true")
    publish = commands.add_parser("deploy", help="Deploy static frontend and optionally connect an existing HTTPS backend.")
    publish.add_argument("--origin")
    publish.add_argument("--dry-run", action="store_true")
    preview = commands.add_parser("dev", help="Preview the Cloudflare Worker without loading application secrets.")
    preview.add_argument("--origin")
    commands.add_parser("install-tunnel", help="Install the official cloudflared binary into the project.")
    args = parser.parse_args()
    try:
        if args.command == "up":
            up(args)
        elif args.command == "deploy":
            deploy(args.origin, args.dry_run)
        elif args.command == "dev":
            options = ["dev", "--config", str(ROOT / "cloudflare/wrangler.tunnel.jsonc")]
            if args.origin:
                options += ["--var", "API_ORIGIN:" + args.origin]
            wrangler(*options, stream=True)
        else:
            print(cloudflared())
    except KeyboardInterrupt:
        print("Cloudflare backend connection stopped.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Native tools already emit sanitized errors; do not dump environments or credentials.
        print(f"Cloudflare setup did not complete: {type(error).__name__}. Check the local logs and configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
