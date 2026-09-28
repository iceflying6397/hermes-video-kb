"""Opt-in installed Hermes pure-auth integration; no real account/network/CLI."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from video_kb import providers


def test_installed_hermes_native_anthropic_auth_and_api_headers(tmp_path):
    configured = os.environ.get("VIDEO_KB_TEST_HERMES_ROOT")
    if not configured:
        pytest.skip("opt-in installed Hermes integration")
    root, python = providers._hermes_install({"hermes_root": configured})
    home = tmp_path / "isolated-hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom:synthetic\n  default: test-model\n"
        "providers:\n  synthetic:\n    base_url: https://api.minimax.io/anthropic\n"
        "    api_mode: anthropic_messages\n"
        "auth:\n  adopt_external_logins: false\n")
    script = tmp_path / "native_api_probe.py"
    script.write_text(r'''
import importlib.util,json,os,sys
from pathlib import Path
root,bridge,home=sys.argv[1:]
sys.path.insert(0,root)
spec=importlib.util.spec_from_file_location("video_provider",bridge)
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
def audit(event,args):
    if event in {"socket.connect","socket.getaddrinfo","subprocess.Popen","os.system"}:
        raise PermissionError("no network or CLI in native integration")
    if event=="open" and args and isinstance(args[0],(str,bytes)):
        name=os.fsdecode(args[0])
        if Path(name).name in {"auth.json",".env","config.yaml",".credentials.json"} and not Path(name).resolve().is_relative_to(Path(home).resolve()):
            raise PermissionError("not the synthetic profile")
sys.addaudithook(audit)
p._suppress_probe_config_writes()
assert p._check_configuration()=="ready"
from agent import anthropic_adapter as native
def no_cli(): raise AssertionError("version discovery must not run")
native._get_claude_code_version=no_cli
native._detect_claude_code_version=no_cli
requests=[]
class Response:
    status=200
    def getheader(self,name): return None
    def read(self,n):
        return json.dumps({"content":[{"type":"text","text":"{}"}],"choices":[{"message":{"content":"{}"}}]}).encode()
class Connection:
    def __init__(self,host,port,**kwargs):
        assert host in {"api.minimax.io","api.anthropic.com","gateway.example.test"}
        assert kwargs["timeout"]==55
    def request(self,method,path,body,headers): requests.append((path,json.loads(body),headers))
    def getresponse(self): return Response()
    def close(self): pass
p.http.client.HTTPSConnection=Connection
messages=[{"role":"system","content":"Summarize only; no tools."},{"role":"user","content":"Synthetic source text."}]
for name,base,token in [
    ("minimax","https://api.minimax.io/anthropic","synthetic-minimax"),
    ("anthropic_api","https://api.anthropic.com","sk-ant-api-synthetic"),
    ("anthropic_oauth","https://api.anthropic.com","sk-ant-oat-synthetic")]:
    assert p._request({"api_mode":"anthropic_messages","base_url":base,"api_key":token,"model":"test-model"},messages)=="{}"
    path,body,headers=requests[-1]
    assert "tools" not in body and body["max_tokens"]==3000
    assert headers["accept-encoding"]=="identity"
    if name=="anthropic_api":
        assert headers["x-api-key"]==token and "authorization" not in headers
    else:
        assert headers["authorization"]=="Bearer "+token and "x-api-key" not in headers
    if name=="anthropic_oauth":
        assert headers["user-agent"]=="claude-code/"+native._CLAUDE_CODE_VERSION_FALLBACK+" (external, cli)"
        assert all(beta in headers["anthropic-beta"] for beta in native._OAUTH_ONLY_BETAS)
        assert body["system"][0]["text"]==native._CLAUDE_CODE_SYSTEM_PREFIX
    else:
        assert body["system"]==messages[0]["content"]
assert p._request({"api_mode":"chat_completions","base_url":"https://gateway.example.test/v1",
    "api_key":"unused-synthetic","model":"test-model","extra_headers":{"X-Gateway-Token":"synthetic","Authorization":"Bearer synthetic-gateway"}},messages)=="{}"
assert requests[-1][2]["authorization"]=="Bearer synthetic-gateway"
assert requests[-1][2]["x-gateway-token"]=="synthetic"
print(json.dumps({"cases":4,"native_auth_helpers":True,"real_account_used":False,"network_calls":0,"cli_calls":0}))
''')
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(tmp_path / "runtime"))
    result = subprocess.run([str(python), "-I", "-B", str(script), str(root), str(Path(providers.__file__).resolve()), str(home)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, text=True)
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "cases": 4, "native_auth_helpers": True, "real_account_used": False, "network_calls": 0, "cli_calls": 0,
    }
