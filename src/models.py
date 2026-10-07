"""Shared dataclasses for PRODUCT_OUT inspection, capabilities, and launch plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SupportState = Literal[
    "supported",
    "conditionally_supported",
    "unsupported",
    "incomplete",
    "conflicting",
]

ArtifactStatus = Literal[
    "required",
    "conditionally_required",
    "optional",
    "unsupported",
    "conflicting",
    "missing",
    "detected",
]

ProductFamily = Literal[
    "emulator_ranchu",
    "cuttlefish",
    "unknown",
    "conflicting",
]


@dataclass(frozen=True)
class Evidence:
    source: str
    key: str
    value: str


@dataclass
class ValidationIssue:
    severity: str  # error, warning, info
    code: str
    message: str
    path: str | None = None


@dataclass
class ImageInspection:
    path: Path
    size: int
    format: str
    architecture: str | None = None
    notes: list[str] = field(default_factory=list)
    is_symlink: bool = False
    symlink_target: str | None = None
    broken_symlink: bool = False


@dataclass
class Artifact:
    name: str
    kind: str
    path: Path | None
    status: ArtifactStatus
    format: str | None = None
    notes: list[str] = field(default_factory=list)
    inspection: ImageInspection | None = None


@dataclass
class ProductArtifacts:
    product_out: Path
    requested_path: str
    target_name: str | None
    architecture: str | None
    family: ProductFamily
    family_confidence: str
    architecture_evidence: list[Evidence] = field(default_factory=list)
    family_evidence: list[Evidence] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    kernel: Path | None = None
    ramdisk: Path | None = None
    boot: Path | None = None
    vendor_boot: Path | None = None
    init_boot: Path | None = None
    system: Path | None = None
    system_ext: Path | None = None
    vendor: Path | None = None
    product: Path | None = None
    userdata: Path | None = None
    super_image: Path | None = None
    optional_images: dict[str, Path] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    dynamic_partitions: bool = False
    incomplete: bool = False

    def artifact(self, kind: str) -> Artifact | None:
        for item in self.artifacts:
            if item.kind == kind:
                return item
        return None


@dataclass
class ArtifactMapping:
    roles: dict[str, str] = field(default_factory=dict)
    ignored: dict[str, str] = field(default_factory=dict)


@dataclass
class CommandSpec:
    argv: list[str]
    backend: str
    executable: Path | None
    mapping: ArtifactMapping
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    support_state: SupportState = "unsupported"
    bootable: bool = False
    reasons: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    extra_args_applied: bool = False


@dataclass
class LaunchPlan:
    product: ProductArtifacts
    spec: CommandSpec
    accelerator: str
    memory_mb: int
    cpus: int
    adb_port: int
    headless: bool
    read_only: bool


@dataclass
class LaunchOptions:
    backend: str = "auto"
    kernel: Path | None = None
    ramdisk: Path | None = None
    vendor_boot: Path | None = None
    system: Path | None = None
    system_ext: Path | None = None
    vendor: Path | None = None
    product_image: Path | None = None
    userdata: Path | None = None
    memory_mb: int = 4096
    cpus: int = 4
    adb_port: int = 5555
    accel: str = "auto"
    headless: bool = False
    writable_system: bool = False
    snapshot: bool = False
    read_only: bool = False
    append: str | None = None
    extra_args: list[str] = field(default_factory=list)
    allow_extra_args: bool = False
    work_dir: Path | None = None
    reset_data: bool = False
    dry_run: bool = False
    print_command: bool = False
    verbose: bool = False
