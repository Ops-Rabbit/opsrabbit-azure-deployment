"""Materialize private server configuration without logging credentials."""

import json
import os
from pathlib import Path

os.umask(0o077)
key = os.environ.pop("OPENSANDBOX_API_KEY")
if len(key) < 32:
    raise ValueError("A strong API key is required")
config = Path("/tmp/azure-opensandbox.toml")
config.write_text(
    '[server]\nhost="0.0.0.0"\nport=8080\napi_key='
    + json.dumps(key)
    + '\n[runtime]\ntype="azure_dynamic_sessions"\nexecd_image="baked-into-approved-pool-image"\n[log]\nlevel="WARNING"\n[store]\ntype="sqlite"\npath="/tmp/opensandbox.db"\n'
)
os.execvp("opensandbox-server", ["opensandbox-server", "--config", str(config)])
