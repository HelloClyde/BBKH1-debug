# Source, license and asset notices

- H1Debug resident service, USB protocol/client, native module runtime, BDA
  launcher/runtime, tests, build/release scripts and the accompanying skill
  are original work developed for this project. This independent distribution
  licenses that project code and documentation under **GPL-3.0-only**; see
  `LICENSE`. The development workspace also contained other projects with
  different licenses; those projects are not part of this repository.
- `vendor/h1-sdk` is the **BBK H1 Native BDA SDK** by its contributors, pinned to
  `067fe072477861dfc8949d7b1a55279fb92d2548` from
  <https://github.com/MrDefinition1999/bbk-h1-bda-sdk>.
  It remains separately licensed under **Apache-2.0**, with its own `LICENSE`
  and `NOTICE`. The launcher uses SDK inline bindings. GPLv3 was selected for
  this original project distribution to accommodate those Apache-2.0 bindings.
  See the [Apache compatibility explanation](https://www.apache.org/licenses/GPL-compatibility.html).
- The Windows GNU MIPS 15.2.0 toolchain is a build dependency downloaded from
  <https://static.grumpycoder.net/pixel/mips/g++-mipsel-none-elf-15.2.0.zip>.
  Its archive is pinned by SHA256. Toolchain binaries are not redistributed.
- `h1-debug/assets/icon.png` was generated for this project with ImageGen;
  its prompt and provenance are in `h1-debug/assets/icon.provenance.json`.
  H1 interface imagery was used as a style reference, not as a bundled icon
  library. No reference app dump or reference-image collection is distributed.
- Screenshots in `docs/evidence` depict the user's physical H1 and homebrew
  test apps. Proprietary interface elements and marks remain the property of
  their respective owners; the software license does not relicense those
  depicted elements. They are supplied as testing evidence.
- No BBK firmware, NAND image, original-device BDA dump, commercial ROM,
  personal data, serial wire capture, toolchain or other emulator core is
  included. Private-firmware tests require the user's own inputs.
