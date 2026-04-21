import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_MAIN = ROOT / "messenger-gateway" / "main.py"
DISCORD_BOT_MAIN = ROOT / "discord-bot" / "main.py"


def _load_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(_load_source(path), filename=str(path))
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


class SeedLabRegressionSmokeTests(unittest.TestCase):
    def test_gateway_defines_clip_text_helper(self) -> None:
        self.assertIn("_clip_text", _function_names(GATEWAY_MAIN))

    def test_gateway_progress_failure_is_logged_not_raised(self) -> None:
        source = _load_source(GATEWAY_MAIN)
        self.assertIn("seedlab progress message update failed", source)
        self.assertIn('return {"ok": True, "updated": should_update_message}', source)

    def test_discord_bot_wraps_gateway_transport_errors(self) -> None:
        source = _load_source(DISCORD_BOT_MAIN)
        self.assertIn("httpx.ReadTimeout", source)
        self.assertIn("httpx.ConnectError", source)
        self.assertIn("httpx.TransportError", source)
        self.assertIn("Seed Lab start timed out while waiting for gateway response", source)


if __name__ == "__main__":
    unittest.main()
