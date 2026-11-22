"""Deploy the complete application to Cloudflare Python Workers + Workflows + D1."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from cloudflare import ROOT, PRIVATE, backend_environment, cloudflare_environment, password_file

PROJECT = ROOT / "cloudflare"
STATE = PRIVATE / "cloudflare-cloud.json"
LOCAL = PROJECT / "wrangler.local.jsonc"


def environment():
    result = cloudflare_environment()
    result["PATH"] = str(ROOT / ".tools/uv/bin") + os.pathsep + result.get("PATH", "")
    return result


def run(command, *, cwd=ROOT, input=None):
    result = subprocess.run(command, cwd=cwd, env=environment(), input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    result.check_returncode()
    return result.stdout


def wrangler(*args, input=None):
    return run(["node", str(ROOT / "node_modules/wrangler/bin/wrangler.js"), *args,
                "--config", str(LOCAL if LOCAL.exists() else PROJECT / "wrangler.jsonc"),
                "--env-file", str(PROJECT / ".env.example")], input=input)


def settings(path):
    sys.path.insert(0, str(ROOT / "src"))
    from admissions_editor.config import load_config
    config = load_config(path)
    # Native Workers cannot read the operator's local source directories. Keep
    # local knowledge paths out of deployment settings and report them missing.
    config.pop("knowledge", None)
    for item in [config.get("review", {}), config.get("workflow", {}), *config.get("roles", {}).values(),
                 *(config.get(section, {}) for section in ("revision", "reviser", "polisher", "humanizer", "revision_verifier"))]:
        if item.get("provider", "openai") != "openai" or "fixtures" in item:
            raise ValueError("Cloud configuration requires OpenAI providers; built-in demo remains available.")
    return config


def prepare(args):
    run(["node", str(ROOT / "scripts/build_web.mjs")])
    folder = PROJECT / "python"
    package = folder / "admissions_editor"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(ROOT / "src/admissions_editor", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "web"))
    examples = folder / "examples"
    if examples.exists():
        shutil.rmtree(examples)
    examples.mkdir()
    for name in ("draft.txt", "requirements.txt", "preferences.txt", "claims.json", "sources.json"):
        shutil.copyfile(ROOT / "examples" / name, examples / name)
    for name in ("fixtures", "evidence", "revision"):
        shutil.copytree(ROOT / "examples" / name, examples / name)
    (folder / "settings.json").write_text(json.dumps(settings(ROOT / args.config), indent=2) + "\n")
    config = json.loads((PROJECT / "wrangler.jsonc").read_text())
    config["name"] = args.name
    config["workflows"][0]["name"] = args.name + "-review"
    if STATE.exists():
        config["d1_databases"][0]["database_id"] = json.loads(STATE.read_text())["database_id"]
    LOCAL.write_text(json.dumps(config, indent=2) + "\n")
    print("Prepared cloud bundle: application code, synthetic examples, model settings; no secrets.", flush=True)


def init_db(args):
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    output = wrangler("d1", "list", "--json")
    databases = json.loads(output)
    matches = [d for d in databases if d["name"] == "admissions-editor"]
    if not matches:
        wrangler("d1", "create", "admissions-editor", "--update-config=false")
        matches = [d for d in json.loads(wrangler("d1", "list", "--json")) if d["name"] == "admissions-editor"]
    if len(matches) != 1:
        raise ValueError("Could not identify the project D1 database.")
    existing = json.loads(STATE.read_text()) if STATE.exists() else {}
    STATE.write_text(json.dumps({**existing, "database_name": "admissions-editor", "database_id": matches[0]["uuid"]}, indent=2) + "\n")
    STATE.chmod(0o600)
    prepare(args)
    wrangler("d1", "migrations", "apply", "DB", "--remote")


def tools_setup():
    uv = ROOT / ".tools/uv/bin/uv"
    if not uv.exists():
        run([sys.executable, "-m", "pip", "install", "--target", str(ROOT / ".tools/uv"), "uv==0.13.4"])
    run([str(uv), "sync", "--frozen"], cwd=PROJECT)


def secrets_upload():
    env = backend_environment()
    password = password_file(env).read_text().strip()
    secrets = {key: env.get(key, "").strip() for key in ("OPENAI_API_KEY", "OPENAI_MODEL")}
    if any(not v or v.startswith(("REPLACE_", "YOUR_")) for v in secrets.values()):
        raise ValueError("Set real OPENAI_API_KEY and OPENAI_MODEL in .env first.")
    secrets["ADMISSIONS_WEB_PASSWORD"] = password
    # Secret values travel over stdin, never command arguments or printed files.
    wrangler("secret", "bulk", input=json.dumps(secrets))


def deploy(args, dry_run=False):
    prepare(args)
    command = [str(PROJECT / ".venv/bin/pywrangler"), "deploy", "--config", str(LOCAL),
               "--env-file", str(PROJECT / ".env.example")]
    if dry_run:
        command += ["--dry-run", "--outdir", str(ROOT / "runs/cloud-native-build")]
    output = run(command, cwd=PROJECT)
    if not dry_run:
        url = re.search(r"https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", output)
        version = re.search(r"Current Version ID:\s*([a-f0-9-]+)", output)
        value = {**json.loads(STATE.read_text()), "url": url[0] if url else None, "worker": args.name,
                 "workflow": args.name + "-review", "version_id": version[1] if version else None,
                 "deployment": "cloudflare-native", "requires_local_backend": False}
        value["last_code_version_id"] = value["version_id"]
        value.pop("active_deployment_id", None)  # A new deployment supersedes the previous reference.
        STATE.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "prepare", "init", "check", "deploy", "secrets", "qa-secret", "migrate", "redirect-legacy"))
    parser.add_argument("--name", default="admissions-editor-cloud")
    parser.add_argument("--config", default="examples/online.toml")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,55}", args.name):
        parser.error("Invalid Worker name")
    try:
        if args.command == "setup":
            tools_setup()
        elif args.command == "prepare":
            prepare(args)
        elif args.command == "redirect-legacy":
            target = json.loads(STATE.read_text())["url"]
            if not target or "admissions-editor-cloud." not in target:
                raise ValueError("Deploy and verify the native cloud Worker first.")
            run(["node", str(ROOT / "node_modules/wrangler/bin/wrangler.js"), "deploy",
                 "--config", str(PROJECT / "wrangler.redirect.jsonc"), "--var", "TARGET_ORIGIN:" + target,
                 "--env-file", str(PROJECT / ".env.example")])
        elif args.command == "init":
            init_db(args)
        elif args.command == "secrets":
            prepare(args)
            secrets_upload()
        elif args.command == "qa-secret":
            if args.name != "admissions-editor-cloud":
                raise ValueError("QA credentials may only be used on the separate candidate Worker.")
            prepare(args)
            password = secrets.token_urlsafe(32)
            path = PRIVATE / "cloud-qa-password.txt"
            path.write_text(password + "\n")
            path.chmod(0o600)
            wrangler("secret", "bulk", input=json.dumps({"ADMISSIONS_WEB_PASSWORD": password}))
            print("Candidate uses a newly generated synthetic QA password only; no .env values were read or uploaded.")
        elif args.command == "migrate":
            prepare(args)
            wrangler("d1", "migrations", "apply", "DB", "--remote")
        elif args.command == "check":
            deploy(args, dry_run=True)
        else:
            if not STATE.exists():
                raise ValueError("Run cloud_deploy.py init first.")
            deploy(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Cloud setup failed ({type(error).__name__}); inspect the command output and local configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
