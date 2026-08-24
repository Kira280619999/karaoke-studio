# Security Policy

Karaoke Studio is designed for one operator on a trusted local machine. Both servers bind to `127.0.0.1`; do not expose the development server or FastAPI port to a LAN or the public internet.

## Dependency gate

CI runs `pnpm audit --prod --audit-level high`. Production dependencies currently report no known vulnerabilities.

The complete dev-dependency audit currently reports two denial-of-service advisories in `image-size@2.0.2`, inherited only through the Vinext development toolchain. As of 2026-08-23, npm has no patched `image-size` release. Karaoke Studio mitigates this boundary by:

- binding the development server to loopback only;
- keeping user uploads in the separate FastAPI process;
- allowing background uploads only as PNG, JPEG or WebP (and supported video formats), never ICNS, JXL or HEIF;
- committing only trusted PNG/SVG public assets;
- tracking the upstream dependency with Dependabot.

Remove this note and require a clean full audit as soon as Vinext adopts a patched parser.

## Reporting

Do not open a public issue containing private media, tokens or local paths. Reproduce with synthetic media whenever possible and include the relevant tool version, project state and redacted QA report.
