"""Deployment and registration contracts without contacting Telegram or ngrok."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_exposure_and_credential_contracts():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    for service in services.values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:")
        assert service["logging"]["options"]["max-file"] == "3"
    assert services["ngrok"]["networks"] == ["telegram-edge"]
    assert services["telegram-ingress"]["networks"] == ["telegram-upstream", "telegram-edge"]
    assert "telegram-upstream" in services["api"]["networks"]
    assert compose["networks"]["telegram-upstream"]["internal"]
    assert "api:8000" not in services["ngrok"]["command"]
    assert "--authtoken" not in services["ngrok"]["command"]
    assert "--inspect=false" in services["ngrok"]["command"]
    ingress = services["telegram-ingress"]
    assert "@sha256:" in ingress["image"]
    assert ingress["user"] == "101:101" and ingress["read_only"]
    assert "ports" not in ingress and "environment" not in ingress
    assert services["worker"].get("ports") is None
    mounts = services["worker"]["volumes"]
    auth_mounts = [mount for mount in mounts if isinstance(mount, dict)]
    assert len(auth_mounts) == 2
    assert all(
        mount["read_only"] and not mount["bind"]["create_host_path"] for mount in auth_mounts
    )
    assert {mount["target"] for mount in auth_mounts} == {
        "/root/.codex/auth.json",
        "/root/.gemini/antigravity-cli/antigravity-oauth-token",
    }


def test_build_context_excludes_secret_variants():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    for pattern in (
        ".env*",
        "**/.env*",
        "**/auth.json",
        "**/oauth_creds.json",
        "**/antigravity-oauth-token",
        ".codex/",
        ".gemini/",
        "artifacts/",
    ):
        assert pattern in patterns
    assert "!cert.pem" in patterns  # Public local CA support remains intentional.


def write_executable(path, content):
    path.write_text(f"#!{sys.executable}\n" + content)
    path.chmod(0o755)


@pytest.fixture
def registration(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "apk", "import os,pathlib\npathlib.Path(os.environ['M2_APK']).touch()\n"
    )
    write_executable(
        bin_dir / "jq",
        """import json,os,sys
if '-n' in sys.argv:
 print(json.dumps({'url':os.environ['M2_TELEGRAM_WEBHOOK_URL'],
  'secret_token':os.environ['CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN'],
  'allowed_updates':['message'],'drop_pending_updates':False}))
elif '-r' in sys.argv:
 print(json.load(sys.stdin)['tunnels'][0]['public_url'])
else:
 data=json.load(open(sys.argv[-1]))
 sys.exit(0 if data.get('ok') is True and data.get('result') is True else 1)
""",
    )
    write_executable(
        bin_dir / "curl",
        """import json,os,pathlib,sys
args=sys.argv[1:]
if '--config' not in args:
 print(json.dumps({'tunnels':[{'proto':'https','public_url':'https://synthetic.invalid'}]}))
else:
 config=sys.stdin.read()
 assert '123:synthetic' in config
 assert all('synthetic' not in arg and 't'*32 not in arg for arg in args)
 body=pathlib.Path(args[args.index('--data-binary')+1][1:]).read_text()
 pathlib.Path(os.environ['M2_CAPTURE']).write_text(body)
 pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(
  {'ok':False,'description':'t'*32} if os.environ.get('M2_REJECT') else {'ok':True,'result':True}))
""",
    )
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": "1",
        "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "t" * 32,
        "CODE_AGENT_TELEGRAM_BOT_TOKEN": "123:synthetic",
        "CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS": "42",
        "CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": "100",
        "M2_APK": str(tmp_path / "apk-called"),
        "M2_CAPTURE": str(tmp_path / "payload.json"),
    }
    return env


@pytest.mark.parametrize(
    "override",
    [
        {"CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": "0"},
        {"CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": "wrong"},
        {"CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": ""},
        {"CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "t" * 31},
        {"CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "t" * 32 + "!"},
        {"CODE_AGENT_TELEGRAM_BOT_TOKEN": ""},
        {"CODE_AGENT_TELEGRAM_BOT_TOKEN": '123:bad"token'},
        {"CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS": ""},
        {"CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": "-100"},
    ],
)
def test_registration_fails_before_network_or_skips_when_disabled(registration, override):
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/init_webhook.sh")],
        env={**registration, **override},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (
        0 if override.get("CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED") == "0" else 1
    )
    assert not Path(registration["M2_APK"]).exists()
    assert not Path(registration["M2_CAPTURE"]).exists()


@pytest.mark.parametrize("reject", [False, True])
def test_registration_preserves_updates_and_sanitizes_errors(registration, reject):
    if reject:
        registration["M2_REJECT"] = "1"
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/init_webhook.sh")],
        env=registration,
        capture_output=True,
        text=True,
    )
    assert result.returncode == int(reject)
    assert "t" * 32 not in result.stdout + result.stderr
    assert "123:synthetic" not in result.stdout + result.stderr
    payload = json.loads(Path(registration["M2_CAPTURE"]).read_text())
    assert payload == {
        "url": "https://synthetic.invalid/telegram/webhook",
        "secret_token": "t" * 32,
        "allowed_updates": ["message"],
        "drop_pending_updates": False,
    }
