"""Run offline installer failure injection in an isolated root container."""
import os,subprocess
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
env=os.environ.copy()
for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(key,None)
result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','run','--rm','--network','none','--user','0:0',
 '--mount',f'type=bind,src={repo},dst=/repo,readonly','--mount',f'type=bind,src={os.environ.get("ARMADA_TEST_OPENSSL","/usr/bin/openssl")},dst=/usr/bin/openssl,readonly',
 '--entrypoint','python','python:3.12-slim-bookworm','/repo/tests/test_release_safety.py'],env=env)
raise SystemExit(result.returncode)
