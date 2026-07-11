# theforeman/foremanctl  [PASS, верифицировано]

- Дата: 2026-06-23T18:30:00
- Ошибок: 298  →  1  (delta=297)
- ACCEPT (заявлено): 1  |  NEEDS_REVIEW: 1  |  REJECT: 0
- **Верификация: 1 REAL_FIX (strong), 0 STILL_FLAGGED, 0 unsafe_accept, 0 semantic_suspicious**

`.github/workflows/release.yml:50` semgrep shell-injection — патч перенёс `${{ github.ref_name }}`/`${{ github.repository }}` из прямой интерполяции в shell-команде в `env:`-переменные, использует их как `"$REF_NAME"`/`"$REPO"` (quoted). Настоящий, содержательный security-фикс. Последний (9-й из 10) проект серии — RedHatInsights/ansible-collections-insights пропущен из-за сетевого сбоя клонирования (2 попытки, timeout к github.com).
