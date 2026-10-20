# Frontend INF4173-SJ

Installation, configuration et limites : [README principal](../README.md).
Depuis ce dossier : `npm.cmd ci`, `npm.cmd run dev`, `npm.cmd test`,
`npm.cmd exec tsc -- -b`, `npm.cmd run lint`, `npm.cmd run build`.
Le proxy `/api` cible Django sur localhost:8000. Aucun secret dans `VITE_`.

React + TypeScript + Vite, sessions Django et protection CSRF, sans bibliothèque
d'authentification ni de routage. Le lanceur E2E utilise une configuration et des
ports distincts ; voir [le protocole](../docs/tests_e2e_INF4173.txt).
