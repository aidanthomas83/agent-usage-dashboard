#!/usr/bin/env python3
"""Fail if tracked repository files look like local telemetry or secrets."""
from __future__ import annotations
import fnmatch
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PREFIXES = ("data/","preview-data/","test-output/",".codex/")
FORBIDDEN_GLOBS = ("*.jsonl","*.sqlite","*.sqlite3","*.db","*.db-wal","*.db-shm",".env",".env.*","*.pem","*.key","*.pfx","*.p12")
ALLOWED_SENSITIVE_FILENAMES = {".env.example"}
SECRET_PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub classic PAT": re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained PAT": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Bearer credential": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b", re.I),
}
LOCAL_PATH_PATTERNS = {
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\(?!%USERPROFILE%)[^\\\r\n]+", re.I),
    "Unix home path": re.compile(r"/(?:home|Users)/[^/\s]+/"),
}
def tracked_files():
    try:
        out=subprocess.check_output(["git","ls-files","-z"],cwd=ROOT,stderr=subprocess.DEVNULL)
        names=[p for p in out.decode("utf-8","replace").split("\0") if p]
        if names: return [ROOT/p for p in names]
    except (OSError,subprocess.CalledProcessError): pass
    return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
def main():
    failures=[]
    for path in tracked_files():
        rel=path.relative_to(ROOT).as_posix(); low=rel.lower()
        if any(low.startswith(p) for p in FORBIDDEN_PREFIXES):
            failures.append(f"forbidden generated/local path: {rel}"); continue
        if low not in ALLOWED_SENSITIVE_FILENAMES and any(fnmatch.fnmatch(low,p.lower()) for p in FORBIDDEN_GLOBS):
            failures.append(f"forbidden sensitive file type: {rel}"); continue
        try: raw=path.read_bytes()
        except OSError as exc:
            failures.append(f"could not read {rel}: {exc}"); continue
        if b"\x00" in raw: continue
        txt=raw.decode("utf-8","replace")
        for label,pat in SECRET_PATTERNS.items():
            if pat.search(txt): failures.append(f"possible {label} in {rel}")
        if rel!="scripts/check_repo_safety.py":
            for label,pat in LOCAL_PATH_PATTERNS.items():
                if pat.search(txt): failures.append(f"possible {label} in {rel}")
    if failures:
        print("Repository safety check FAILED:")
        for item in sorted(set(failures)): print(f" - {item}")
        return 1
    print("Repository safety check passed: no forbidden telemetry files or obvious credentials found.")
    return 0
if __name__=="__main__": raise SystemExit(main())
