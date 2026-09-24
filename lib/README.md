# lib/

- `ld-musl-aarch64.so.1` — musl 1.2.6 dynamic linker, taken unmodified from
  Alpine Linux's `musl` package (aarch64). MIT licensed; see `musl-COPYRIGHT`.
  `agy.bin` has its `PT_INTERP` pointed here by `install.sh`, so the repo is
  self-contained: nothing else has to be installed first.
  sha256: 32377e6d71725bb019e9ff6d5e9f16b4d5156d6f2c36504191c2d6a7c4d4a44d
- `libagyshim.so` — built from `../shim.c` at install time (ignored by git).
