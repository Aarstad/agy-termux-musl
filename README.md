# Antigravity CLI for Termux

Run Google's Antigravity CLI (`agy`) natively on Android inside Termux — with no glibc chroot, no proot, and no VM.

This installer sets up the ARM64 binary to run against a lightweight musl loader (~720KB instead of a ~450MB glibc environment), applies surgical binary patches for Android execution, and provides a small background proxy so networking and DNS work seamlessly.

---

## Quick Start

### 1. Requirements

- **Termux on ARM64 (`aarch64`)**: Run `uname -m` to verify it prints `aarch64`.
- **The musl loader**: `$PREFIX/lib/ld-musl-aarch64.so.1`  
  *(If you already installed [claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl), you already have this! Otherwise, see [Installing the musl loader](#installing-the-musl-loader) below).*
- **Build tools & dependencies**: `curl`, `tar`, `clang`, `python3`, `patchelf`.
- **A Google account** with Antigravity access.

### 2. Install

Open **Termux** and run:

```bash
# 1. Install prerequisites
pkg update
pkg install git curl tar clang python patchelf

# 2. Clone the repository
git clone https://github.com/Aarstad/agy-termux-musl.git
cd agy-termux-musl

# 3. Download the VA39-adapted engine & install
# (Most Android kernels use 39-bit VA; start from wallentx's VA39-adapted build)
curl -fsSL -o agy.tar.gz \
  https://github.com/wallentx/antigravity-cli-termux/releases/download/v1.2.7/antigravity-termux-standalone.tar.gz
tar xzf agy.tar.gz agy.va39
./install.sh --from-binary agy.va39

# 4. (Optional) Add agy to your PATH
mkdir -p ~/.local/bin
ln -sf "$PWD/agy" ~/.local/bin/agy
```

If `~/.local/bin` is in your `$PATH`, you can now run `agy` from anywhere.

### 3. Sign in and Verify

Run:

```bash
agy models
```

Follow the browser prompt to sign in with your Google account. Once authenticated, `agy` will list available models (e.g. Gemini 3.8 Flash, Gemini 3.1 Pro) and is ready for interactive coding sessions.

```bash
# Start an interactive session in your project
agy
```

---

## Installing the musl Loader

`agy` runs against Alpine's musl dynamic linker rather than a heavy glibc package.

If `$PREFIX/lib/ld-musl-aarch64.so.1` is not already installed on your system, you can fetch and set it up quickly:

```bash
mkdir -p "$PREFIX/lib"
curl -fsSL -o "$PREFIX/lib/ld-musl-aarch64.so.1" \
  https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64/ld-musl-aarch64.so.1
chmod +x "$PREFIX/lib/ld-musl-aarch64.so.1"
```

---

## What Works

- **Interactive CLI & Full Turn Sessions**: Streaming output, multi-turn reasoning, and local SQLite state persistence.
- **Built-in Tool Execution**: File reading/writing, workspace search, terminal bash command execution.
- **Model Selection & Switching**: Seamlessly switch between Gemini 3.8 Flash, Gemini 3.1 Pro, etc.
- **Subagents**: Autonomous subagent spawning, task delegation, and lifecycle management.
- **MCP Servers**: Model Context Protocol servers (e.g. external servers running under `bun` or `node`) called via tool use.

---

## Troubleshooting & FAQ

### TCMalloc abort / "Memory mapping failed" on startup
Android kernels typically configure a 39-bit virtual address space (`VA39`), whereas Google's stock bundled TCMalloc allocator assumes a 48-bit address space (`VA48`).
- **Fix**: Ensure you pass `--from-binary agy.va39` during installation as shown in the install steps. Running `./install.sh` on stock unpatched Google binaries will trigger TCMalloc aborts on 39-bit VA devices.

### Network hangs or TLS certificate errors
Android lacks standard Linux paths like `/etc/resolv.conf` and `/etc/ssl/certs/ca-certificates.crt`.
- The included `agy` wrapper automatically points to the shared `termux-dns-proxy` daemon on `127.0.0.1:18080` if running, or launches an ephemeral companion bionic DNS tunnel (`dns-proxy`) as fallback.
- Sets `SSL_CERT_FILE="$PREFIX/etc/tls/cert.pem"` for trusted root CA verification.
- If you run the raw binary directly (`./agy.bin`), DNS queries will hang. Always launch via `./agy` (or the symlink).

---

## How It Works (Under the Hood)

For the curious: Google distributes `agy` as a glibc-linked dynamic PIE executable. Making it run on Android under musl required solving four distinct hurdles:

1. **glibc to musl Loader & Shim (`shim.c`)**:
   The ELF interpreter is repointed to `ld-musl-aarch64.so.1`. A tiny 40-line C shim (`lib/libagyshim.so`) provides missing glibc symbols (`__open`, `__close`, `__read`, `pvalloc`, pthread cancellation stubs).
2. **Dynamic Phdr Load-Bias Heuristic (`patch.py`)**:
   The internal binary function `google_find_phdr` miscalculated load biases under non-prelinking loaders due to an unsigned 64-bit comparison against `0xfffffffefffff001`. Replacing 5 conditional selects (`csel`) with unconditional moves (`mov`) fixes the crash at startup (20 bytes patched).
3. **Thread-Control-Block (TCB) Fallback (`patch.py`)**:
   Glibc reserves several hundred bytes below the thread pointer (`[tp - 0x260]`). Musl has a smaller TCB, causing segfaults on unmapped memory. Forcing the check to zero routes execution into Google's built-in global fallback path (4 bytes patched).
4. **Android DNS & TLS Integration (`dns-proxy.c` / `termux-dns-proxy`)**:
   A lightweight, single-threaded `epoll` + `splice(2)` proxy bridges network lookups to Android's bionic resolver, while Termux's certificate bundle provides trusted root CAs. Can run as a shared background service (`termux-dns-proxy`) across all musl tools.

Detailed analysis, disassembly traces, and offset tables are documented in [FINDINGS.md](file:///data/data/com.termux/files/home/projects/agy-termux-musl/FINDINGS.md).

---

## Related Projects

- **[claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)** — Claude Code on Termux via musl.
- **[codex-termux](https://github.com/Aarstad/codex-termux)** — OpenAI Codex CLI on Termux.
- **[wallentx/antigravity-cli-termux](https://github.com/wallentx/antigravity-cli-termux)** — The pioneer project that discovered the TCMalloc VA39 patch and proved running `agy` on Termux was possible.

## License

MIT for the code and scripts in this repository. Google's `agy` binary is owned by Google and is not redistributed here.
