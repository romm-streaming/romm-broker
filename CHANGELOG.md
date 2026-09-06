# Changelog

## [0.7.0](https://github.com/romm-streaming/romm-broker/compare/v0.6.0...v0.7.0) (2026-08-30)


### Features

* **room:** run the room socket, audio pipelines and capture rungs in workers ([534c052](https://github.com/romm-streaming/romm-broker/commit/534c052876ab2b3551ef710072178be7ed85a0d0))
* **rpcs3,shadps4:** bound GPU-probe retries, harden cache moves, sweep RPCS3 scratch dirs ([ec51bba](https://github.com/romm-streaming/romm-broker/commit/ec51bba7daf8d485379c8b6b14ec834061feefc7))
* **saves:** label archive members in a manifest, close RetroArch state gaps ([cd6d576](https://github.com/romm-streaming/romm-broker/commit/cd6d576a74798faac165a26162dff5f3343098fd))
* **shadps4,rpcs3:** surface extraction progress, gate pkg/archive on the cache ([adc6359](https://github.com/romm-streaming/romm-broker/commit/adc63597ed0446103a29e268611c465e2b97e61a))
* **shadps4:** unpack pkg/archive ROMs through a CACHE_DIR extraction cache ([d2597cb](https://github.com/romm-streaming/romm-broker/commit/d2597cb47b18d603bae01d70e07c5e70d4060789))


### Bug Fixes

* address full-codebase review findings ([f206891](https://github.com/romm-streaming/romm-broker/commit/f20689112f75032db5b092064cfe2848da624718))
* **api:** retire the session when activate's launch fails ([53ed7de](https://github.com/romm-streaming/romm-broker/commit/53ed7deaf3125bb07e5c1785398e354982754df6))
* **cache:** refuse an extraction that cannot fit before it starts ([a372a93](https://github.com/romm-streaming/romm-broker/commit/a372a93da0d81f1a40e4a7148aac1c99de589089))
* **cemu:** default the TV/Pad audio device instead of leaving it blank ([b6b8c25](https://github.com/romm-streaming/romm-broker/commit/b6b8c2552935041c1274e73484efbf31aae0356b))
* **cemu:** match SDL's name-based fallback GUID for the virtual pad ([71685a2](https://github.com/romm-streaming/romm-broker/commit/71685a2e751ca0f41c93ef5add9daa19e81f9dd2))
* Merge pull request [#24](https://github.com/romm-streaming/romm-broker/issues/24) from romm-streaming/dev ([62b9bad](https://github.com/romm-streaming/romm-broker/commit/62b9badf0228e802ce7a6731c56a72573f1a18ad))
* **room:** cap concurrent viewer seats ([d2637e1](https://github.com/romm-streaming/romm-broker/commit/d2637e1f737a0eb1d99fe6eabf5264ad939de9ed))
* **room:** close a rejoin-evicted seat's still-live socket ([39e4143](https://github.com/romm-streaming/romm-broker/commit/39e414362fd6a0b17801cc4446aca863a773dae2))
* **room:** close reclaim gaps found in a second review pass ([9d0919f](https://github.com/romm-streaming/romm-broker/commit/9d0919faba155ec19ab830b13bb73e9f2f791754))
* **room:** close reclaim gaps found in a third review pass ([c461839](https://github.com/romm-streaming/romm-broker/commit/c461839f3c4db59872de2ce3900fee6898ef30ff))
* **room:** keep seat admission atomic and cover CI for async tests ([d39ed08](https://github.com/romm-streaming/romm-broker/commit/d39ed081c6ee215c64fef716d9389cbb5eae5e20))
* **room:** reclaim disconnected anonymous seats at the cap ([8b13b73](https://github.com/romm-streaming/romm-broker/commit/8b13b73c4f071d0374ff29e093300c444f610e47))
* **room:** share seat-release cleanup between rejoin and reclaim ([340c562](https://github.com/romm-streaming/romm-broker/commit/340c562b92bfdbb24281d1acbd1e20244a262869))
* **room:** stop broadcasting raw bearer tokens in state_update ([be7346e](https://github.com/romm-streaming/romm-broker/commit/be7346e3b01d6b528c9ae4e6f4624be6dcde1714))
* **saves,retroarch:** guard the manifest classifier, log RetroArch's silent state drops ([03c991e](https://github.com/romm-streaming/romm-broker/commit/03c991e3fb5042bc38049aac05391dc1b939e5fc))
* **tests:** wait for a sleeper's argv before recording its pid ([ecd56ab](https://github.com/romm-streaming/romm-broker/commit/ecd56abaae2efcda6e89167678fdfe953d442c8f))


### Documentation

* add CONTRIBUTING.md and set the LICENSE copyright holder ([75bfcc7](https://github.com/romm-streaming/romm-broker/commit/75bfcc7be60082114d9e894ad899146eebf05e85))
* add player/frontend guides and fix documentation audit gaps ([8661a20](https://github.com/romm-streaming/romm-broker/commit/8661a203b93aafeef2b4666a6d3dec2119d5366d))
* add README quickstart, fix developer guide gaps ([e419e3b](https://github.com/romm-streaming/romm-broker/commit/e419e3b8322f08f40a770050d7e09b61ef3c37d6))
* add SECURITY.md, note the desktop terminal in README ([f1cedcc](https://github.com/romm-streaming/romm-broker/commit/f1cedcc0181f15fb6f5a2ffc4c61da721fbe067e))
* use romm-broker as the display name, document optional BIOS volume mount ([009eed3](https://github.com/romm-streaming/romm-broker/commit/009eed393e11b21d2464752aad38a6f2e79f950e))

## [0.6.0](https://github.com/romm-streaming/romm-broker/compare/v0.5.0...v0.6.0) (2026-08-24)


### Features

* bring DuckStation to standalone parity, add RetroArch PS1 core ([99e68e1](https://github.com/romm-streaming/romm-broker/commit/99e68e1909d64205b6d8fe77422156e8f3224427))
* gate Dolphin's whole-card memory sync to GameCube, not Wii ([9e1f398](https://github.com/romm-streaming/romm-broker/commit/9e1f398ac4ae7b06c4714e4bedcd9b18db0d1d32))
* RPCS3 archive (7z/zip/rar) boot support with LRU-evicted cache ([0b58abe](https://github.com/romm-streaming/romm-broker/commit/0b58abe66baa14d479e14e2f0d045e8db3f3cec3))


### Bug Fixes

* address marko review findings in RPCS3 archive support ([15828fe](https://github.com/romm-streaming/romm-broker/commit/15828fe3fe819cbffedbca5541b4befbc838750d))
* address second marko review of emulator modules ([99a94e0](https://github.com/romm-streaming/romm-broker/commit/99a94e02c097443e59c19eace997e765778de69c))
* cap chat DOM nodes, not just the message store ([00c43d1](https://github.com/romm-streaming/romm-broker/commit/00c43d164ec929a32e1bd71a3d65657915cb1b51))
* cap FATX test image size to avoid exhausting CI runner disk ([b48c6d6](https://github.com/romm-streaming/romm-broker/commit/b48c6d675459b19858e0963986586c0579252ac8))
* close gaps found by post-remediation audit ([567b15b](https://github.com/romm-streaming/romm-broker/commit/567b15b891ac7018be30f70c46f1e30f9829c4c2))
* reconnect the room websocket on abnormal closure ([9396a11](https://github.com/romm-streaming/romm-broker/commit/9396a111738c769ec682a6c3944ed0ed43a83ca4))
* repo-wide security remediation from full audit ([6f612f1](https://github.com/romm-streaming/romm-broker/commit/6f612f1afdb142f9d8adde3a823fed989f6411f2))
* saved must not stay true when the state stat fails ([fa572d6](https://github.com/romm-streaming/romm-broker/commit/fa572d6b04098ed162c5d356ae8a241cd6c78106))
* use constant-time compare for the room websocket's controller token ([b57b319](https://github.com/romm-streaming/romm-broker/commit/b57b3192df9227c46f3c2ce1e8896283941c60f0))


### Documentation

* add migration guide from per-emulator brokers to webstation-broker ([4625853](https://github.com/romm-streaming/romm-broker/commit/46258530182a12df26bcb704d48e25a8c9fedef3))
* add RetroArch core BIOS/firmware manifest ([9bff9be](https://github.com/romm-streaming/romm-broker/commit/9bff9be9ef99bcd365d5bc8c48dd821d24cb55b7))

## [0.5.0](https://github.com/romm-streaming/romm-broker/compare/v0.4.0...v0.5.0) (2026-08-22)


### Features

* add boot_failed field to the Emulator base class ([cfc3ab8](https://github.com/romm-streaming/romm-broker/commit/cfc3ab8232d5b1857545e0895a0a576d53cc399b))
* add disc-swap contract to the emulator base class ([61d3c4e](https://github.com/romm-streaming/romm-broker/commit/61d3c4effb3dcd6e94eb5cc11b643b9578f91d2b))
* add PPSSPP emulator module with working save/load-state ([abae001](https://github.com/romm-streaming/romm-broker/commit/abae001fe7375ff3013605a190f6b540f4728ffc))
* add standalone dolphin launcher to the webstation broker ([7949385](https://github.com/romm-streaming/romm-broker/commit/794938522d5205c6844663e53dc18775de82d7e1))
* gate the room comms surface on the session multiplayer flag and add invite links ([9934ccd](https://github.com/romm-streaming/romm-broker/commit/9934ccdee8787408675eb67fe14947e9e6b26cf5))
* generalize PCSX2's deferred-load thread into a boot watchdog ([b556428](https://github.com/romm-streaming/romm-broker/commit/b5564284d0d9aa89e3d49ae4e52694fcc5d61b59))
* prefer m3u playlists on retroarch disc platforms ([2c8dcd9](https://github.com/romm-streaming/romm-broker/commit/2c8dcd9eab25bb42127e57501e48ec5b519a90b1))
* **retroarch:** link core assets so the ppsspp core can boot ([976b1f3](https://github.com/romm-streaming/romm-broker/commit/976b1f3f4c0dde5077dc33bc96ab03d7d83d0bd1))
* **room:** move track capture/presentation onto a worker-based pipeline ([3281f6e](https://github.com/romm-streaming/romm-broker/commit/3281f6eb0572dd42c16cdc10ca2820f649d247e4))
* serve swap-disc on the webstation broker ([f9db17e](https://github.com/romm-streaming/romm-broker/commit/f9db17ea78e2d120caa97e85d3dcf73c45a20dca))
* surface PCSX2 boot-failure detection on GET /api/session/status ([bd109ab](https://github.com/romm-streaming/romm-broker/commit/bd109ab119145b5d9e46caca951dd33691711f6c))
* swap discs on a running retroarch core ([85e8f5c](https://github.com/romm-streaming/romm-broker/commit/85e8f5c71e00278917a227e09e9352fb6391966d))
* sync save states between the webstation broker and RomM ([3663ec4](https://github.com/romm-streaming/romm-broker/commit/3663ec42be9ede35fe23aafc751dbbbea6436ae5))
* sync the whole PS2 memory card as a folder card ([12fcd8c](https://github.com/romm-streaming/romm-broker/commit/12fcd8c66c0dab78eec3a79f950fc34504aef0a0))
* track the retroarch playlist and mounted disc index ([1621d41](https://github.com/romm-streaming/romm-broker/commit/1621d41a9de4d18c08294d97d108ff8273c178fd))
* **xemu:** add XEMU_SOFTWARE_GL to force CPU rendering for xemu alone ([658d0b4](https://github.com/romm-streaming/romm-broker/commit/658d0b46b57b1e523336b8c0a4a964104a524ee0))
* **xemu:** pin fullscreen on startup alongside the renderer ([b9565e6](https://github.com/romm-streaming/romm-broker/commit/b9565e639ea0baa7a9d6bcee27ac18619d0ff678))


### Bug Fixes

* add PPSSPP emulator module with working save/load-state ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))
* disable savestate thumbnails for GPU-rendered dolphin core ([584fc7f](https://github.com/romm-streaming/romm-broker/commit/584fc7feadf14d81c5f07601bc293380d510291a))
* guard against a dead or superseded core committing a disc swap ([bb13df3](https://github.com/romm-streaming/romm-broker/commit/bb13df3969c93a1e05528828f8bed1db0bdef7ca))
* keep the exit state readable after the session is torn down ([6465922](https://github.com/romm-streaming/romm-broker/commit/64659220d83bdfc60d3fa8c5660096f6b868d21a))
* lay down the pcsx2 folder card marker so the slot 1 card is recognized ([c700f96](https://github.com/romm-streaming/romm-broker/commit/c700f96db66bb8ce7034613b47c97c60e7a61aa1))
* lock disc swaps against each other and the deferred resume load ([f8022ad](https://github.com/romm-streaming/romm-broker/commit/f8022adc08d5c125e5451483dde5549fa2bb7480))
* match xemu save directories on the disk's own case ([1caf2c8](https://github.com/romm-streaming/romm-broker/commit/1caf2c87bc6354f474cb148bd101e31af1915198))
* Merge pull request [#13](https://github.com/romm-streaming/romm-broker/issues/13) from romm-streaming/dev ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))
* pin dolphin's gamecube slot a to the gci folder card device ([7bda8c5](https://github.com/romm-streaming/romm-broker/commit/7bda8c5836b5addf77eace4bccd27576a7699a4c))
* pin the retroarch joypad driver to linuxraw so selkies pads register ([37ad1be](https://github.com/romm-streaming/romm-broker/commit/37ad1beedb6d7f83c515779c5d38074615c86feb))
* reap orphaned emulators on broker start and let an exit skip the state save ([5244955](https://github.com/romm-streaming/romm-broker/commit/52449552d1ae9289c7438cd1fdc94bd12e91347f))
* **retroarch:** drop the inline platform table shadowing the json one ([8160d6f](https://github.com/romm-streaming/romm-broker/commit/8160d6fa7bacf6a5b808dc76b8be73092f758578))
* **retroarch:** link ppsspp assets where the core actually reads them ([16ee9e0](https://github.com/romm-streaming/romm-broker/commit/16ee9e00d6551cc27bfec723c20f146d3750fca6))
* run the startup reap on the app that is actually served ([c30e2ca](https://github.com/romm-streaming/romm-broker/commit/c30e2ca603db12ff111d63ad39e41f01f22ec6ee))
* skip a synced memory card left in an older save archive instead of failing the restore ([97cc401](https://github.com/romm-streaming/romm-broker/commit/97cc4019183bca8510d3867922e905c3547e82bf))
* treat resume slot 0 as a resume request, not as no request ([b8536c0](https://github.com/romm-streaming/romm-broker/commit/b8536c0fa2bd6b65d9206b85639668ee9760eea9))
* **xemu:** pin the renderer to OpenGL before each launch ([c1f6f73](https://github.com/romm-streaming/romm-broker/commit/c1f6f732c740cbdd0a06dad400a8f5c1c57a4fba))


### Documentation

* add reverse proxy guide for serving the container from the parent origin ([653bbcf](https://github.com/romm-streaming/romm-broker/commit/653bbcf070631f16bbfe47c02a9a747de1a8346c))
* document the state routes and the retroarch launcher ([cea0c29](https://github.com/romm-streaming/romm-broker/commit/cea0c296d055ca7220d1dfc2afe84f81e15f26a9))
* replace the Zoraxy virtual directory recipe with a host rule ([9e22c68](https://github.com/romm-streaming/romm-broker/commit/9e22c68d95d234de06c4507fa8124ddb90e6605a))
* trim unsupported emulator references from the readme ([13c7aea](https://github.com/romm-streaming/romm-broker/commit/13c7aead50a6ccea9325e45fb5f6cd3e447bd0d8))

## [0.4.0](https://github.com/romm-streaming/romm-broker/compare/v0.3.0...v0.4.0) (2026-08-21)


### Features

* add xenia emulator support ([c8bd030](https://github.com/romm-streaming/romm-broker/commit/c8bd030))
* documentation site built with Fumadocs and deployed to GitHub Pages from a workflow, with the guide split out of the README and a developer reference generated from the Python docstrings


### Documentation

* Google-style docstrings and type hints across the package and the test suite, enforced by ruff's pydocstyle and annotation rules in CI
* move the reverse proxy and emulator setup guides into the docs site and trim the README down to a pointer


### Continuous Integration

* run the test suite and lint the tests alongside the package

## [0.3.0](https://github.com/romm-streaming/romm-broker/compare/v0.2.0...v0.3.0) (2026-08-17)


### Features

* add PPSSPP emulator module with working save/load-state ([abae001](https://github.com/romm-streaming/romm-broker/commit/abae001fe7375ff3013605a190f6b540f4728ffc))


### Bug Fixes

* add PPSSPP emulator module with working save/load-state ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))
* Merge pull request [#13](https://github.com/romm-streaming/romm-broker/issues/13) from romm-streaming/dev ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))

## [0.2.0](https://github.com/romm-streaming/romm-broker/compare/v0.1.0...v0.2.0) (2026-08-17)


### Features

* add boot_failed field to the Emulator base class ([cfc3ab8](https://github.com/romm-streaming/romm-broker/commit/cfc3ab8232d5b1857545e0895a0a576d53cc399b))
* add disc-swap contract to the emulator base class ([61d3c4e](https://github.com/romm-streaming/romm-broker/commit/61d3c4effb3dcd6e94eb5cc11b643b9578f91d2b))
* gate the room comms surface on the session multiplayer flag and add invite links ([9934ccd](https://github.com/romm-streaming/romm-broker/commit/9934ccdee8787408675eb67fe14947e9e6b26cf5))
* generalize PCSX2's deferred-load thread into a boot watchdog ([b556428](https://github.com/romm-streaming/romm-broker/commit/b5564284d0d9aa89e3d49ae4e52694fcc5d61b59))
* prefer m3u playlists on retroarch disc platforms ([2c8dcd9](https://github.com/romm-streaming/romm-broker/commit/2c8dcd9eab25bb42127e57501e48ec5b519a90b1))
* **retroarch:** link core assets so the ppsspp core can boot ([976b1f3](https://github.com/romm-streaming/romm-broker/commit/976b1f3f4c0dde5077dc33bc96ab03d7d83d0bd1))
* **room:** move track capture/presentation onto a worker-based pipeline ([3281f6e](https://github.com/romm-streaming/romm-broker/commit/3281f6eb0572dd42c16cdc10ca2820f649d247e4))
* serve swap-disc on the webstation broker ([f9db17e](https://github.com/romm-streaming/romm-broker/commit/f9db17ea78e2d120caa97e85d3dcf73c45a20dca))
* surface PCSX2 boot-failure detection on GET /api/session/status ([bd109ab](https://github.com/romm-streaming/romm-broker/commit/bd109ab119145b5d9e46caca951dd33691711f6c))
* swap discs on a running retroarch core ([85e8f5c](https://github.com/romm-streaming/romm-broker/commit/85e8f5c71e00278917a227e09e9352fb6391966d))
* track the retroarch playlist and mounted disc index ([1621d41](https://github.com/romm-streaming/romm-broker/commit/1621d41a9de4d18c08294d97d108ff8273c178fd))
* **xemu:** add XEMU_SOFTWARE_GL to force CPU rendering for xemu alone ([658d0b4](https://github.com/romm-streaming/romm-broker/commit/658d0b46b57b1e523336b8c0a4a964104a524ee0))
* **xemu:** pin fullscreen on startup alongside the renderer ([b9565e6](https://github.com/romm-streaming/romm-broker/commit/b9565e639ea0baa7a9d6bcee27ac18619d0ff678))


### Bug Fixes

* guard against a dead or superseded core committing a disc swap ([bb13df3](https://github.com/romm-streaming/romm-broker/commit/bb13df3969c93a1e05528828f8bed1db0bdef7ca))
* lock disc swaps against each other and the deferred resume load ([f8022ad](https://github.com/romm-streaming/romm-broker/commit/f8022adc08d5c125e5451483dde5549fa2bb7480))
* match xemu save directories on the disk's own case ([1caf2c8](https://github.com/romm-streaming/romm-broker/commit/1caf2c87bc6354f474cb148bd101e31af1915198))
* pin the retroarch joypad driver to linuxraw so selkies pads register ([37ad1be](https://github.com/romm-streaming/romm-broker/commit/37ad1beedb6d7f83c515779c5d38074615c86feb))
* reap orphaned emulators on broker start and let an exit skip the state save ([5244955](https://github.com/romm-streaming/romm-broker/commit/52449552d1ae9289c7438cd1fdc94bd12e91347f))
* **retroarch:** drop the inline platform table shadowing the json one ([8160d6f](https://github.com/romm-streaming/romm-broker/commit/8160d6fa7bacf6a5b808dc76b8be73092f758578))
* **retroarch:** link ppsspp assets where the core actually reads them ([16ee9e0](https://github.com/romm-streaming/romm-broker/commit/16ee9e00d6551cc27bfec723c20f146d3750fca6))
* run the startup reap on the app that is actually served ([c30e2ca](https://github.com/romm-streaming/romm-broker/commit/c30e2ca603db12ff111d63ad39e41f01f22ec6ee))
* treat resume slot 0 as a resume request, not as no request ([b8536c0](https://github.com/romm-streaming/romm-broker/commit/b8536c0fa2bd6b65d9206b85639668ee9760eea9))
* **xemu:** pin the renderer to OpenGL before each launch ([c1f6f73](https://github.com/romm-streaming/romm-broker/commit/c1f6f732c740cbdd0a06dad400a8f5c1c57a4fba))


### Documentation

* trim unsupported emulator references from the readme ([13c7aea](https://github.com/romm-streaming/romm-broker/commit/13c7aead50a6ccea9325e45fb5f6cd3e447bd0d8))

## 0.1.0 (2026-08-08)


### Features

* add standalone dolphin launcher to the webstation broker ([7949385](https://github.com/romm-streaming/romm-broker/commit/794938522d5205c6844663e53dc18775de82d7e1))
* sync save states between the webstation broker and RomM ([3663ec4](https://github.com/romm-streaming/romm-broker/commit/3663ec42be9ede35fe23aafc751dbbbea6436ae5))
* sync the whole PS2 memory card as a folder card ([12fcd8c](https://github.com/romm-streaming/romm-broker/commit/12fcd8c66c0dab78eec3a79f950fc34504aef0a0))


### Bug Fixes

* disable savestate thumbnails for GPU-rendered dolphin core ([584fc7f](https://github.com/romm-streaming/romm-broker/commit/584fc7feadf14d81c5f07601bc293380d510291a))
* keep the exit state readable after the session is torn down ([6465922](https://github.com/romm-streaming/romm-broker/commit/64659220d83bdfc60d3fa8c5660096f6b868d21a))
* lay down the pcsx2 folder card marker so the slot 1 card is recognized ([c700f96](https://github.com/romm-streaming/romm-broker/commit/c700f96db66bb8ce7034613b47c97c60e7a61aa1))
* pin dolphin's gamecube slot a to the gci folder card device ([7bda8c5](https://github.com/romm-streaming/romm-broker/commit/7bda8c5836b5addf77eace4bccd27576a7699a4c))
* skip a synced memory card left in an older save archive instead of failing the restore ([97cc401](https://github.com/romm-streaming/romm-broker/commit/97cc4019183bca8510d3867922e905c3547e82bf))


### Documentation

* add reverse proxy guide for serving the container from the parent origin ([653bbcf](https://github.com/romm-streaming/romm-broker/commit/653bbcf070631f16bbfe47c02a9a747de1a8346c))
* document the state routes and the retroarch launcher ([cea0c29](https://github.com/romm-streaming/romm-broker/commit/cea0c296d055ca7220d1dfc2afe84f81e15f26a9))
* replace the Zoraxy virtual directory recipe with a host rule ([9e22c68](https://github.com/romm-streaming/romm-broker/commit/9e22c68d95d234de06c4507fa8124ddb90e6605a))
