# LEGACY: bootloader/boot.S is a 16-bit MBR stub. It is not used to boot
# AOSP x86_64. Prefer: python3 src/launcher.py --product-out PATH --diagnose
CROSS_COMPILE ?=
AS = $(CROSS_COMPILE)as
LD = $(CROSS_COMPILE)ld
OBJCOPY = $(CROSS_COMPILE)objcopy
PYTHON = python3

BOOTLOADER_SRC = bootloader/boot.S
BOOTLOADER_OBJ = bootloader/boot.o
BOOTLOADER_BIN = bootloader/boot.bin

.PHONY: all build build-aosp run clean help test diagnose

AOSP_ROOT ?=
AOSP_LUNCH ?= sdk_phone64_x86_64-trunk_staging-userdebug
AOSP_JOBS ?= 4

all: help

# Legacy 16-bit stub. Not part of the AOSP PRODUCT_OUT launch path.
build: $(BOOTLOADER_BIN)

$(BOOTLOADER_BIN): $(BOOTLOADER_SRC)
	$(AS) -o $(BOOTLOADER_OBJ) $(BOOTLOADER_SRC)
	$(LD) --oformat binary -Ttext 0x7C00 -o $(BOOTLOADER_BIN) $(BOOTLOADER_OBJ)

# Legacy: boots the 16-bit stub via the old QEMU disk path. Do not use for AOSP.
run: build
	@echo "WARNING: 'make run' is a legacy target and does not boot AOSP x86_64."
	$(PYTHON) src/launcher.py --help

test:
	$(PYTHON) -m compileall -q src tests
	$(PYTHON) -m unittest discover -s tests -v

diagnose:
	$(PYTHON) src/launcher.py --diagnose

build-aosp:
	@test -n "$(AOSP_ROOT)" || (echo "Set AOSP_ROOT=/path/to/aosp"; exit 2)
	$(PYTHON) src/launcher.py --build-aosp "$(AOSP_ROOT)" --lunch "$(AOSP_LUNCH)" --build-jobs "$(AOSP_JOBS)" --build-only

clean:
	rm -f $(BOOTLOADER_OBJ) $(BOOTLOADER_BIN)

help:
	@echo "StartLoader / AOSP x86_64 PRODUCT_OUT launcher"
	@echo "Primary:"
	@echo "  python3 src/launcher.py --product-out PATH --diagnose"
	@echo "  python3 src/launcher.py --product-out PATH --dry-run"
	@echo "  make test"
	@echo "  make build-aosp AOSP_ROOT=/path/to/aosp"
	@echo ""
	@echo "Legacy (16-bit MBR stub, does not boot Android):"
	@echo "  build   Assemble and link bootloader/boot.bin"
	@echo "  run     Print launcher help (legacy stub is not launched)"
	@echo "  clean   Remove bootloader build artifacts"
