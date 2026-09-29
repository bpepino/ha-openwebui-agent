"""Load the protocol independently of Home Assistant on any OS."""

from pathlib import Path
import sys
import types

# A namespace alias avoids importing the HA lifecycle module on Windows.
# The modules under test are the actual integration sources, not test doubles.
package = types.ModuleType("owui_protocol")
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components/openwebui_conversation")
]
sys.modules["owui_protocol"] = package
