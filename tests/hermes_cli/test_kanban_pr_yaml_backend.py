"""The PR gate uses the config parser, not an extra PyYAML dependency."""
import subprocess
import sys


def test_gate_reuses_loaded_config_yaml_backend():
    result = subprocess.run(
        [sys.executable, "-c", '''
import base64
import sys
from hermes_cli import config
# A managed runtime can provide Hermes's YAML backend without the yaml package.
sys.modules["yaml"] = None
from hermes_cli.kanban_pr_acceptance import validate_contract
from hermes_cli.kanban_pr_workflows import inventory
assert validate_contract("https://github.com/acme/repo/pull/1") == "https://github.com/acme/repo/pull/1"
path = ".github/workflows/ci.yml"
def content(source):
    return {"path": path, "type": "file", "encoding": "base64",
            "content": base64.b64encode(source.encode()).decode()}
assert inventory(content("jobs:\\n  build:\\n    name: Build\\n    steps: []\\n"), path) == [{"job_id": "build", "name": "Build"}]
try:
    inventory(content("jobs: ["), path)
except config.yaml.YAMLError:
    pass
else:
    raise AssertionError("Malformed workflow must not be accepted")
'''],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
