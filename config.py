"""Load config.yaml once and expose it as a plain dict."""
import yaml

CONFIG_PATH = "config.yaml"


def load(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
