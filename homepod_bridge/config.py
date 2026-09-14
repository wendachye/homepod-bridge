"""Tiny JSON config persistence for the tray app.

Stored at ``%APPDATA%/homepod-bridge/config.json`` on Windows
(``~/.config/homepod-bridge/config.json`` elsewhere).
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["BridgeConfig", "ConfigStore", "default_config_path"]


@dataclass
class BridgeConfig:
    devices: List[str] = field(default_factory=list)  # stable device identifiers
    volume: float = 50.0  # master; default for devices without an override
    device_volumes: Dict[str, float] = field(default_factory=dict)
    autoconnect: bool = False
    bitrate: int = 320
    quality: int = 2
    #: Receiver-side AirPlay buffer, seconds. Lower = less delay, less
    #: tolerance for network jitter. pyatv's stock value is 1.5.
    raop_latency: float = 0.5
    schema_version: int = 2
    # Old releases persisted display names. Keep undiscovered names pending
    # until a scan can resolve them; ambiguous names require user selection.
    legacy_devices: List[str] = field(default_factory=list)
    legacy_device_volumes: Dict[str, float] = field(default_factory=dict)

    def resolve_legacy(self, devices: Sequence) -> bool:
        """Migrate old names only when discovery identifies exactly one device."""
        by_name: Dict[str, set] = {}
        for device in devices:
            if device.streamable:
                by_name.setdefault(device.name, set()).add(device.identifier)
        changed = False
        for name in dict.fromkeys(self.legacy_devices + list(self.legacy_device_volumes)):
            matches = by_name.get(name, set())
            if not matches:
                continue
            changed = True
            if len(matches) == 1:
                identifier = next(iter(matches))
                if name in self.legacy_devices and identifier not in self.devices:
                    self.devices.append(identifier)
                if name in self.legacy_device_volumes:
                    self.device_volumes.setdefault(identifier, self.legacy_device_volumes[name])
            else:
                logger.warning("Saved device name %r is ambiguous; select the intended device again", name)
            self.legacy_devices = [n for n in self.legacy_devices if n != name]
            self.legacy_device_volumes.pop(name, None)
        return changed


def _clamp_level(value) -> float:
    """0-100 clamp that REJECTS NaN: json.loads accepts the NaN literal, and
    max/min would silently clamp it to 100 - full blast from an undefined
    value. Raising routes it to the corrupt-config default path instead."""
    level = float(value)
    if math.isnan(level):
        raise ValueError("volume must be a number")
    return max(0.0, min(100.0, level))


def _clamp_latency(value) -> float:
    from .raop_latency import MAX_LATENCY_SECONDS, MIN_LATENCY_SECONDS

    seconds = float(value)
    if math.isnan(seconds):
        raise ValueError("raop_latency must be a number")
    return max(MIN_LATENCY_SECONDS, min(MAX_LATENCY_SECONDS, seconds))


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "homepod-bridge" / "config.json"


class ConfigStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_config_path()

    def load(self) -> BridgeConfig:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            known = {f.name: data[f.name] for f in fields(BridgeConfig) if f.name in data}
            cfg = BridgeConfig(**known)
            # Coercion must stay inside the guard: valid JSON with wrong
            # types ({"volume": null}) would otherwise crash every launch.
            cfg.volume = _clamp_level(cfg.volume)
            if not isinstance(cfg.devices, list) or not isinstance(cfg.legacy_devices, list):
                raise ValueError("devices must be a list")
            cfg.devices = [str(d) for d in cfg.devices]
            cfg.device_volumes = {
                str(k): _clamp_level(v)
                for k, v in dict(cfg.device_volumes).items()
            }
            cfg.autoconnect = bool(cfg.autoconnect)
            cfg.bitrate = int(cfg.bitrate)
            cfg.quality = int(cfg.quality)
            cfg.raop_latency = _clamp_latency(cfg.raop_latency)
            cfg.legacy_devices = [str(d) for d in cfg.legacy_devices]
            cfg.legacy_device_volumes = {
                str(k): _clamp_level(v)
                for k, v in dict(cfg.legacy_device_volumes).items()
            }
            version = int(data.get("schema_version", 1))
            if version == 1:
                cfg.legacy_devices = list(dict.fromkeys(cfg.legacy_devices + cfg.devices))
                cfg.legacy_device_volumes = {**cfg.device_volumes, **cfg.legacy_device_volumes}
                cfg.devices, cfg.device_volumes = [], {}
            elif version != 2:
                raise ValueError("unsupported config version")
            cfg.schema_version = 2
        except FileNotFoundError:
            return BridgeConfig()
        except Exception:  # noqa: BLE001 - corrupt config must never crash the app
            logger.warning("Corrupt config at %s; using defaults", self.path)
            return BridgeConfig()
        return cfg

    def save(self, cfg: BridgeConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for stale in self.path.parent.glob(self.path.stem + "-*.tmp"):
            try:
                stale.unlink()  # orphaned by a hard kill mid-save
            except OSError:
                pass  # in use by a concurrent save
        # Unique temp name: concurrent saves (menu thread vs slider thread)
        # sharing one fixed .tmp could interleave into corrupt JSON.
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.stem + "-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(cfg), indent=2))
            os.replace(tmp_name, self.path)  # atomic-ish: never half-written
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
