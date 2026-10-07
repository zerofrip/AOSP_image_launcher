# StartLoader — AOSP x86_64 PRODUCT_OUT launcher

Inspect and (when supported) launch an AOSP `PRODUCT_OUT` directory for **x86_64** guests.

This repository was historically **StartLoader**, an Android software-image emulator with a 16-bit bootloader stub and Tk extras (the StartLoader / RetroFrost lineage). That path does **not** boot AOSP. The supported entry point is `python3 src/launcher.py`.

Python 3.11+, standard library only. Commands are built as argv lists (`shell=True` is never used). `PRODUCT_OUT` is treated as read-only.

## Supported families (v1)

| Family | Detection | This launcher |
| --- | --- | --- |
| `emulator_ranchu` | `kernel-ranchu` / `kernel-ranchu-64`, `advancedFeatures.ini`, `ro.hardware=ranchu`/`goldfish`, `emu64x`, `sdk_phone*` | Android Emulator only, and only when kernel + ramdisk + `system.img` are present |
| `cuttlefish` | `vsoc_*`, `aosp_cf_*`, board `cutf`, `super.img` + `vendor_boot.img` | **Unsupported.** Diagnose and recommend `launch_cvd`. Do not convert images |
| unknown / conflicting | mixed or missing evidence | Refused (`bootable=False`) |

**Cuttlefish is unsupported in v1.** `vsoc_x86_64` is not an Android Emulator AVD and is not a plain `qemu-system-x86_64` disk boot. Use the Cuttlefish host tools (`launch_cvd`). This launcher will not rewrite `super.img` / `vendor_boot.img` into emulator disks.

**QEMU backend is fail-closed for `emulator_ranchu` and `cuttlefish`.** ranchu needs goldfish devices the stock qemu binary does not provide; Cuttlefish requires `launch_cvd`. Missing kernel/initrd/cmdline evidence, dynamic partitions, and `super.img` also refuse.

**Generic x86_64 builds** (`product.family = "unknown"` — no `ro.hardware=ranchu/goldfish`, no Cuttlefish evidence): `qemu-system-x86_64` is now supported when `qemu-system-x86_64` is on PATH, architecture is `x86_64`, a kernel + ramdisk (or `boot.img`/`init_boot.img`/`vendor_boot.img` parsed natively — no external `unpack_bootimg` required) and `system.img` are available, and neither `vendor_boot` nor `super.img`/dynamic partitions are present. `vendor_boot` and `super.img` are still never attached as a regular disk on any family. The `emulator_ranchu` and `cuttlefish` families remain explicitly unsupported by this backend.

This path has been verified to boot real Android userspace end to end:
given just `boot.img` + `init_boot.img` + `vendor_boot.img` + `system.img`
+ `vendor.img` from a real AOSP x86_64 build (no standalone `kernel`/
`ramdisk.img` files needed), the launcher natively combines the kernel +
vendor/generic ramdisks + a synthetic, injected `/fstab.<hardware>` +
`androidboot.partition_map`/`androidboot.boot_devices` (so fs_mgr can
create `/dev/block/by-name/<partition>` symlinks for this flat,
GPT-less, one-partition-per-virtio-disk layout) and real Android's
first-stage init successfully mounts `/system` and `/vendor`, loads the
SELinux policy, and reaches second-stage init parsing real `init.rc`
service definitions (zygote, apexd, aconfigd, …). Additional partitions
(`vendor.img`, `product.img`, `system_ext.img`, `userdata.img`) are
auto-attached the same way when present in `PRODUCT_OUT`.

### Friendlier QEMU boot flow

Real-world verification (building `qemu-system-x86_64` from source and booting
real AOSP x86_64 images) surfaced two rough edges that are now handled
automatically instead of requiring manual flags:

- **Firmware path for a built-but-not-`ninja install`-ed qemu.** A locally
  built `qemu-system-x86_64` that was never installed cannot find its
  `bios-256k.bin` / `linuxboot_dma.bin` at its compiled-in default datadir,
  so `-kernel` silently falls through to an unbootable BIOS with no error.
  The launcher now checks the standard system firmware directories first and,
  if the firmware isn't there, looks next to the resolved `qemu-system-x86_64`
  binary (`<builddir>/qemu-bundle/usr/local/share/qemu`, `<builddir>/pc-bios`,
  `../share/qemu`) and adds `-L <dir>` automatically, with a warning
  suggesting `ninja install` for a permanent fix. `ninja install` itself needs
  root (`/usr/local/...`); this auto-detection is the no-sudo-required path.
- **Network backend availability.** `-netdev user` (slirp) requires libslirp,
  which some qemu builds lack; it used to be hardcoded and failed at runtime
  with `network backend 'user' is not compiled into this binary`. The
  launcher now probes `qemu-system-x86_64 -netdev help` and prefers
  `user` → `passt` (only if the external `passt(1)` helper binary is also
  actually on `PATH` — `-netdev help` lists the backend *type* regardless of
  whether the helper is installed) → no networking (`-nic none`), with a
  warning explaining the degraded ADB reachability. Boot is never blocked by
  a missing network backend.

### Friendlier Cuttlefish guidance

The launcher now detects `launch_cvd`/`cvd` on `PATH` and reports concrete,
host-specific next steps instead of a generic "go install something" message:
- found + `/dev/kvm` accessible → points at the exact `launch_cvd` path to run.
- found but `/dev/kvm` not accessible → gives the exact fix (`sudo usermod -aG
  kvm,cvdnetwork,render "$USER" && sudo reboot`).
- not found → points at the [android-cuttlefish](https://github.com/google/android-cuttlefish)
  host packages to install.

Cuttlefish itself remains unsupported by the Android Emulator and
`qemu-system-x86_64` backends in this launcher — these are diagnostics only,
not a new execution path.

`bootable=True` is reported only after compatibility checks pass.

## PRODUCT_OUT resolution

Precedence:

1. `--product-out PATH`
2. `PRODUCT_OUT`
3. `ANDROID_PRODUCT_OUT`
4. Unique discovery from cwd / parents (`out/target/product/<target>`)

Multiple targets raise an error listing the candidates. Pass `--target` or an explicit `--product-out`. `--list-product-outs [ROOT]` prints matches and exits. Relative and absolute paths are accepted (WSL also accepts `C:\...` and maps to `/mnt/<drive>/...`).

Guest architecture is taken from directory name, `build.prop` ABI keys, fingerprints, and kernel inspection (ELF / bzImage `HdrS`). **Only x86_64 is accepted.** ARM64 and mixed x86-vs-ARM evidence are errors; extra ABIs in `abilist` after the primary are not treated as conflicts.

## Backends and accelerators

Android Emulator is the primary backend, and only for the ranchu/goldfish family. Discovery order:

1. `PATH` (`emulator` / `emulator.exe`)
2. `$ANDROID_SDK_ROOT/emulator/` then `$ANDROID_HOME/emulator/`
3. Walk up from `PRODUCT_OUT` and `$ANDROID_BUILD_TOP` for `prebuilts/android-emulator/<os>-x86_64/emulator`

Goldfish ADB uses `emulator -ports <console>,<adb>` (`--adb-port 5555` → `5554,5555`). slirp `hostfwd` is not used.

Accelerators are **interrogated on the selected executable** (`qemu-system-x86_64 -accel help`, `emulator -help-accel` / `emulator-check accel`), 5 second timeout. Probe failures become diagnostics, not silent success.

- **WHPX** is only for native Windows PE executables that report WHPX. It is never inferred from hostname.
- Linux/WSL ELF + `--accel whpx` is an error (WSL Linux qemu rejects `--accel whpx`).
- Explicit unavailable accel (`--accel kvm` without `/dev/kvm`, `--accel whpx` when not reported) is an error.
- Only `--accel auto` may fall back to TCG, and then with a recorded reason.

KVM requires `/dev/kvm` present and accessible on Linux/WSL.

## Images this launcher will not misuse

- **`vendor_boot.img` / `init_boot.img`**: never passed as `-initrd` or as a regular `-drive`.
- **`super.img` / dynamic partitions**: never attached as an ordinary disk. Diagnose them; do not `simg2img` / `lpunpack` automatically.
- **AVB / `vbmeta*.img`**: inventoried as optional metadata. v1 does not disable or strip verification.
- `--extra-arg` is ignored unless `--allow-extra-args` is set (otherwise a validation error).

`--diagnose`, `--list-product-outs`, `--dry-run`, and `--print-command` do not start a guest. They may probe `emulator -help` / accel help (timeout 5s) in order to assemble a plan.

## Userdata and instance state

Writable userdata is seeded **once** from `PRODUCT_OUT` into a persistent instance directory. Later launches preserve guest changes; `--reset-data` safely deletes and re-seeds only that instance copy. `PRODUCT_OUT` is never modified.

Default state directory:

- Linux/WSL: `${XDG_STATE_HOME:-$HOME/.local/state}/startloader`
- Windows: `%LOCALAPPDATA%\startloader`
- Override: `STARTLOADER_STATE_DIR` or `--work-dir`

Instance path: `<state>/instances/<sanitized-target>/` with `userdata.img` and `state.json`.

Initial seed/reset uses a tempfile + atomic replace and `shutil.copy2` (file-level sparsity preserved when the OS copy does). Existing regular instance userdata is retained; symlinks and non-regular destinations are rejected. There is **no** automatic `simg2img`. `--reset-data` deletes only the instance copy and refuses `..`, raw absolute wipe targets, symlink escape, deleting the state root, and deleting `PRODUCT_OUT`.

`~/.startloader` is **not** the default.

## CLI

```bash
python3 src/launcher.py --help
python3 src/launcher.py --product-out PATH --diagnose
python3 src/launcher.py --product-out PATH --dry-run
python3 src/launcher.py --list-product-outs /path/to/aosp
python3 src/launcher.py --product-out PATH --headless   # launch only if bootable
python3 src/launcher.py --gui
```

### Build an x86_64 Emulator product from AOSP

Use the goldfish/ranchu `sdk_phone64_x86_64-trunk_staging-userdebug` product. Current AOSP lunch syntax is `<product>-<release>-<variant>`. The default `droid` target builds the product images and host-side dependencies, then the launcher resolves and validates `PRODUCT_OUT` before launch:

```bash
python3 src/launcher.py \
  --build-aosp /home/one/android-latest-release \
  --lunch sdk_phone64_x86_64-trunk_staging-userdebug \
  --build-jobs 4 \
  --headless

# Build and validate only; do not launch a guest
python3 src/launcher.py \
  --build-aosp /home/one/android-latest-release \
  --build-only

# Makefile wrapper
make build-aosp AOSP_ROOT=/home/one/android-latest-release
```

The build uses a fixed Bash script with the lunch target and build targets passed as positional arguments; it does not concatenate user values into shell source. `PRODUCT_OUT` must resolve below `<AOSP_ROOT>/out/target/product`. `--build-target` may be repeated when a narrower AOSP target is intentionally required; the default `droid` build is the supported path.

Do not build `vsoc_x86_64` for this backend. It is a Cuttlefish product and remains a `launch_cvd` workflow even though it produces files named `system.img`, `boot.img`, and `super.img`.

Defaults: `--backend auto`, `--memory 4096` (legacy `4G` is accepted), `--cpus min(4, host)`, `--adb-port 5555`, `--accel auto`.

Validation failures exit non-zero. Cuttlefish `--diagnose` exits 0 when inspection succeeded. `--dry-run` / launch of an unsupported product exits non-zero with `bootable=False`. `KeyboardInterrupt` exits 130.

### `--help` (captured)

```
usage: launcher.py [-h] [--product-out PRODUCT_OUT] [--target TARGET]
                   [--list-product-outs [ROOT]]
                   [--backend {auto,emulator,qemu}] [--kernel KERNEL]
                   [--ramdisk RAMDISK] [--vendor-boot VENDOR_BOOT]
                   [--system SYSTEM] [--system-ext SYSTEM_EXT]
                   [--vendor VENDOR] [--product PRODUCT_IMAGE]
                   [--userdata USERDATA] [--memory MEMORY] [--cpus CPUS]
                   [--adb-port ADB_PORT] [--accel {auto,whpx,kvm,tcg}]
                   [--headless] [--writable-system] [--snapshot] [--read-only]
                   [--append APPEND] [--extra-arg EXTRA_ARGS]
                   [--allow-extra-args] [--dry-run] [--print-command]
                   [--diagnose] [--verbose] [--gui] [--work-dir WORK_DIR]
                   [--reset-data]

Launch an AOSP x86_64 PRODUCT_OUT via Android Emulator (ranchu) or diagnose
Cuttlefish.

options:
  -h, --help            show this help message and exit
  --product-out PRODUCT_OUT
                        PRODUCT_OUT directory (overrides PRODUCT_OUT env)
  --target TARGET       Select among out/target/product/<target> when multiple
                        exist
  --list-product-outs [ROOT]
                        List PRODUCT_OUT directories under ROOT (default: cwd)
                        and exit
  --backend {auto,emulator,qemu}
                        Launch backend (default: auto)
  --kernel KERNEL       Override kernel path
  --ramdisk RAMDISK     Override ramdisk path
  --vendor-boot VENDOR_BOOT
                        Rejected: vendor_boot is never -initrd or -drive
  --system SYSTEM       Override system.img
  --system-ext SYSTEM_EXT
                        Override system_ext.img
  --vendor VENDOR       Override vendor.img
  --product PRODUCT_IMAGE
                        Override product.img
  --userdata USERDATA   Override userdata.img source
  --memory MEMORY       Guest memory in MB, or legacy size like 4G (default:
                        4096)
  --cpus CPUS           vCPU count (default: min(4, host cpus))
  --adb-port ADB_PORT   Goldfish ADB port (default: 5555 → -ports 5554,5555)
  --accel {auto,whpx,kvm,tcg}
                        Accelerator (default: auto; only auto may fall back to
                        TCG)
  --headless            Pass -no-window when supported
  --writable-system
  --snapshot
  --read-only
  --append APPEND       Extra kernel cmdline (emulator -qemu -append when
                        supported)
  --extra-arg EXTRA_ARGS
                        Extra backend arg (repeatable; requires --allow-extra-
                        args)
  --allow-extra-args
  --dry-run             Print plan and argv; do not spawn the guest
  --print-command       Print the command line; do not spawn
  --diagnose            Inspect PRODUCT_OUT; do not spawn the guest
  --verbose
  --gui                 Open the Tk frontend
  --work-dir WORK_DIR   Override instance state directory
  --reset-data          Reset instance userdata under the state dir
```

### Real `vsoc_x86_64` `--diagnose` (captured)

Inspected `/home/zero/android-latest-release/out/target/product/vsoc_x86_64` (exit 0). No files in that tree were modified (SHA-256 / size / mtime of `android-info.txt`, `required_images`, and `userdata.img` were identical before and after).

```
StartLoader PRODUCT_OUT diagnosis
product_out: /home/zero/android-latest-release/out/target/product/vsoc_x86_64
requested:   /home/zero/android-latest-release/out/target/product/vsoc_x86_64
target:      vsoc_x86_64
architecture: x86_64
family:      cuttlefish (confidence high)
dynamic_partitions: True
incomplete:  False

Artifacts:
  kernel       detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/kernel
  ramdisk      detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/ramdisk.img
  boot         detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/boot.img
  vendor_boot  unsupported  /home/zero/android-latest-release/out/target/product/vsoc_x86_64/vendor_boot.img
  init_boot    detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/init_boot.img
  system       detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/system.img
  vendor       detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/vendor.img
  userdata     detected     /home/zero/android-latest-release/out/target/product/vsoc_x86_64/userdata.img
  super        unsupported  /home/zero/android-latest-release/out/target/product/vsoc_x86_64/super.img
  note: vendor_boot is never passed as -initrd or -drive
  note: super.img is not a regular disk

Architecture evidence:
  directory_name:target=vsoc_x86_64
  directory_name:target.arch=x86_64
  build.prop:ro.product.cpu.abi=x86_64
  build.prop:ro.bionic.arch=x86_64
  build.prop:ro.product.vendor.model=Cuttlefish x86_64 phone
  build.prop:ro.product.system.model=Generic System
  build.prop:ro.product.board=cutf
  build.prop:ro.product.vendor.name=aosp_cf_x86_64_phone
  build.prop:ro.product.vendor.device=vsoc_x86_64
  build_fingerprint:build_fingerprint-aosp_cf_x86_64_phone.txt=generic/aosp_cf_x86_64_phone/vsoc_x86_64:Baklava/CP2A.260605.016/eng.zero:userdebug/test-keys
  build_fingerprint:build_fingerprint-aosp_cf_x86_64_phone.txt.arch=x86_64
  build_fingerprint:content.arch=x86_64
  kernel:kernel.architecture=x86_64
Family evidence:
  directory_name:vsoc_=vsoc_x86_64
  build.prop:ro.product.vendor.name=aosp_cf_x86_64_phone
  build.prop:ro.product.vendor.model=Cuttlefish x86_64 phone
  build.prop:ro.product.board=cutf
  build_fingerprint:build_fingerprint-aosp_cf_x86_64_phone.txt=generic/aosp_cf_x86_64_phone/vsoc_x86_64:Baklava/CP2A.260605.016/eng.zero:userdebug/test-keys
  file:super.img+vendor_boot.img=present
  required_images:super.img+vendor_boot.img=listed
Issues:
  [warning] dynamic_partitions: dynamic partitions / super.img present; v1 backends cannot map this as a regular disk

Host: wsl cpus=16 kvm_exists=True kvm_accessible=True
Tools:
  emulator         /home/zero/android-latest-release/prebuilts/android-emulator/linux-x86_64/emulator
  qemu-system-x86_64 not found
  emulator-check   /home/zero/android-latest-release/prebuilts/android-emulator/linux-x86_64/emulator-check
  unpack_bootimg   /home/zero/android-latest-release/out/host/linux-x86/bin/unpack_bootimg
  lpunpack         not found
  simg2img         /usr/bin/simg2img
  avbtool          /home/zero/android-latest-release/out/host/linux-x86/bin/avbtool

backend:     cuttlefish
bootable:     False
support:      unsupported
accelerator:  kvm
Reasons:
  - Cuttlefish is unsupported by the Android Emulator and qemu-system-x86_64 backends
  - use launch_cvd from the Cuttlefish host package
Backend errors:
  - Cuttlefish is unsupported by the Android Emulator and qemu-system-x86_64 backends
  - use launch_cvd from the Cuttlefish host package
  - Android Emulator backend supports ranchu/goldfish only; family is cuttlefish
  - Cuttlefish must be launched with launch_cvd, not the Android Emulator
  - Cuttlefish (vsoc_x86_64) is not supported by qemu-system-x86_64; use launch_cvd
  - super.img / dynamic partitions cannot be attached as an ordinary QEMU disk
  - vendor_boot must not be passed as -initrd or -drive
  - missing complete kernel/initrd/cmdline evidence for a goldfish-free QEMU boot

Cuttlefish (vsoc) is not supported by this launcher in v1.
Do not convert images. Use launch_cvd from the Cuttlefish host tools.
Android Emulator is refused. QEMU is fail-closed.

No executable launch plan (argv empty).
```

### Synthetic ranchu `--diagnose` (captured, not bootable)

Magic-byte fixture only. Emulator was not on `PATH` in this run, so `bootable=False`.

```
StartLoader PRODUCT_OUT diagnosis
product_out: /tmp/startloader_fixtures/emu64x
requested:   /tmp/startloader_fixtures/emu64x
target:      emu64x
architecture: x86_64
family:      emulator_ranchu (confidence high)
dynamic_partitions: False
incomplete:  False

Artifacts:
  kernel       required     /tmp/startloader_fixtures/emu64x/kernel-ranchu-64
  ramdisk      required     /tmp/startloader_fixtures/emu64x/ramdisk-qemu.img
  boot         missing      -
  vendor_boot  missing      -
  init_boot    missing      -
  system       required     /tmp/startloader_fixtures/emu64x/system.img
  vendor       detected     /tmp/startloader_fixtures/emu64x/vendor.img
  userdata     detected     /tmp/startloader_fixtures/emu64x/userdata.img
  super        missing      -

Architecture evidence:
  directory_name:target=emu64x
  directory_name:target.arch=x86_64
  build.prop:ro.product.cpu.abi=x86_64
  build.prop:ro.product.cpu.abilist.primary=x86_64
  build.prop:ro.product.cpu.abilist.translated=x86
  build.prop:ro.hardware=ranchu
  build.prop:ro.product.model=sdk_phone64_x86_64
  build.prop:ro.product.name=sdk_phone_x86_64
  kernel:kernel-ranchu-64.architecture=x86_64
Family evidence:
  directory_name:emu64x=emu64x
  build.prop:ro.hardware=ranchu
  file:kernel-ranchu-64=present
  file:advancedFeatures.ini=present

Host: wsl cpus=16 kvm_exists=True kvm_accessible=True
Tools:
  emulator         not found
  qemu-system-x86_64 not found
  emulator-check   not found
  unpack_bootimg   not found
  lpunpack         not found
  simg2img         /usr/bin/simg2img
  avbtool          not found

backend:     emulator
bootable:     False
support:      unsupported
accelerator:  tcg
Reasons:
  - emulator not found (PATH, ANDROID_SDK_ROOT, ANDROID_HOME, AOSP prebuilts)
  - QEMU backend is fail-closed in v1 without a complete non-goldfish command evidence pack
  - ranchu/goldfish requires Android Emulator goldfish pipe/sync/battery devices; plain qemu-system-x86_64 is unsupported
  - missing complete kernel/initrd/cmdline evidence for a goldfish-free QEMU boot
Backend errors:
  - emulator not found (PATH, ANDROID_SDK_ROOT, ANDROID_HOME, AOSP prebuilts)
  - auto: emulator plan is not bootable; QEMU is fail-closed for ranchu

No executable launch plan (argv empty).
```

### Synthetic Cuttlefish `--dry-run` (captured, exit 2)

```
error: Cuttlefish is unsupported by the Android Emulator and qemu-system-x86_64 backends
error: use launch_cvd from the Cuttlefish host package
error: Android Emulator backend supports ranchu/goldfish only; family is cuttlefish
error: Cuttlefish must be launched with launch_cvd, not the Android Emulator
error: Cuttlefish (vsoc_x86_64) is not supported by qemu-system-x86_64; use launch_cvd
error: super.img / dynamic partitions cannot be attached as an ordinary QEMU disk
error: vendor_boot must not be passed as -initrd or -drive
error: missing kernel evidence
error: missing initrd/ramdisk evidence
error: missing complete kernel/initrd/cmdline evidence for a goldfish-free QEMU boot
bootable=False backend=cuttlefish accel=tcg
argv=[]
```

## GUI

`python3 src/launcher.py --gui` is a thin Tk frontend over the same Python APIs (not a string of CLI flags to `launcher.py`). Launch is disabled when `bootable` is False. Stop uses `terminate`, then `kill` after a wait timeout.

**Custom ROM Creator** is a labeled non-functional legacy mock. It does not write a ROM and does not report false success.

The old “allocate userdata by truncating a huge empty file under `assets/`” path is removed.

## Tests

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests -v
# or
make test
```

CI (`.github/workflows/ci.yml`) runs compile + unittest on Ubuntu and Windows, Python 3.11 and 3.12. Tests use synthetic magic-byte fixtures only; they do not boot qemu or the Android Emulator. See `tests/fixtures/README.md`.

## Legacy bootloader

`bootloader/boot.S` is a 16-bit MBR stub. `make build` still assembles it. `make run` prints launcher help and warns that it does not boot AOSP x86_64. Do not use `boot.bin` as an Android bootloader.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `AmbiguousProductOutError` | Several `out/target/product/*` dirs; pass `--product-out` or `--target` |
| `guest architecture is not x86_64` | This tree is x86_64-only |
| Cuttlefish `bootable=False` | Expected. Use `launch_cvd` |
| ranchu `emulator not found` | Put `emulator` on `PATH`, set `ANDROID_SDK_ROOT`, or use an AOSP tree with `prebuilts/android-emulator/` |
| `--accel whpx` on WSL | Linux ELF qemu/emulator cannot use WHPX |
| `--extra-arg` error | Pass `--allow-extra-args` |
| ADB | Goldfish uses `-ports`, not slirp `hostfwd` |
| Want to unpack `vendor_boot` / `super` | Out of scope for v1; do not attach them as disks |

## Lineage

Historical UI, extras, and the 16-bit stub come from **StartLoader** (RetroFrost / startloader lineage in this tree). The PRODUCT_OUT inspector/launcher is the current supported behavior.

## Licensing

Older README text said this project was MIT-licensed. There is **no `LICENSE` file** in the repository, and git history has **no copyright notice**. This document does not invent a copyright holder. Adding a `LICENSE` and a holder line is an unresolved upstream issue.

## Requirements

- Python 3.11+
- AOSP host build prerequisites, including `bash` and the OS `zip` package
- `tkinter` only if you use `--gui`
- Android Emulator (ranchu launch)
- Cuttlefish host tools (`launch_cvd`) for `vsoc_*` — not bundled here
