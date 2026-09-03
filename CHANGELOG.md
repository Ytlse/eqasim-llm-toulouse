# Changelog

## Fork Toulouse (llm-agents-gama) — non publié

### 2026-09-03 — Le service Docker applique enfin les réglages d'appariement de `config_toulouse.yml`
Jusqu'ici `generate_population.py` construisait sa propre configuration synpp **sans**
`filter_hts`, `matching_attributes`, `matching_minimum_observations` ni les réglages des journées
donneuses : synpp retombait sur ses défauts — `filter_hts: True`, soit **308 donneurs ENTD**
résidents de Haute-Garonne (323 après le filtre des jours de classe) pour 12 000 personnes à
apparier, seuil de 20 observations et classe d'âge abandonnée avant le sexe par la dégradation —
pendant que `config_toulouse.yml` disait le contraire depuis le ticket 008. Toutes les
populations générées par le service jusqu'à la v3 incluse portent des chaînes d'activités issues
de ce vivier réduit (mesuré sur le vivier v4-répétition avant correction : 13 donneurs de
6-10 ans, 7 de 11-14, 4 de 15-17 ; 63 % des écoliers mobiles avec une activité d'études).
Le wrapper part désormais de `config_toulouse.yml` (monté dans le conteneur, `EQASIM_BASE_CONFIG`)
et ne remplace que les chemins et paramètres d'exécution ; il **refuse** de générer (code 5) si le
fichier manque ou ne fixe pas ces réglages, et imprime les réglages scientifiques retenus.

Deux corrections du même jour : un donneur dont la journée de référence est écartée par le filtre
des jours de classe **sort du vivier** au lieu de devenir un immobile (première version : 40,6 %
d'immobiles dans la population générée, 50 % chez les 6-17 ans, contre 10,6 % dans l'enquête ;
3 295 donneurs écartés, 14 702 Kish restants) ; une régénération forcée **remplace** le fichier
cible existant — le fichier écrit par synpp se reconnaît à sa date, pas à la nouveauté de son
nom — au lieu de rendre l'ancien vivier sous le nom demandé. La sortie standard du service est
mise en tampon ligne par ligne, pour que ses lignes apparaissent dans `docker logs` dans l'ordre.

**Mesuré sur le vivier régénéré (Haute-Garonne, 11 922 personnes, 6 min) :** 0 domicile hors des
453 communes, `household.commune_id` renseigné pour tous (276 communes), **89,0 %** des 6-17 ans
mobiles avec une activité d'études (6-10 ans 91,9 %, 11-14 ans 91,9 %, 15-17 ans 80,4 % — ces
derniers partagent la classe d'âge 15-29 de l'appariement avec les jeunes adultes) contre 57,5 %
dans le vivier v3, immobiles 19,3 % (la sélection les ramène à la cible), 2,93 déplacements par
personne et 3,63 par personne mobile contre 2,58 et 2,88 dans la v3 (enquête : 3,53 et 3,95).

### 2026-09-03 — Le cadre de tirage devient les 453 communes du périmètre, six départements
`config_toulouse.yml` demande désormais les six départements de l'EMC² 2023 (31, 32, 81, 82,
09, 11) et pointe sur la liste des 453 communes (`communes_file`, ticket 031 § 1.1). Le stage
`data.spatial.codes` journalise le cadre retenu par département (346 / 38 / 27 / 22 / 10 / 10)
et **refuse** une commune demandée que le référentiel IRIS ne connaît pas, au lieu de la laisser
sortir du cadre en silence. Le service Docker (`generate_population.py`) vérifie **avant** de
lancer synpp que chaque département demandé a sa BD TOPO et sa BAN, et s'arrête avec la liste de
ce qui manque (code 3) : un département sans données ne se « saute » pas. Son défaut passe aux
six départements ; le déploiement le restreint par `EQASIM_DEPARTMENTS` tant que les données des
cinq autres ne sont pas là.

L'export `llm_agents` renseigne `household.commune_id` et `household.iris_id` pour **tous** les
ménages, à partir du tirage de zone du domicile (`spatial.home.zones`) : la colonne du
recensement valait « undefined » pour 36 % des personnes du vivier v3 (IRIS anonymisés), et le
runtime doit filtrer par commune du domicile (ticket 031, partie 2). « undefined » compte
désormais comme valeur manquante dans le journal des champs de scellement.

### 2026-09-03 — Les chaînes d'activités viennent de jours de classe
Le stage `data.hts.entd.cleaned` ne garde plus que les journées donneuses hors vacances
scolaires (`V2_VAC_SCOL`), et hors mercredi pour les moins de 11 ans (`V2_JOUR_DEP`), réglages
`hts_school_days_only` et `hts_exclude_wednesday_under_age` de `config_toulouse.yml`. Motif :
50 à 54 % seulement des 6-17 ans générés avaient une activité d'études un jour de semaine,
contre 90 à 95 % dans l'EMC² 2023 ; hors vacances et hors mercredi, 88 à 96 % des journées
ENTD ont un trajet vers l'école. Un témoin imprime la part des scolaires mobiles avec trajet
vers l'école et alarme sous 85 % (mesuré sur les donneurs : 72.0 % → 90.8 % ; donneurs 15 687 → 12 392). Deux colonnes de `K_deploc` sont lues en plus.


## [1.4.0](https://github.com/eqasim-org/eqasim-france/compare/v1.3.0...v1.4.0) (2026-02-19)


### Features

* add carAvail attribute for standard MATSim ([#315](https://github.com/eqasim-org/eqasim-france/issues/315)) ([a639bb4](https://github.com/eqasim-org/eqasim-france/commit/a639bb456d8c75fcbc0332def917ae539816845b))
* Add docker and apptainer containers ([#274](https://github.com/eqasim-org/eqasim-france/issues/274)) ([e70a297](https://github.com/eqasim-org/eqasim-france/commit/e70a29701ea6da004526dc18cea13c3c5ca8f401))
* **data:** support emp 2019  ([#298](https://github.com/eqasim-org/eqasim-france/issues/298)) ([e280e27](https://github.com/eqasim-org/eqasim-france/commit/e280e27ea7dcfffee7ab160567886838300176c0))
* **data:** use parquet format for SIRENE data ([#348](https://github.com/eqasim-org/eqasim-france/issues/348)) ([8962295](https://github.com/eqasim-org/eqasim-france/commit/896229550572a873aacea993de854d0b25c20def))
* Full MATSim run, Scenario Cutter and Acoustic maps ([#337](https://github.com/eqasim-org/eqasim-france/issues/337)) ([5435ab6](https://github.com/eqasim-org/eqasim-france/commit/5435ab64a6f25da2a2ec908a6123d0414572bae6))
* handle GTFS without location types ([#309](https://github.com/eqasim-org/eqasim-france/issues/309)) ([4a372d0](https://github.com/eqasim-org/eqasim-france/commit/4a372d09ec56e0c12e1d0cd45cc4ce3420bcd275))
* improve testing ([#422](https://github.com/eqasim-org/eqasim-france/issues/422)) ([c98bfd2](https://github.com/eqasim-org/eqasim-france/commit/c98bfd2d78f66913d3beb9007fd942df48a124f9))
* make use of osmium instead of osmosis ([#321](https://github.com/eqasim-org/eqasim-france/issues/321)) ([2c985ae](https://github.com/eqasim-org/eqasim-france/commit/2c985ae7f86e2fc372ecc49bfc14db4c78df6006))
* optionally use local maven cache ([#316](https://github.com/eqasim-org/eqasim-france/issues/316)) ([f84b295](https://github.com/eqasim-org/eqasim-france/commit/f84b295c5d0ef4e9377d878dbcbedee841f7ae86))
* update projections functionality by department ([#270](https://github.com/eqasim-org/eqasim-france/issues/270)) ([6e17a25](https://github.com/eqasim-org/eqasim-france/commit/6e17a25f4c49fd41c12e5a584c6746dddf313936))
* update to modern numpy rng ([#423](https://github.com/eqasim-org/eqasim-france/issues/423)) ([7401c60](https://github.com/eqasim-org/eqasim-france/commit/7401c600d12d4228d4cd56b24bda336569b4dc81))


### Bug Fixes

* **analysis:** correct and improve population analysis ([#299](https://github.com/eqasim-org/eqasim-france/issues/299)) ([de8d1a2](https://github.com/eqasim-org/eqasim-france/commit/de8d1a2007f43dc7d559e3c98cd688bac05660a0))
* avoid devalidation of gtfs stage ([#387](https://github.com/eqasim-org/eqasim-france/issues/387)) ([299e757](https://github.com/eqasim-org/eqasim-france/commit/299e7571b8cf3ccba4e3fc6f42a3dd2f27a7f019))
* **data:** update BPE to 2024 version ([#347](https://github.com/eqasim-org/eqasim-france/issues/347)) ([20664aa](https://github.com/eqasim-org/eqasim-france/commit/20664aab65d9b6150566dd8eff436db93ba449f3))
* handling of attribute collisions in gtfs ([#428](https://github.com/eqasim-org/eqasim-france/issues/428)) ([a2afaa6](https://github.com/eqasim-org/eqasim-france/commit/a2afaa6f0cc42935a6e6b710189af0bc24e73236))
* improve schools numbers ([#300](https://github.com/eqasim-org/eqasim-france/issues/300)) ([dd7d779](https://github.com/eqasim-org/eqasim-france/commit/dd7d7790e0ab7ecede5cb6a7efef5f9bd59d81ad))
* make processes option volatile ([#438](https://github.com/eqasim-org/eqasim-france/issues/438)) ([2e17910](https://github.com/eqasim-org/eqasim-france/commit/2e17910ba9196b6bd9cdf3575a61c14e3e417848))
* potential improvement of determinism in statistical matching ([#414](https://github.com/eqasim-org/eqasim-france/issues/414)) ([97f0be2](https://github.com/eqasim-org/eqasim-france/commit/97f0be288871fe1d86b7c97acf7783029e180dac))
* restructuring of the runtime stages ([#439](https://github.com/eqasim-org/eqasim-france/issues/439)) ([3a22613](https://github.com/eqasim-org/eqasim-france/commit/3a2261345f9156f12652be7f7a4c9e57f60a3c36))
* url for mobpro in data test script ([#443](https://github.com/eqasim-org/eqasim-france/issues/443)) ([76c1c3a](https://github.com/eqasim-org/eqasim-france/commit/76c1c3af1138921ef921b8f0f97c5c2bf21a35c5))
* **vehicles:** use valid hbefa keys for vehicle types ([#343](https://github.com/eqasim-org/eqasim-france/issues/343)) ([791369c](https://github.com/eqasim-org/eqasim-france/commit/791369c6fe1d49ff2edbd3ac5312490e89ca8f63))
* write crs for matsim ([#407](https://github.com/eqasim-org/eqasim-france/issues/407)) ([2460005](https://github.com/eqasim-org/eqasim-france/commit/24600054c57d1b8acf8559b686d93da6df8b0308))

## [1.3.0](https://github.com/eqasim-org/eqasim-france/compare/v1.2.0...v1.3.0) (2025-01-06)


### Features

* add a full config with all possible entries ([#273](https://github.com/eqasim-org/eqasim-france/issues/273)) ([3034923](https://github.com/eqasim-org/eqasim-france/commit/3034923d88c751133039e55ee0a593915044407a))
* add mode choice stage ([#195](https://github.com/eqasim-org/eqasim-france/issues/195)) ([24650e9](https://github.com/eqasim-org/eqasim-france/commit/24650e9a3f20779cdadcb1d55e13aabc6c96cf5d))
* Add more configurable paths for input data. ([#144](https://github.com/eqasim-org/eqasim-france/issues/144)) ([15d8c2b](https://github.com/eqasim-org/eqasim-france/commit/15d8c2b533b2cf5f6f2258929e8eb0102ce656d0))
* add municipality info ([#258](https://github.com/eqasim-org/eqasim-france/issues/258)) ([e82ae98](https://github.com/eqasim-org/eqasim-france/commit/e82ae98861b85ff93086cc1f4f7c143cf2101589))
* Add option to export detailed link geometries ([#125](https://github.com/eqasim-org/eqasim-france/issues/125)) ([d9a1519](https://github.com/eqasim-org/eqasim-france/commit/d9a151932c0cbb5f93de3e37067112a36df62065))
* Add vehicles to households output ([#149](https://github.com/eqasim-org/eqasim-france/issues/149)) ([9e143c2](https://github.com/eqasim-org/eqasim-france/commit/9e143c25f74bf80350f67a1f314a0525a8380e6b))
* Detect GTFS files in gtfs directory ([#176](https://github.com/eqasim-org/eqasim-france/issues/176)) ([716fe65](https://github.com/eqasim-org/eqasim-france/commit/716fe65bc3436f7def5035fcd55d5c6a6bbc3066))
* Generating PT legs during mode choice + skipping routing during mode choice + update to Eqasim 1.5.0 (v2) ([#230](https://github.com/eqasim-org/eqasim-france/issues/230)) ([00259df](https://github.com/eqasim-org/eqasim-france/commit/00259df99917adffd2a83b6b09de42198c79a1a4))
* improve handling of projections ([#236](https://github.com/eqasim-org/eqasim-france/issues/236)) ([a70b2a3](https://github.com/eqasim-org/eqasim-france/commit/a70b2a34fef877a115410f28d4cd28ce0104a19a))
* improved location of activities and outputs analysis ([#252](https://github.com/eqasim-org/eqasim-france/issues/252)) ([c2224ec](https://github.com/eqasim-org/eqasim-france/commit/c2224ec5c4a197dff39fcce2a035e1fe204a7c4b))
* integrate vehicles by default ([#233](https://github.com/eqasim-org/eqasim-france/issues/233)) ([66969ab](https://github.com/eqasim-org/eqasim-france/commit/66969ab776998927329b507d6c094c44d6cbf438))
* Introduce buildings with attached addresses and weights based on available housing ([#184](https://github.com/eqasim-org/eqasim-france/issues/184)) ([3cbf36a](https://github.com/eqasim-org/eqasim-france/commit/3cbf36a7ba44d940f2075fb6b5b26c65d4639d72))
* Load all input data from zip archives instead of unpacking the files before ([#166](https://github.com/eqasim-org/eqasim-france/issues/166)) ([4abd860](https://github.com/eqasim-org/eqasim-france/commit/4abd8607b96608fc60aad662a7029dabe5fe9c2d))
* load BD-TOPO by department ([#179](https://github.com/eqasim-org/eqasim-france/issues/179)) ([721fe9e](https://github.com/eqasim-org/eqasim-france/commit/721fe9ed22d85a004c7a0996f4d3083a449d0a29))
* optionally not run MATSim ([#231](https://github.com/eqasim-org/eqasim-france/issues/231)) ([b953b42](https://github.com/eqasim-org/eqasim-france/commit/b953b42addfbdb78dce0d9fce4ad6d9ec91c0923))
* **output:** export all outputs in parquet files ([#238](https://github.com/eqasim-org/eqasim-france/issues/238)) ([d90d93e](https://github.com/eqasim-org/eqasim-france/commit/d90d93ec91be8a5d98fbcbb37e141db1d6b25bad))
* Simplify management of BD-TOPO data ([#186](https://github.com/eqasim-org/eqasim-france/issues/186)) ([62b3245](https://github.com/eqasim-org/eqasim-france/commit/62b3245d9b41fb82e05eae4a8a605f3ec292a5e1))
* update latest input data ([#289](https://github.com/eqasim-org/eqasim-france/issues/289)) ([d9b1b78](https://github.com/eqasim-org/eqasim-france/commit/d9b1b78b78452deaab6969156f447b71e50f3d57))
* use bhepop2 package for income assignment ([#243](https://github.com/eqasim-org/eqasim-france/issues/243)) ([f74bd98](https://github.com/eqasim-org/eqasim-france/commit/f74bd98d838be5114d5cac7aa23e73a031a868ee))
* use future population projections ([#210](https://github.com/eqasim-org/eqasim-france/issues/210)) ([f5f3666](https://github.com/eqasim-org/eqasim-france/commit/f5f36666a2d1350093657f7d46bf41dc0e33dfb5))
* use urban typology for activity chain matching ([#209](https://github.com/eqasim-org/eqasim-france/issues/209)) ([43af03e](https://github.com/eqasim-org/eqasim-france/commit/43af03e312ef46c887fddc428b62b0063bb9bfd0))
* verify availability of open data ([#226](https://github.com/eqasim-org/eqasim-france/issues/226)) ([b0c45cc](https://github.com/eqasim-org/eqasim-france/commit/b0c45cc956a80b5679e2f4e16ffa6177d96b86ac))


### Bug Fixes

* Arbitrary order of week days in merged GTFS ([#131](https://github.com/eqasim-org/eqasim-france/issues/131)) ([f963e3b](https://github.com/eqasim-org/eqasim-france/commit/f963e3b2eeb06fbabf15ff872610c9df0d3b5535))
* avoid regenerating OSM when population changes ([#265](https://github.com/eqasim-org/eqasim-france/issues/265)) ([f08437f](https://github.com/eqasim-org/eqasim-france/commit/f08437f5ff2a815ce3890b29faf17c816fa105fc))
* Behaviour of shutil.which ([#128](https://github.com/eqasim-org/eqasim-france/issues/128)) ([2879603](https://github.com/eqasim-org/eqasim-france/commit/2879603d10dc8e5b178b86e89fe7ce42dfd37d01))
* compatibility with older git ([#227](https://github.com/eqasim-org/eqasim-france/issues/227)) ([3a4b23b](https://github.com/eqasim-org/eqasim-france/commit/3a4b23b2e27c8b8c565f8014fc2de64854ab1531))
* conda dependencies ([#214](https://github.com/eqasim-org/eqasim-france/issues/214)) ([ea93307](https://github.com/eqasim-org/eqasim-france/commit/ea93307cdbe14752e9e49df4eae2505eb0d024b6))
* error when using urban_type and egt 2010 ([#267](https://github.com/eqasim-org/eqasim-france/issues/267)) ([7f73d12](https://github.com/eqasim-org/eqasim-france/commit/7f73d120f862276153c152cddf6bcf162399ceb4))
* Fix coordinates format (commas vs points) in BPE. ([#142](https://github.com/eqasim-org/eqasim-france/issues/142)) ([8ccc958](https://github.com/eqasim-org/eqasim-france/commit/8ccc95880591dd240b82ce64308e491b8dbb0946))
* Fix raw mode recognition in ENTD. ([#143](https://github.com/eqasim-org/eqasim-france/issues/143)) ([a597d65](https://github.com/eqasim-org/eqasim-france/commit/a597d6505504d5315a9ef7c652ef1d70ac360c7e))
* Properly treat non-movers in EDGT 44 ([#133](https://github.com/eqasim-org/eqasim-france/issues/133)) ([09ed87a](https://github.com/eqasim-org/eqasim-france/commit/09ed87ae47703d519fc90ef7080f775358412a73))
* Properly treat non-movers in EDGT Lyon ADISP data ([#169](https://github.com/eqasim-org/eqasim-france/issues/169)) ([282b857](https://github.com/eqasim-org/eqasim-france/commit/282b8579a0b1500fd281cc049aff0f7ed1895b88))
* random seeds in secondary location model ([#246](https://github.com/eqasim-org/eqasim-france/issues/246)) ([be43c17](https://github.com/eqasim-org/eqasim-france/commit/be43c17f87ed3339f2ca872e08e0a5c991fe14ee))
* read order for files discovered using glob ([#203](https://github.com/eqasim-org/eqasim-france/issues/203)) ([a2ba98d](https://github.com/eqasim-org/eqasim-france/commit/a2ba98de31026d3a9696a516949a774bdea4e8d9))
* Remaining bug when loading BPE 2019 ([#132](https://github.com/eqasim-org/eqasim-france/issues/132)) ([3457a46](https://github.com/eqasim-org/eqasim-france/commit/3457a468443e0cfb0d848ad6bb07f366570081ed))
* repairing of completely overlapping trips in ENTD ([#247](https://github.com/eqasim-org/eqasim-france/issues/247)) ([5e98fbe](https://github.com/eqasim-org/eqasim-france/commit/5e98fbe224b77e559226ffe9a48f8e3565cc95b4))
* Resolve segfault in statistical matching ([#183](https://github.com/eqasim-org/eqasim-france/issues/183)) ([3198174](https://github.com/eqasim-org/eqasim-france/commit/319817450cf50b5181dc49025a8f001fd2db6689))
* shuffling of primary location candidates ([#242](https://github.com/eqasim-org/eqasim-france/issues/242)) ([e892be8](https://github.com/eqasim-org/eqasim-france/commit/e892be8e4d4d81857d1b68c0427d05cecff78ac9))
* Update documentation to updated data sets ([#160](https://github.com/eqasim-org/eqasim-france/issues/160)) ([1281d93](https://github.com/eqasim-org/eqasim-france/commit/1281d937a0c696ceeec9d35d03989c1d220e3438))
* Update git commit in meta.json ([#181](https://github.com/eqasim-org/eqasim-france/issues/181)) ([305569c](https://github.com/eqasim-org/eqasim-france/commit/305569c071c87734b87220991cc096668c9d850d))
* Update Levenshtein dependency ([#134](https://github.com/eqasim-org/eqasim-france/issues/134)) ([1aecebb](https://github.com/eqasim-org/eqasim-france/commit/1aecebb25f1d0dcfd4e332f7f5c8578eb146ff97))
* Update of outdated data sets to 2019 ([#153](https://github.com/eqasim-org/eqasim-france/issues/153)) ([2daab1d](https://github.com/eqasim-org/eqasim-france/commit/2daab1dfb6d2d5e6c49661f12435857bfbba0cd1))
* update pyarrow to latest version ([#241](https://github.com/eqasim-org/eqasim-france/issues/241)) ([39281ae](https://github.com/eqasim-org/eqasim-france/commit/39281ae63ca3dd42abaada3512dc8d5a91397f0c))
* Update to BPE 2021 ([#130](https://github.com/eqasim-org/eqasim-france/issues/130)) ([1d797a6](https://github.com/eqasim-org/eqasim-france/commit/1d797a67ae1743d1b82c6f2620c3da5a4f08a145))
* version of openpyxl ([#173](https://github.com/eqasim-org/eqasim-france/issues/173)) ([776fcfa](https://github.com/eqasim-org/eqasim-france/commit/776fcfae4ad5b78204fb8dcedbdb22466008f593))
