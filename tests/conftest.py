import os
import pathlib

os.environ["GATEWAY_CONFIG"] = str(pathlib.Path(__file__).resolve().parents[1] / "infra" / "config.yaml")
