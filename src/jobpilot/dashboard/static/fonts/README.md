# Vendored fonts

Geist and Geist Mono variable fonts from the `geist` npm package, version 1.7.2
(published 2026-06-01), fetched from jsDelivr:

| File | Source | sha256 |
| --- | --- | --- |
| Geist-Variable.woff2 | https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-sans/Geist-Variable.woff2 | a369fcf5628ea2aa4e1b9e2ec6a5b3624e365bda588e1f0f2f12b564f728fbb8 |
| GeistMono-Variable.woff2 | https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-mono/GeistMono-Variable.woff2 | fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab |
| LICENSE.txt | https://cdn.jsdelivr.net/npm/geist@1.7.2/LICENSE.txt | 930853ee1daa68554d9e35c8a9175affb74f699fad9a5da6ee5ebe76379d9137 |

Licensed under the SIL Open Font License 1.1 (see LICENSE.txt). Served locally from
/static/fonts; the dashboard never loads fonts from a CDN at runtime.

Verify: `shasum -a 256 src/jobpilot/dashboard/static/fonts/*.woff2`
