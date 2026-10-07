# Synthetic fixtures

Unit tests build tiny PRODUCT_OUT trees at runtime under `tempfile`.

Those trees contain **magic bytes and text metadata only**. They are **not
bootable** Android images:

- Android sparse header `0xED26FF3A` (LE bytes `3A FF 26 ED`)
- `ANDROID!` / `VNDRBOOT` boot magics
- ELF headers (x86_64 vs aarch64)
- bzImage `HdrS` setup header
- Cuttlefish-like `super.img` + `vendor_boot.img` stubs

Do not copy these stubs into a real AOSP `PRODUCT_OUT`. Do not treat a passing
unit test as evidence that a guest will boot.
