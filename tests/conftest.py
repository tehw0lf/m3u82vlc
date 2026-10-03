import sys
import types

# env.py is gitignored, so the tests bring their own configuration
sys.modules["env"] = types.SimpleNamespace(
    base_path="/nonexistent-recordings",
    favorites=[],
    elements_to_click_on_load=[],
    server_token="test-token",
)
