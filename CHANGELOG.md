# Changelog

## [0.11.0](https://github.com/romm-streaming/romm-broker/compare/v0.10.0...v0.11.0) (2026-09-26)


### Features

* **pcsx2:** fetch patches.zip before a launch when missing, invalid or a week old ([71b9f16](https://github.com/romm-streaming/romm-broker/commit/71b9f16b2dec0d1f43a55f21834bbb2e118d5a77))
* **pcsx2:** fetch patches.zip from pcsx2_patches with an unprivileged download and a sudo install ([46295c8](https://github.com/romm-streaming/romm-broker/commit/46295c8a81367db0498ae5da777440d9291e66c9))
* **retroarch:** add nintendo-dsi on the melondsds core ([3a8e7a3](https://github.com/romm-streaming/romm-broker/commit/3a8e7a31c9f1187315f02ac8af5ee8298931b905))


### Bug Fixes

* **dolphin:** lay a pushed GameCube card where the folder card reads it ([092396a](https://github.com/romm-streaming/romm-broker/commit/092396a63aa12f34739bc29db081b65eae264157))
* **pcsx2:** point patches.zip at /usr/share/PCSX2/resources and validate its contents ([c0adfcd](https://github.com/romm-streaming/romm-broker/commit/c0adfcde587ca5fe058ec0d66fcb84e8e917da30))
* **retroarch:** scope 3ds save_subtrees to the core's real doubled Azahar path ([7bbf91e](https://github.com/romm-streaming/romm-broker/commit/7bbf91e1e11387beeab13f46c55d63cf42756e38))
* **saves:** refuse restores that would fail after the slot is cleared ([48178ff](https://github.com/romm-streaming/romm-broker/commit/48178ffdfc506ae666059bab1cb48a16d0c98f57))


### Documentation

* full Traefik recipe and shared Docker network setup ([6c452c7](https://github.com/romm-streaming/romm-broker/commit/6c452c707e26d98cab383f70570a0c066998f80a))
* install pytest-asyncio in every dev setup command and refresh the test count ([5567f7d](https://github.com/romm-streaming/romm-broker/commit/5567f7d1ab6fdf4c77baf5089a14c5989304f987))
* mount the library at /romm/library like RomM, and explain a missing rom ([9ed7c56](https://github.com/romm-streaming/romm-broker/commit/9ed7c56a22443e469c56652147c44efc47596989))
* update migrating example ([d6fb797](https://github.com/romm-streaming/romm-broker/commit/d6fb79758b26ee5e57b8bdee925fc8d491c219cb))
* update migrating example ([80dbf4f](https://github.com/romm-streaming/romm-broker/commit/80dbf4fd86eeff4485e8867710b79754dccb1552))

## [0.10.0](https://github.com/romm-streaming/romm-broker/compare/v0.9.0...v0.10.0) (2026-09-24)


### Features

* add shared cache-key fingerprinting ([4b0075b](https://github.com/romm-streaming/romm-broker/commit/4b0075b83addd696b3228d692836acf93538d5ac))
* add shared dir-sizing and last-accessed marker helpers ([9632f3d](https://github.com/romm-streaming/romm-broker/commit/9632f3dd4ed07b774c753970a22baa1df37b118e))
* add shared LRU eviction with an on_evict hook ([f6f35ff](https://github.com/romm-streaming/romm-broker/commit/f6f35ff716666828433cb053eb538f93d7d01bd0))
* add shared per-instance locking and scratch cleanup ([27ee55a](https://github.com/romm-streaming/romm-broker/commit/27ee55acadacf80f01eaba29526effe4bd7d237a))
* add shared two-figure disk-space guard ([429ac3c](https://github.com/romm-streaming/romm-broker/commit/429ac3c7a4f9ff20b1729106d7a7359da3826062))
* add the shared default listing-based budget ([56be69f](https://github.com/romm-streaming/romm-broker/commit/56be69f3b169b225ff75d19998289dc7701d5675))
* add the shared default safe-extract stage ([124471d](https://github.com/romm-streaming/romm-broker/commit/124471d2a94e74c71eaf0add2b45122db9b0e730))
* add the shared extract() orchestration method ([5597c3c](https://github.com/romm-streaming/romm-broker/commit/5597c3c38af7d2400af29ada645f8516289ea338))
* scaffold the shared ExtractionCache class ([7982dfe](https://github.com/romm-streaming/romm-broker/commit/7982dfe818246c79de78b0f2dfb01561d67b59ff))


### Bug Fixes

* correct the eviction test's cap so it actually forces eviction ([3c92c8f](https://github.com/romm-streaming/romm-broker/commit/3c92c8fa17d6344f59ce1887997ce27bfc8b1da0))
* **extraction-cache:** fix log drift and doubled sweep name, dedupe shared constants, document extract() as unsupported for rpcs3/shadps4 ([4cb03ea](https://github.com/romm-streaming/romm-broker/commit/4cb03ea582cc49a39d06214ffd26620258313ea3))
* **pcsx2:** validate cached patches.zip before every launch ([32518c7](https://github.com/romm-streaming/romm-broker/commit/32518c781e74dcc1731c755d1245aef0b2a959c3))
* restore forward-looking imports for later ExtractionCache tasks ([67061e3](https://github.com/romm-streaming/romm-broker/commit/67061e325a27b4068e0cd20495277f33617a22dc))
* **retroarch:** enable .srm save import/export for nds ([fd338f8](https://github.com/romm-streaming/romm-broker/commit/fd338f8cbf7c7ac00b3dbd77acc54b9315bf0405))
* **retroarch:** isolate core-owned saves that live outside SAVE_DIR ([e1d3bf5](https://github.com/romm-streaming/romm-broker/commit/e1d3bf50033ad4953d9dd1616a4fa1157b5dc7a0))
* **retroarch:** isolate core-owned saves that live outside SAVE_DIR ([d769c6a](https://github.com/romm-streaming/romm-broker/commit/d769c6a9b72f8797c7bfa81bd2e62bf027c4ef23))
* **retroarch:** switch nds to the maintained melondsds core ([81bb50c](https://github.com/romm-streaming/romm-broker/commit/81bb50c9a3658109bf9c91d30b4864839f70baf6))
* **rpcs3:** drop dead missing_target_error config and stale docstring reference ([b9f6a09](https://github.com/romm-streaming/romm-broker/commit/b9f6a098c0943209d1797787a172ca0fd06d75f2))
* **saves:** refuse bzip2 and lzma members instead of unbounded-decompressing them ([cb714b7](https://github.com/romm-streaming/romm-broker/commit/cb714b754f0e217b2f47efebc841f6a4b2ee21e5))

## [0.9.0](https://github.com/romm-streaming/romm-broker/compare/v0.8.5...v0.9.0) (2026-09-21)


### Features

* **activate:** preflight declared imports before the clear and record placements ([218b5c4](https://github.com/romm-streaming/romm-broker/commit/218b5c466b496985348cd0f4f29589afd90b5937))
* **api:** accept RomM's title_id and save_target on activate ([bb46efd](https://github.com/romm-streaming/romm-broker/commit/bb46efdf8cc14063743cfea08a5d9368e340a863))
* **api:** add the import-spec discovery route ([e31fa34](https://github.com/romm-streaming/romm-broker/commit/e31fa3480716fe9429f67b7422d6adbb72fdcd91))
* **azahar:** accept declared save imports and rewrite the console ids ([35a6325](https://github.com/romm-streaming/romm-broker/commit/35a6325692bec5be18531ee0c9858e916e858776))
* **cemu:** accept declared save imports under the account Cemu created ([54749ef](https://github.com/romm-streaming/romm-broker/commit/54749ef22ff4041b6752b954286fca30e9bb9730))
* **dolphin:** accept declared GameCube cards, Wii NAND saves and states ([ca6b142](https://github.com/romm-streaming/romm-broker/commit/ca6b14294be32b85c6c4aa952ad4f3698b2204a3))
* **dolphin:** read a game id out of bytes, and normalise a full Wii title id ([2099339](https://github.com/romm-streaming/romm-broker/commit/20993394fcceadfb9af042d25e96a0690a2a9f85))
* **duckstation:** pin the shared memory card and accept cards and resume states as imports ([eac417f](https://github.com/romm-streaming/romm-broker/commit/eac417f0f48eadf75520cfbfd093cd670893dc66))
* **eden:** accept declared save imports with their profile store ([fb97713](https://github.com/romm-streaming/romm-broker/commit/fb977135079c900d8762b46d5f18277e2b6d6193))
* **flycast:** accept VMU saves, cards and marker-owned resume states as imports ([9fd1583](https://github.com/romm-streaming/romm-broker/commit/9fd1583b03fd2e1b5a05c537a8502fa08a9546ff))
* **imports:** add kind companions, unit subtrees and the push-route identity checks ([44a46d7](https://github.com/romm-streaming/romm-broker/commit/44a46d774e1837ade0ce76e9f79ddd32ac741e4f))
* **imports:** add owner_marker_sidecar for marker-owned resume states ([10ce7d9](https://github.com/romm-streaming/romm-broker/commit/10ce7d91a3ba6fca24ad15ff1e2ee9e4d98c150e))
* **imports:** add preflight and the emulator import hooks, refusing by default ([d726de1](https://github.com/romm-streaming/romm-broker/commit/d726de1be2ac2d92cc87f52915d46cc86238c486))
* **imports:** add the declared-import types and refusal body ([fc560b0](https://github.com/romm-streaming/romm-broker/commit/fc560b08e74694db444b4ba73a94a2d5d7f68c91))
* **imports:** add the kind gate and shared placement helpers ([dfd56ec](https://github.com/romm-streaming/romm-broker/commit/dfd56ec76eb50eef1cebd6a18d446373df88b761))
* **imports:** check an import plan as a whole before anything is written ([49b2279](https://github.com/romm-streaming/romm-broker/commit/49b227972bf9a10af0baa20f509fa00ad85030ed))
* **imports:** parse the version 2 manifest's declarations ([6eefb99](https://github.com/romm-streaming/romm-broker/commit/6eefb994f7e50e063eac4035ddd9f47b605a9851))
* **imports:** refine refusals by declared origin and suggest an emulator ([f203747](https://github.com/romm-streaming/romm-broker/commit/f2037476d53927d35ed7a4f5e0b2473f29ef595e))
* **imports:** refuse unreadable members as unreadable_member, not unsafe_path ([16666d1](https://github.com/romm-streaming/romm-broker/commit/16666d109dfee4cab9d96ca09438f96a07e1b907))
* **imports:** refuse unsafe import member paths in one pass ([6ad0f9b](https://github.com/romm-streaming/romm-broker/commit/6ad0f9bdf8404f16a7a2c39facca670bf10a4227))
* **imports:** resolve and check game identity for imports ([b21f727](https://github.com/romm-streaming/romm-broker/commit/b21f727a6e07a18a951808f91bfee0a911cfb47a))
* **pcsx2:** accept declared folder memory cards ([1b0de57](https://github.com/romm-streaming/romm-broker/commit/1b0de57e4b3b0ac13299b0c68668dea532ecb09b))
* **ppsspp:** accept declared PSP save folders and one state with its screenshot ([5ba749a](https://github.com/romm-streaming/romm-broker/commit/5ba749a2bec3f7b16d04c1118f434135537bd6b6))
* **retroarch:** import Wii NAND and 3DS saves, keep GameCube refused ([08b9e49](https://github.com/romm-streaming/romm-broker/commit/08b9e497f5bd22fcd1535d065ccab5bf574619b0))
* **retroarch:** pin the sorted save dirs and accept .srm saves as imports ([451da72](https://github.com/romm-streaming/romm-broker/commit/451da72c22ba23afb714cb67a3a15550fec74265))
* **rpcs3:** import save folders, game data and one savestate ([405267e](https://github.com/romm-streaming/romm-broker/commit/405267ea9d0b37af9934885aa4c0889e3157124b))
* **saves:** check a v1 restore's members without writing them ([9c27f78](https://github.com/romm-streaming/romm-broker/commit/9c27f789a8f0e861c0a7f6eee95fe4376857eadc))
* **saves:** read an archive's members in one pass before restoring ([e4b81f7](https://github.com/romm-streaming/romm-broker/commit/e4b81f789bc7830b6f4a8b2bd895d7834e2024a3))
* **saves:** ship a session's placed imports in its exit dump ([f15dab7](https://github.com/romm-streaming/romm-broker/commit/f15dab7e7d1202398b6dba0135418cf032884f85))
* **scummvm:** import saves and one state by target ([38f5fe7](https://github.com/romm-streaming/romm-broker/commit/38f5fe7d6b122f8c6a4811be7f7d98f771e9b40d))
* **shadps4:** accept declared save imports and re-root them to the default user ([88e375e](https://github.com/romm-streaming/romm-broker/commit/88e375ec160c556746d130ab02d65ade7ad94a86))
* **xemu:** accept declared save imports under the session's title ([b57e728](https://github.com/romm-streaming/romm-broker/commit/b57e7287bbc969e1c851714be9be89e60c36ad6a))
* **xenia:** accept declared save imports and read the signed-in profile's XUID ([6832e5f](https://github.com/romm-streaming/romm-broker/commit/6832e5fe1b92795af8cfb8fc8cea31651997e599))


### Bug Fixes

* **activate:** refuse a bad save archive before clearing the slot ([3fc139d](https://github.com/romm-streaming/romm-broker/commit/3fc139dba6e90bf639623a1ea4ec126e27631211))
* **api:** say an unknown save_target_layout is passed through ([0578595](https://github.com/romm-streaming/romm-broker/commit/05785954ccf21ba57f58193df51b0802496a02af))
* **dolphin:** let Dolphin's own config pick the video backend ([12209dc](https://github.com/romm-streaming/romm-broker/commit/12209dc899b55a859a96261b0d56d3440eb9e3be))
* **duckstation,rpcs3:** serve the exit state a saving exit confirmed through the state-file GET so RomM can file it ([3c6d529](https://github.com/romm-streaming/romm-broker/commit/3c6d529c2b455b1ef39a378c7c528b26276f6034))
* **duckstation:** normalize archive paths in the carried-card check and skip a non-file pinned card ([9cf53fb](https://github.com/romm-streaming/romm-broker/commit/9cf53fb241d43d8cef9a6970e35d38236f9bb62f))
* **emulators:** fullmatch state and card names as ASCII, and push names through check_state_basename ([8b07dbc](https://github.com/romm-streaming/romm-broker/commit/8b07dbc4bee3dd137931cd3857d3801e38320404))
* **imports:** hold Azahar, Cemu, shadPS4 and xemu saves to the launched game ([2f08b42](https://github.com/romm-streaming/romm-broker/commit/2f08b4200652028cec56b3dda94c025030b2e614))
* **imports:** hold sidecars to the placement checks and suggest retroarch only for libretro states ([82d9fd2](https://github.com/romm-streaming/romm-broker/commit/82d9fd29814cd043fc534db7eb4be2c509a6c609))
* **imports:** keep the xemu registry tests off /config and correct two docs entries ([cb566eb](https://github.com/romm-streaming/romm-broker/commit/cb566eb9465b82f1395f0e304de1ec10f5e05e14))
* **imports:** key the identity memo on every input and read ids as ASCII ([aa84991](https://github.com/romm-streaming/romm-broker/commit/aa84991d993faeaefb998348535cc142f16ca7cf))
* **imports:** log both ids and the override hint when a pushed state names another game ([b23ef27](https://github.com/romm-streaming/romm-broker/commit/b23ef272d074b4976a49dc1b4acef4a548c0aaf1))
* **imports:** read a non-string manifest origin as unknown ([d149c02](https://github.com/romm-streaming/romm-broker/commit/d149c0212b4a8f7fb1f1f3c71d931635ba4b0c30))
* **imports:** refuse a file/directory clash between destinations before the clear ([2d152e9](https://github.com/romm-streaming/romm-broker/commit/2d152e9b2350cfdf7f521ac3d4a46abd33be47eb))
* **imports:** refuse C1, bidi and duplicate names; catch failed head() reads ([97cbebb](https://github.com/romm-streaming/romm-broker/commit/97cbebbdfe236c0aba3f4a0d05044b39e7621416))
* **imports:** refuse, not crash, when a renamer rejects a name ([39d7f8c](https://github.com/romm-streaming/romm-broker/commit/39d7f8cfdbdac86215498d04a4ae9c4ddf081acc))
* **logging:** bound the import block and log import refusals at warning ([c690ce7](https://github.com/romm-streaming/romm-broker/commit/c690ce76b00ae9ac5fb6870248eb0e5653286651))
* **pcsx2:** take save_root from DATA_DIR so restore and PCSX2 agree on the tree ([ed1aea4](https://github.com/romm-streaming/romm-broker/commit/ed1aea450e06cac1e693b1c683755915ff231c27))
* **ppsspp:** keep the undo states from counting against an imported state ([bc9fc53](https://github.com/romm-streaming/romm-broker/commit/bc9fc53bc62a87c0da02fdbda4756264ebfff821))
* **retroarch:** accept .srm only on platforms whose core loads it ([09f8838](https://github.com/romm-streaming/romm-broker/commit/09f8838940c5ac9ecf14142bd779b30ba04753ea))
* **retroarch:** derive the state leaf regex from the suffix regex and assert the renamed .srm is accepted ([140031e](https://github.com/romm-streaming/romm-broker/commit/140031e911dae67a466a1b124c4c9ec0ba0ca02c))
* **room:** start the stream at 60% volume on a first visit ([24f5369](https://github.com/romm-streaming/romm-broker/commit/24f5369406a36e8221d7b150cfdc097d3127030d))
* **saves:** let RPCS3's savestates link through as a declared link root ([e438aac](https://github.com/romm-streaming/romm-broker/commit/e438aac35e923825f01d43bc3badbdde2f995bb5))
* **saves:** read every planned member before the working slot is cleared ([9841a87](https://github.com/romm-streaming/romm-broker/commit/9841a874ab23fe0a506fac14aa1fa03d308f3e9a))
* **saves:** refuse unreadable archive members before the slot is cleared ([710567f](https://github.com/romm-streaming/romm-broker/commit/710567ff9375a0c3dd8afea5e28fef523d4ba029))
* **xenia:** drop the removed --headless flag and give Xenia a terminal so launch errors reach the log ([cf2b3c6](https://github.com/romm-streaming/romm-broker/commit/cf2b3c63b2cb983ce7c44c6f415bc9957a6adb0c))
* **xenia:** drop the removed --headless flag and give Xenia a terminal so launch errors reach the log ([2c1ac89](https://github.com/romm-streaming/romm-broker/commit/2c1ac89334a3224306f7e11a5e4541d0892eeb20))


### Documentation

* **api:** correct import refusal, discovery and out-of-subtree wording ([f6cda86](https://github.com/romm-streaming/romm-broker/commit/f6cda86434563a33744c7925d64e8fcb5a8bcc56))
* **api:** document declared imports and every refusal reason ([9c1e230](https://github.com/romm-streaming/romm-broker/commit/9c1e23023d518087ba58122f748ec8f363dd32fb))
* **contributing:** require signed commits ([a6b3c3f](https://github.com/romm-streaming/romm-broker/commit/a6b3c3fa1113f60672609fa3d924fcc6496d1b2d))
* **contributing:** require signed commits ([84d51d0](https://github.com/romm-streaming/romm-broker/commit/84d51d066805a74c90de4c1c3c7bd64ae4743799))
* correct the pinned-card comment and the RetroArch memcard advice ([3f63c32](https://github.com/romm-streaming/romm-broker/commit/3f63c3254cd87c0b2eed8cda774ce29ce456fce6))
* **imports:** document PPSSPP, Dolphin and PCSX2 imports ([7ba75a2](https://github.com/romm-streaming/romm-broker/commit/7ba75a27601aac459d9f33416945e5fad17354f5))
* **imports:** document RPCS3, ScummVM and RetroArch Wii and 3DS imports ([7e6b7b4](https://github.com/romm-streaming/romm-broker/commit/7e6b7b447dec24824d472e8da380f34bf4cf5da5))
* **imports:** document the Cemu, Eden, Azahar, xemu, Xenia and shadPS4 imports ([c97b3ce](https://github.com/romm-streaming/romm-broker/commit/c97b3ce6d3b2bce84cb94a95fc3e7d935c27061d))
* **imports:** fix the core-name, resume_slot and manifest-example wording ([5e54cd1](https://github.com/romm-streaming/romm-broker/commit/5e54cd15349967c295797c9e889f2eb37be8ae25))
* **imports:** narrow the loose data.bin and Dolphin push-check wording ([b9471fe](https://github.com/romm-streaming/romm-broker/commit/b9471fe8cd8e19104e1d4485796e46d516672961))
* point webstation examples at taisun/random-images:webstation-romm ([9c31cca](https://github.com/romm-streaming/romm-broker/commit/9c31cca93c4b5006964bdc87e6e00b12498e8b17))

## [0.8.5](https://github.com/romm-streaming/romm-broker/compare/v0.8.4...v0.8.5) (2026-09-17)


### Bug Fixes

* **room:** keep a member's tile up when their camera turns off ([0071584](https://github.com/romm-streaming/romm-broker/commit/0071584c0bdf1d177b3b40af52736399f9c4fd59))
* **room:** keep the self tile up when the camera turns off ([ba73064](https://github.com/romm-streaming/romm-broker/commit/ba73064f1093f25a97c32b452cc2174e775e8e90))


### Documentation

* clarify Activate on the broker settings page ([b811e3a](https://github.com/romm-streaming/romm-broker/commit/b811e3a3bf82f7e737169db519c6e8716398a57f))
* cut implementation-defense noise from the emulator settings page ([e837edc](https://github.com/romm-streaming/romm-broker/commit/e837edca77b3d8021faa6f3100cf9b02d67646b7))
* cut remaining tone issues from the docs site ([88d598d](https://github.com/romm-streaming/romm-broker/commit/88d598dce215ffd7d6c00b5e19a684d9c2a45674))
* fix typos across the docs site ([cd0b67f](https://github.com/romm-streaming/romm-broker/commit/cd0b67f7be3692c01ee51bc5a5ec4ce890bbcb55))

## [0.8.4](https://github.com/romm-streaming/romm-broker/compare/v0.8.3...v0.8.4) (2026-09-16)


### Bug Fixes

* **azahar:** empty the SD and NAND save trees before the restore ([149ef9d](https://github.com/romm-streaming/romm-broker/commit/149ef9d3f7ba18d3f8802cca4cc0943cc399095e))
* **cemu:** empty the last session's Wii U saves before the restore ([c2f92f3](https://github.com/romm-streaming/romm-broker/commit/c2f92f3549c265b95dcfe77a0abbbc285221c6b9)), closes [#42](https://github.com/romm-streaming/romm-broker/issues/42)
* **dolphin:** empty the GC cards and the Wii NAND, not just the state slot ([2f6b70e](https://github.com/romm-streaming/romm-broker/commit/2f6b70e265fde958b8e30fb1814ed17159eefeca))
* **duckstation:** empty the memory cards alongside the resume states ([9d9952b](https://github.com/romm-streaming/romm-broker/commit/9d9952b654eb25193cc74c28fe672597405f9874))
* **emulators:** make emptying the save tree part of the activate contract ([cb3c826](https://github.com/romm-streaming/romm-broker/commit/cb3c8262b0f5f6b9ed8c3fa0d8ce8773b42ea7d5))
* **emulators:** stop the config knobs from desyncing broker and emulator ([3bae05f](https://github.com/romm-streaming/romm-broker/commit/3bae05fd065189fba9cfb8fc0d0535976e347b85))
* **emulators:** stop the save-tree knobs from desyncing broker and emulator ([ec12e27](https://github.com/romm-streaming/romm-broker/commit/ec12e277ef3902d30f3de41937f5ca32f403349b))
* **emulators:** stop the state-watch knobs from desyncing broker and emulator ([d29c774](https://github.com/romm-streaming/romm-broker/commit/d29c7740689536b1748a6bb5e2cd47e870183ffe))
* **pcsx2:** empty the memory cards alongside the state slot ([eba2b75](https://github.com/romm-streaming/romm-broker/commit/eba2b75a0088fd4ee7957384e667578f8ff499af))
* **ppsspp:** empty the memory stick saves, not just the broker's state slot ([db864dd](https://github.com/romm-streaming/romm-broker/commit/db864ddfd31e75e7d35d0fa729b609a18beee299))
* **scummvm:** empty the whole save directory, and stop shipping torn saves ([171d65e](https://github.com/romm-streaming/romm-broker/commit/171d65ef9369a479dd5fb755394740810eb8e853))
* **xemu:** clear the launched title's saves off the HDD image before injecting ([fec8cec](https://github.com/romm-streaming/romm-broker/commit/fec8cec383ede6338d11bef012710b0f2bb6b5db))


### Documentation

* add a quickstart walkthrough and a general troubleshooting page ([8903697](https://github.com/romm-streaming/romm-broker/commit/89036978ae656432699c3371e95588662057b193))
* humanize prose and fix drift from actual broker behavior ([d908ec2](https://github.com/romm-streaming/romm-broker/commit/d908ec23db06693955d3fe53ea50a2218f1c09b7))

## [0.8.3](https://github.com/romm-streaming/romm-broker/compare/v0.8.2...v0.8.3) (2026-09-15)


### Bug Fixes

* **cemu, azahar:** stop the directory knobs from desyncing broker and emulator ([df18695](https://github.com/romm-streaming/romm-broker/commit/df1869582253b0f3c20ad992a25ad2cb4e39b799))
* **desktop:** close apps left open when the desktop session ends ([3953ef5](https://github.com/romm-streaming/romm-broker/commit/3953ef564e19054e841cbf851e5da49c4b5bb41f))
* **desktop:** close tagged apps on every path that drops the pid record ([84cd20a](https://github.com/romm-streaming/romm-broker/commit/84cd20ad589c1758c5c07dbc474f7ff9fe79785d))
* **dolphin:** repair pad bindings already seeded with the wrong name ([1f21ca7](https://github.com/romm-streaming/romm-broker/commit/1f21ca72630d82a24b6efa8558442fd1d5a9a7f5))
* **dolphin:** seed GCPad bindings with the SDL-backend pad name ([f1f79a8](https://github.com/romm-streaming/romm-broker/commit/f1f79a89409c27f97deb7c243d682b430a745075))
* **dolphin:** share one config directory with the desktop launcher ([50e981b](https://github.com/romm-streaming/romm-broker/commit/50e981b6819812f4a21457d08b7345dccd406ffc))
* **room:** bound the pointer lock retries and split the two waits ([3ccb396](https://github.com/romm-streaming/romm-broker/commit/3ccb39699240b4c285bc4a4f8e7846bdf078fee6))
* **room:** fall back to a manual copy when clipboard-write is blocked ([c11b1ff](https://github.com/romm-streaming/romm-broker/commit/c11b1ff56b5df7649a75730101bfaa6af768aaa9))


### Documentation

* cover pooling rules in the multi-container reverse-proxy section ([9f72079](https://github.com/romm-streaming/romm-broker/commit/9f720798339d6ad85d62dac1c2df8ce67ddbe22b))
* drop the audio known-issue, fixed upstream in the current image ([691ee58](https://github.com/romm-streaming/romm-broker/commit/691ee582aaf82e2b72c8bbe70b7699660006b570))

## [0.8.2](https://github.com/romm-streaming/romm-broker/compare/v0.8.1...v0.8.2) (2026-09-14)


### Bug Fixes

* Merge pull request [#36](https://github.com/romm-streaming/romm-broker/issues/36) from thelamer/master ([7c3ba01](https://github.com/romm-streaming/romm-broker/commit/7c3ba01d972af056db40d77b81f9e6ef1bd70bbe))
* **room:** gate gaming mode on mouse and keyboard ownership ([dc5ef34](https://github.com/romm-streaming/romm-broker/commit/dc5ef34513ca62e0a7a8d4b617beea171ab90ae9))
* Use proper socket, make grabbing a screenshot universal ([7c3ba01](https://github.com/romm-streaming/romm-broker/commit/7c3ba01d972af056db40d77b81f9e6ef1bd70bbe))


### Documentation

* cover the stream controls and gaming mode in the bar ([d409f91](https://github.com/romm-streaming/romm-broker/commit/d409f9198198309735c0cbd74120429b55952568))
* document PCSX2 GS init failure on AMD iGPU as a Vulkan/RADV issue ([88dde26](https://github.com/romm-streaming/romm-broker/commit/88dde26c00c9ee3f1945fd0bcea30d0df725cfe2))
* document the conventions and security invariants inline ([52c1cda](https://github.com/romm-streaming/romm-broker/commit/52c1cdaddb1d7e336abccb35be95b3f4c8db0ede))

## [0.8.1](https://github.com/romm-streaming/romm-broker/compare/v0.8.0...v0.8.1) (2026-09-12)


### Bug Fixes

* escape glob metacharacters in save-state filename lookup ([88e2366](https://github.com/romm-streaming/romm-broker/commit/88e23668619a59ff61b5ae2c5c81d84e9ce22b82))
* give PSP a longer state-save confirmation window ([c6dac1e](https://github.com/romm-streaming/romm-broker/commit/c6dac1e7c037844a2de02a9d5375f3730a9f1ca8))
* **retroarch:** anchor first-save settle to PLAYING, not process spawn ([915f50f](https://github.com/romm-streaming/romm-broker/commit/915f50ffd619b9ec167c5c0c60a7a53bec6cfb96))
* **retroarch:** detect the wayland display selkies is capturing ([a30df05](https://github.com/romm-streaming/romm-broker/commit/a30df05a0f9648acceebaf92d87901c07916f890))
* **retroarch:** settle first save until a real frame renders ([ab9b42f](https://github.com/romm-streaming/romm-broker/commit/ab9b42f61cc8fa1485d96adec0647e2a7a544867))
* **test:** filter TestResumeGate's thread stub to the deferred-load thread ([46c9a9c](https://github.com/romm-streaming/romm-broker/commit/46c9a9cad86267607be974415936851cd14c15f8))


### Documentation

* document the /dev/nvidia-modeset node NVIDIA presentation needs ([4e0ed6f](https://github.com/romm-streaming/romm-broker/commit/4e0ed6fe379467e5b64d039ca3f7c569a5e3e4ff)), closes [#32](https://github.com/romm-streaming/romm-broker/issues/32)
* record the stale audio.lock that kills Selkies audio on restart ([d6868bf](https://github.com/romm-streaming/romm-broker/commit/d6868bfb75872bff9a6bf7a412b515dfce57f237))

## [0.8.0](https://github.com/romm-streaming/romm-broker/compare/v0.7.0...v0.8.0) (2026-09-06)


### Features

* **scummvm:** add a ScummVM launcher ([7057270](https://github.com/romm-streaming/romm-broker/commit/7057270ab5f690ddb13e2b21e16f89aead1d4b5b))


### Bug Fixes

* **api:** guard restore failures and read state files under lock ([5387b03](https://github.com/romm-streaming/romm-broker/commit/5387b03a751321412669ffdbdede177960a7ecfd))
* **api:** serialize session operations under one lock and guard exit failures ([cc7feea](https://github.com/romm-streaming/romm-broker/commit/cc7feea7de049332ff56ff886a3c1a24289a87e1))
* **azahar:** abort launch when the config patch fails instead of booting past it ([f2ae5b7](https://github.com/romm-streaming/romm-broker/commit/f2ae5b7548d483737c858608a3080f146434191a))
* **base:** require save-clearing emulators to declare it, add lock/handout hooks ([95303cb](https://github.com/romm-streaming/romm-broker/commit/95303cbb0f97eeb636116539cee37c1d594a6435))
* **cemu:** validate both title-id halves and log save-tree walk failures ([e00be28](https://github.com/romm-streaming/romm-broker/commit/e00be2896a5071c4cf01cf9ae098c627446926e7))
* **ci:** let the build-app smoke test run without BROKER_SECRET ([0ba1a9f](https://github.com/romm-streaming/romm-broker/commit/0ba1a9f19e083e043c46ca7f3a2c732cc0cfc4be))
* **ci:** let the build-app smoke test run without BROKER_SECRET ([6585f05](https://github.com/romm-streaming/romm-broker/commit/6585f0535f2c7fb2e792a189e83558f23193f98c))
* **desktop:** log and reraise a launch spawn failure instead of dropping it ([db85e7f](https://github.com/romm-streaming/romm-broker/commit/db85e7ffe4a314c92e504b413d90793454b0fba9))
* **dolphin:** confirm a load on noatime mounts instead of taking it on trust ([eb1fc00](https://github.com/romm-streaming/romm-broker/commit/eb1fc003d14cf9cae557d5bda4e020f91c4699e1))
* **dolphin:** confirm state loads and saves via atime/settle polling instead of blind sleeps ([1582b75](https://github.com/romm-streaming/romm-broker/commit/1582b751b10754c075a02a2c210e7f7e36d19ec3))
* **duckstation:** correctly detect a killed process on exit ([af190d6](https://github.com/romm-streaming/romm-broker/commit/af190d69138d8cc2ef2c7bab195a45bf150378ec))
* **duckstation:** mark resume states by owner instead of matching on serial ([c6afecd](https://github.com/romm-streaming/romm-broker/commit/c6afecdad6a48d365ec21776e29f8dfbfb57b284))
* **eden:** clear stale save data at activate and restamp whole titles at exit ([a566b3e](https://github.com/romm-streaming/romm-broker/commit/a566b3e28c959431851582cf000acbb4b14ea280))
* **flycast:** confirm savestate writes and gate resume on a loadable state ([ce66972](https://github.com/romm-streaming/romm-broker/commit/ce669723230c79b90a634c56f250cfd9e3c98655))
* **flycast:** mark resume state ownership and sweep loose save data at activate ([b5cbe05](https://github.com/romm-streaming/romm-broker/commit/b5cbe054ab0c26f995ca78c745b65b8011203b14))
* **memcard:** clear a stale backup and catch a corrupt member during replace ([c7e1273](https://github.com/romm-streaming/romm-broker/commit/c7e1273ecfa57d681b25830073e44011cc8e036e))
* **memcard:** log instead of silently swallowing stat/size OSErrors ([aaa5331](https://github.com/romm-streaming/romm-broker/commit/aaa53312cdc6884be35324af84b9375b6ed73a17))
* **pcsx2:** scope the working slot per instance and verify loads by disc serial ([a5ba381](https://github.com/romm-streaming/romm-broker/commit/a5ba3815809405764968c36c8bc8b9c672eed20c))
* **ppsspp:** confirm a load by watching the state's access time, not the hotkey send ([ab4da5b](https://github.com/romm-streaming/romm-broker/commit/ab4da5bd6e81d64c848fe56b7e0a26dfcd0bbefc))
* **ppsspp:** wait for the game window before firing a deferred resume load ([854f433](https://github.com/romm-streaming/romm-broker/commit/854f433451c7bbd8b500cacbb5e88d56da20957d))
* **retroarch:** confirm a load actually happened instead of trusting the echo ([b5f53f4](https://github.com/romm-streaming/romm-broker/commit/b5f53f4a5357393e4b4a68f20740ab859f2ab813))
* **retroarch:** fix zero-byte state races and PPSSPP resume timing ([8fd7ff1](https://github.com/romm-streaming/romm-broker/commit/8fd7ff1fbd5e0b27ad15805a895b773b3a0be123))
* **rpcs3:** clear stale save data at activate and verify the final boot target ([a72b61d](https://github.com/romm-streaming/romm-broker/commit/a72b61d20290975f979c3ce5ba0375c4eaef380a))
* **rpcs3:** wire up write confirmation and the deferred leftover-savestate clear ([47083a4](https://github.com/romm-streaming/romm-broker/commit/47083a4ed7d42a811040f27f41a134fc7cf1f01a))
* **saves:** stop misreporting stat/read failures as a clean no-op dump ([3593ea1](https://github.com/romm-streaming/romm-broker/commit/3593ea1eabe8ea137db472ea3b3b30a97778d330))
* **session:** log a refused selkies token push instead of dropping it silently ([58d1e20](https://github.com/romm-streaming/romm-broker/commit/58d1e2010a3bd2b7714189db715cd66186aad6b9))
* **shadps4:** clear stale save data at activate and key extraction cache more tightly ([b031dc7](https://github.com/romm-streaming/romm-broker/commit/b031dc7098e43fdb12d4b28dcc8502bb6ebd9739))
* **shadps4:** guard symlink escapes and detect saves left unmounted by a kill ([3807ddc](https://github.com/romm-streaming/romm-broker/commit/3807ddc15b0e0e153b5daa04651ac0d59eae81c6))
* **xemu:** distinguish a failed save injection/extraction from an empty one ([bd40c99](https://github.com/romm-streaming/romm-broker/commit/bd40c9969a64b4397728a414448f461f31357a7e))
* **xemu:** stop scoping saves to every title on the drive when the id is unknown ([39fca36](https://github.com/romm-streaming/romm-broker/commit/39fca3680331eca84684c93a4262a944922ab47d))
* **xenia:** clear stale save data at activate and restamp whole titles at exit ([55b4b30](https://github.com/romm-streaming/romm-broker/commit/55b4b305a148f85d2aadc4a2867eb8e075ce9a60))


### Documentation

* finish webstation-broker to romm-broker console script rename ([ef5c4b3](https://github.com/romm-streaming/romm-broker/commit/ef5c4b3fa7612caca9d9ac6d6111518f90ed55c3))
* point migration guide at docs.romm.app, not the deleted app-repo file ([2f93036](https://github.com/romm-streaming/romm-broker/commit/2f93036394a63b01bcf1bb806cf3298ab643b598))
* remove zoraxy note ([8892905](https://github.com/romm-streaming/romm-broker/commit/8892905968685116cea6c27b4a832ce3f4b3cede))
* update name ([ec38d80](https://github.com/romm-streaming/romm-broker/commit/ec38d80b0ca0dbf04210c825b5c2d898219048dc))

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
