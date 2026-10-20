# INF4173-SJ — Suivi personnel des candidatures

React + TypeScript + Vite, Django REST Framework et PostgreSQL. Gestion manuelle,
import Gmail et propositions Gemini corrigibles, avec validation humaine
obligatoire. Aucun changement de candidature n'est appliqué automatiquement.

## Fonctionnalités

- Sessions Django, protection CSRF et isolation par utilisateur.
- CRUD, recherche employeur/poste, filtre, huit états et confirmation de réception
  indépendante. Date facultative uniquement pour « À préparer ».
- Import initial sur période inclusive, messages reçus/envoyés/archivés, sans
  spam, corbeille ni brouillons. Lots de dix, pagination, reprise et déduplication.
- Corrections, suggestions d'association, sélection explicite de la cible,
  acceptation/rejet confirmés et historique des changements validés.
- Consultation des erreurs et relances contrôlées. Synchronisation manuelle en
  HTTP 202 et planification serveur de cinq minutes préparée.
- Analyse automatique désactivée par défaut, consentement révocable, exclusion
  des anciens messages et quotas persistants. Navigateur indépendant du worker.

## Installation locale — Windows / PowerShell

Prérequis : Git, Python 3.13 via `py`, Node 24.22.0 avec `npm.cmd`, PostgreSQL 18.
Versions figées : `backend/requirements.txt` et `frontend/package-lock.json`.
Depuis la racine, pour une **nouvelle installation** :

```powershell
py -3.13 -m venv backend/.venv
& backend/.venv/Scripts/python.exe -m ensurepip
& backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
& backend/.venv/Scripts/python.exe -m pip check
if (-not (Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
Set-Location frontend
npm.cmd ci
Set-Location ..
```

Dans votre terminal local, créez une base dédiée et son rôle propriétaire, sans
superutilisateur ni privilèges globaux. Saisissez les mots de passe dans une
invite masquée, jamais dans une commande enregistrée. Éditez `backend/.env`
directement dans votre éditeur ; ne remplacez pas celui d'une installation existante.

Variables : `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`,
`DJANGO_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_REDIRECT_URI`, `OAUTH_SUCCESS_REDIRECT_URL`,
`GOOGLE_TOKEN_ENCRYPTION_KEY`, `GMAIL_MESSAGE_ENCRYPTION_KEY`, `GEMINI_API_KEY`.
Les clés Django/Fernet sont indépendantes. Générez vos clés Fernet uniquement
localement avec `cryptography.fernet.Fernet.generate_key()` et conservez-les dans
un gestionnaire de secrets. Changer une clé sans rotation rend les données déjà
chiffrées illisibles. Les variables du processus priment sur `.env`.

OAuth local : client Web, Gmail API activée, scope
`https://www.googleapis.com/auth/gmail.readonly`, URI exacte
`http://localhost:8000/api/gmail/callback/`, utilisateur Google de test autorisé.
Retour React : `http://localhost:5173/`. Aucun secret dans une variable `VITE_`.
La reconnexion OAuth est explicite et ne lance pas d'import.

Sur une base **neuve et dédiée**, examinez puis appliquez les migrations :

```powershell
Set-Location backend
& .venv/Scripts/python.exe manage.py check
& .venv/Scripts/python.exe manage.py migrate --plan
& .venv/Scripts/python.exe manage.py migrate
& .venv/Scripts/python.exe manage.py createsuperuser
```

Sur une base existante, sauvegardez et vérifiez le plan avant toute migration.
`createsuperuser` est interactif dans le terminal local ; aucune inscription
publique ni compte réel dans les fixtures. Deux terminaux depuis la racine :

```powershell
# Terminal Django
& backend/.venv/Scripts/python.exe backend/manage.py runserver localhost:8000
# Autre terminal : React
Set-Location frontend
npm.cmd run dev -- --host localhost --port 5173 --strictPort
```

Ouvrez `http://localhost:5173` ; conservez le même nom d'hôte pour la session.
Le proxy Vite transmet `/api` à Django. Aucun secret dans `localStorage`.

## Traitements externes : déclenchement explicite

Les appels Gmail/Gemini nécessitent des identifiants réels et transmettent des
données au fournisseur. Les tests ci-dessous les simulent. Ne lancez pas de
worker sur la base réelle pour simplement vérifier l'installation.
Les demandes manuelles/import initial créent un travail persistant, traité hors
requête HTTP par `process_sync_jobs`. Consultez ses options sans traiter de travail :

```powershell
& backend/.venv/Scripts/python.exe backend/manage.py process_sync_jobs --help
```

Une invocation borne la durée et le nombre de comptes. Un timer systemd peut
l'appeler chaque minute pour vider les lots et créer les échéances de cinq
minutes lorsque la configuration l'autorise. Aucun service n'est installé ou
activé par le dépôt. Gmail planifié et Gemini automatique sont désactivés par
défaut. Le consentement IA est explicite et révocable ; aucun ancien message ne
doit être analysé automatiquement. Vérifier aussi les quotas du compte Google.

## Tests reproductibles

Le lanceur Windows réutilise PostgreSQL 18, Chrome installé et Node, sans nouveau
navigateur ni dépendance E2E :

```powershell
& backend/.venv/Scripts/python.exe -B -X utf8 tests/e2e/run.py --temp-root D:/INF4173-SJ-tests
```

Il exige 1 Gio libre sur D: et les ports libres 55484, 18000, 15173 et 19222.
Nouveau cluster PostgreSQL, comptes fictifs et profil Chrome vierge. React,
Django, PostgreSQL et les requêtes HTTP sont réels ; seules les frontières
Gmail/Gemini sont simulées. La base principale est consultée en lecture seule
pour comparer effectifs et empreintes avant/après ; sa configuration locale doit
être disponible. Aucun serveur habituel n'est arrêté. Les processus isolés sont
arrêtés en fin de test ; traces et fichiers temporaires restent sur D:.

La commande couvre les tests backend PostgreSQL, migrations, configuration de
production avec valeurs fictives, scénarios Chrome, tests frontend, TypeScript,
lint et build. Caches/builds sur D:. Voir [le protocole E2E](docs/tests_e2e_INF4173.txt).
Les tests backend utilisent `config.test_postgres_settings`, qui impose des noms
de base/rôle dédiés et un port distinct. Ne les exécutez pas avec les paramètres
applicatifs réels. La CI utilise aussi une base jetable.

Contrôles frontend seuls depuis `frontend` : `npm.cmd test`,
`npm.cmd exec tsc -- -b`, `npm.cmd run lint`, `npm.cmd run build`.
Audit avant ajout Git :

```powershell
& backend/.venv/Scripts/python.exe -B -X utf8 tests/security/audit_repository.py
```

Cet audit retourne chemins, catégories, tailles et empreintes, jamais les valeurs
trouvées. Les secrets locaux ne sont comparés qu'en mémoire. Il ne remplace pas
la revue du contenu ni un scanner exhaustif. Les clés publiques des tests et le
mot de passe éphémère CI sont des fixtures, jamais des secrets d'exploitation.

## Évaluation et preuves

Le [protocole](docs/evaluation_INF4173.txt) décrit 40 courriels fictifs annotés,
les métriques et cinq cas répétés trois fois. Les
[résultats simulés](docs/evaluation_simulee_INF4173.json) valident l'évaluateur,
**pas les performances réelles de Gemini**. Une campagne réelle nécessite un
accord distinct, la revue des annotations et le contrôle des quotas.

Le [protocole 1.1](docs/evaluation_protocole_1_1_INF4173.txt) clarifie les
informations manquantes et privilégie les associations exactes. La
[comparaison hors ligne](docs/comparaison_associations_protocole_1_1_INF4173.json)
réutilise six extractions réelles conservées ; elle ne mesure pas le nouveau
prompt sur Gemini. Toute future campagne utilise un nouveau run-id et des
rapports distincts, après autorisation.

Le [protocole 1.2](docs/evaluation_protocole_1_2_INF4173.txt) ajoute un décodage
HTML limité aux noms de recherche, sans réécrire les extractions enregistrées.
La [comparaison hors ligne dédiée](docs/comparaison_normalisation_html_protocole_1_2_INF4173.json)
distingue les erreurs de sortie originale des associations après normalisation.

Le [bilan actuel des campagnes et de la préparation GitHub](docs/preparation_github_INF4173.txt)
complète ces preuves historiques : le protocole 1.2 compte 14 essais réels sur
50 au 20 octobre 2026. Les rapports de préparation restent inchangés.

Les chemins `D:/INF4173-SJ-tests/...` et autres chemins de traces Windows dans
les documents désignent des emplacements locaux utilisés pour les validations.
Ils ne sont ni publiés avec le dépôt ni accessibles depuis GitHub. Adaptez les
chemins à votre environnement pour reproduire les tests ; leurs journaux et
bases temporaires ne doivent pas être ajoutés à Git.

Le [bilan technique](docs/bilan_livraison_INF4173.txt) distingue implémentation,
tests isolés et validations restantes. Le plan original du 30 septembre reste
inchangé localement dans `docs/plan_de_travail_INF4173.pdf`, exclu du dépôt car il
contient une identité et un identifiant étudiant. Son empreinte figure au bilan.

## Déploiement et limites

[Procédure Linux](docs/deploiement_linux_INF4173.txt) : configuration externe,
`config.production_settings`, Gunicorn, Nginx, HTTPS, systemd, sauvegarde chiffrée,
restauration vers une base neuve et retour arrière. Les exemples `deploy/`
nécessitent une origine HTTPS réelle et des secrets indépendants. Gunicorn est
une dépendance Linux séparée dans `requirements-production.txt`.

GitHub Actions est manuel (`workflow_dispatch`), sans déploiement, identité cloud
ou secret applicatif. Actions figées par SHA. L'exécution distante et ses coûts
éventuels restent à autoriser et vérifier.

Le workflow CI exécute les tests backend PostgreSQL et frontend, TypeScript,
lint, build et les contrôles de configuration. Il **n'exécute pas les tests
navigateur E2E** : les validations Chrome documentées ont été réalisées
localement, avec Gmail et Gemini simulés. Un workflow réussi ne prouvera donc
pas une nouvelle validation navigateur, ni un déploiement Linux opérationnel.

Un workflow manuel distinct, `.github/workflows/linux-https.yml`, prépare la
validation éphémère Nginx/Gunicorn/systemd avec HTTPS exclusivement local,
PostgreSQL fictif et fournisseurs simulés. Il reste à exécuter après autorisation.
Son protocole et les vérifications locales figurent à la fin de la
[procédure Linux](docs/deploiement_linux_INF4173.txt). Aucun service public,
certificat public ou secret réel n'est nécessaire ; les minutes Actions du
dépôt privé doivent rester dans le quota inclus.

Restent à valider : Linux/Nginx/Gunicorn/systemd réels, certificat HTTPS,
restauration d'exploitation, CI distante, autres navigateurs, quotas Google et
campagne Gemini réelle. `check --deploy` conserve W005/W021 (HSTS sous-domaines
et preload non engagés avant validation du domaine). L'audit local n'est ni un
pentest ni un audit CVE complet. Les archives Fernet sont bornées à 64 Mio non
chiffrés en mémoire. Les listes de suivi/propositions sont bornées ; la recherche
et les filtres sont côté React.

Aucune pièce jointe, source MIME ou HTML brut n'est conservé. Textes, propositions
et jetons sont chiffrés côté serveur ; les métadonnées nécessaires restent dans
la base. Revoir les conditions des fournisseurs avant un traitement personnel
en production. Rapports universitaires et vidéo sont des livrables séparés.
